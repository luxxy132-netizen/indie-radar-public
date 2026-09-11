"""
build_user_profiles.py — 구독자마다 스팀 라이브러리(플레이 시간 상위) + 좋아하는 게임 링크로 취향을 만든다
→ profiles/{id}.json. 운영자처럼 profile_path가 있는 사람은 건드리지 않는다(2026-09-11 사람과 정함).

명단   subscribers.yaml — 운영자가 직접 추가한다. id · slack_user · (steam_profile | favorites | profile_path)
규칙   seed_library.yaml user_profile_rules + profile_rules의 tags_per_game · tag_stoplist(운영자 취향과 같은 규칙)
키     환경변수 STEAM_API_KEY — 스팀 라이브러리는 키 없이 못 받는다(2026-09-11 확인: 게임 목록 XML은 로그인으로
       넘어가고, GetOwnedGames는 401). 키는 어디에도 남기지 않는다 — 키가 들어가는 호출은 collect.call_api를
       쓰지 않는다(그쪽은 오류에 예외 메시지 = URL을 남긴다)
개인정보 보유 게임 전체 목록 · steamid는 저장하지 않는다. 고른 시드(appid · 이름 · 플레이 시간)만 남긴다

실패    다시 못 만들면 지난 profiles/{id}.json을 그대로 두고(리포트가 "지난 취향"으로 보낸다) 종료 코드 1.
        조용히 빈 취향이나 좋아하는 게임만으로 만든 얇은 취향으로 바꾸지 않는다

실행:  python build_user_profiles.py [--only ID] [--dry-run]
종료 코드: 0 전원 정상 / 1 누구라도 새로 못 만듦(지난 취향 사용 포함)
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

import requests
import yaml

import collect
from build_profile import GAME_TYPE, compute_profile, items_by_appid, official_tags, tag_names
from peek import SEED_LIBRARY_PATH

SUBSCRIBERS_PATH = "subscribers.yaml"
PROFILES_DIR = "profiles"
LAST_RUN_PATH = os.path.join(PROFILES_DIR, "_last_run.json")
VANITY_PATH = "ISteamUser/ResolveVanityURL/v1/"
OWNED_PATH = "IPlayerService/GetOwnedGames/v1/"
MAX_RETRIES = 2
SLEEP_SEC = 2
TIMEOUT_SEC = 30
ITEMS_CALL_BUDGET = 10          # 한 사람당 GetItems(50개씩). 후보 top_n × 3 + 좋아하는 게임이면 2~3회

ID_RE = re.compile(r"^[a-z0-9]+$")                  # 리포트 파일 이름에 들어간다 — '_' · '-'는 구분자라 안 된다
SLACK_USER_RE = re.compile(r"^[UW][A-Z0-9]+$")      # 슬랙 멤버 ID
PROFILE_URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?steamcommunity\.com/(profiles|id)/([^/?#]+)/?(?:[?#].*)?$")
STEAMID_RE = re.compile(r"^\d{17}$")
VANITY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
STORE_APP_RE = re.compile(r"^(?:https?://)?store\.steampowered\.com/app/(\d+)(?:[/?#].*)?$")

ST_BUILT = "갱신"
ST_FIXED = "고정(profile_path)"
ST_STALE = "지난 취향 사용"
ST_NONE = "취향 없음"


# --- 입력 ----------------------------------------------------------------------

def parse_profile_url(url):
    """("steamid", 17자리) 또는 ("vanity", 사용자 지정 이름). 스팀 커뮤니티 프로필 주소가 아니면 ValueError."""
    m = PROFILE_URL_RE.match((url or "").strip())
    if m and m.group(1) == "profiles" and STEAMID_RE.match(m.group(2)):
        return "steamid", m.group(2)
    if m and m.group(1) == "id" and VANITY_RE.match(m.group(2)):
        return "vanity", m.group(2)
    raise ValueError(f"스팀 프로필 주소가 아니다: {url!r} (예: https://steamcommunity.com/id/이름)")


def favorite_appid(url):
    m = STORE_APP_RE.match((url or "").strip())
    if not m:
        raise ValueError(f"스팀 상점 게임 주소가 아니다: {url!r} (예: https://store.steampowered.com/app/892970/)")
    return int(m.group(1))


def load_subscribers(path=SUBSCRIBERS_PATH):
    """명단을 읽고 검증한다. 사람이 손으로 쓰는 파일이라 틀리면 바로 멈춘다(무엇이 틀렸는지와 함께)."""
    with open(path, encoding="utf-8") as f:
        entries = (yaml.safe_load(f) or {}).get("subscribers") or []
    subs, seen = [], set()
    for n, e in enumerate(entries, 1):
        where = f"{path} {n}번째"
        if not isinstance(e, dict):
            raise ValueError(f"{where}: 항목 모양이 아니다")
        sid = str(e.get("id") or "")
        if not ID_RE.match(sid):
            raise ValueError(f"{where}: id는 영어 소문자 · 숫자만({sid!r})")
        if sid in seen:
            raise ValueError(f"{where}: 중복 id {sid!r}")
        seen.add(sid)
        if not SLACK_USER_RE.match(str(e.get("slack_user") or "")):
            raise ValueError(f"{where}({sid}): slack_user는 슬랙 멤버 ID(U로 시작, 대문자 · 숫자)여야 한다")
        if not (e.get("profile_path") or e.get("steam_profile") or e.get("favorites")):
            raise ValueError(f"{where}({sid}): 취향 재료가 없다 — steam_profile · favorites · profile_path 중 하나")
        if e.get("profile_path") is not None and not isinstance(e["profile_path"], str):
            raise ValueError(f"{where}({sid}): profile_path는 파일 경로(글자)여야 한다")
        if e.get("steam_profile"):
            try:
                parse_profile_url(e["steam_profile"])
            except ValueError as err:
                raise ValueError(f"{where}({sid}) steam_profile: {err}") from None
        try:
            favs = [favorite_appid(u) for u in e.get("favorites") or []]
        except ValueError as err:
            raise ValueError(f"{where}({sid}) favorites: {err}") from None
        subs.append({**e, "id": sid, "favorite_appids": list(dict.fromkeys(favs))})
    return subs


def load_user_rules(path=SEED_LIBRARY_PATH):
    """(user_profile_rules, 제외목록, 게임당 태그 수). 제외목록 · 태그 수는 운영자 취향과 같은 값을 쓴다."""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg["user_profile_rules"], cfg["profile_rules"]["tag_stoplist"], cfg["profile_rules"]["tags_per_game"]


# --- 키가 들어가는 호출 --------------------------------------------------------

def steam_web_get(path, params, key, errors, get=requests.get, sleep=time.sleep):
    """키가 필요한 스팀 웹 API 호출 하나. 응답 dict 또는 None.
    오류에는 예외 종류 · HTTP 코드만 남긴다 — 예외 메시지엔 요청 URL(= 키)이 들어 있다."""
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            sleep(SLEEP_SEC)
        try:
            r = get(f"{collect.API}/{path}", params={**params, "key": key}, timeout=TIMEOUT_SEC)
        except requests.RequestException as e:
            errors.append(f"{path} 시도{attempt}: {type(e).__name__}")
            continue
        if r.status_code != 200:
            errors.append(f"{path} 시도{attempt}: HTTP {r.status_code}")
            if 400 <= r.status_code < 500 and r.status_code != 429:
                break                           # 키 거절(401 · 403) 같은 건 다시 해도 같다
            continue
        try:
            body = r.json()
        except ValueError:
            errors.append(f"{path} 시도{attempt}: JSON이 아니다")
            continue
        if isinstance(body, dict):
            return body
        errors.append(f"{path} 시도{attempt}: 응답 형식 이상")
    return None


def resolve_steamid(url, key, get=requests.get, sleep=time.sleep):
    """(steamid, None) 또는 (None, 문제)."""
    kind, value = parse_profile_url(url)
    if kind == "steamid":
        return value, None
    errors = []
    body = steam_web_get(VANITY_PATH, {"vanityurl": value}, key, errors, get=get, sleep=sleep)
    if body is None:
        return None, "스팀 프로필 주소 해석 호출 실패 — " + " / ".join(errors)
    resp = body.get("response") or {}
    if resp.get("success") == 1 and STEAMID_RE.match(str(resp.get("steamid") or "")):
        return str(resp["steamid"]), None
    return None, f"스팀 프로필 주소 {value!r}를 찾지 못했다 — 주소를 다시 받을 것"


def owned_games(steamid, key, get=requests.get, sleep=time.sleep):
    """(보유 게임 목록, None) 또는 (None, 문제). 비공개 · 플레이 시간 비공개는 각각 따로 알린다."""
    errors = []
    params = {"steamid": steamid, "include_appinfo": 1, "include_played_free_games": 1, "format": "json"}
    body = steam_web_get(OWNED_PATH, params, key, errors, get=get, sleep=sleep)
    if body is None:
        return None, "보유 게임 호출 실패 — " + " / ".join(errors)
    games = [g for g in (body.get("response") or {}).get("games") or [] if isinstance(g, dict)]
    if not games:
        # 비공개면 스팀은 오류 대신 {"response": {}}를 준다
        return None, "보유 게임을 못 봤다 — 스팀 개인정보 설정의 '게임 세부 정보'가 비공개이거나 게임이 없다"
    if not any((g.get("playtime_forever") or 0) > 0 for g in games):
        return None, "플레이 시간이 전부 0이다 — 스팀 개인정보 설정에서 '총 플레이 시간'이 비공개인 것 같다"
    return games, None


# --- 시드 고르기 ---------------------------------------------------------------

def candidate_appids(games, rules):
    """플레이 시간순 후보. 최소 시간 미만은 빼고, top_n의 candidate_factor배까지 —
    소프트웨어(Wallpaper Engine 등)가 시간을 많이 먹어서 게임만 남기면 줄어든다."""
    kept = [g for g in games if (g.get("playtime_forever") or 0) >= rules["min_playtime_min"]]
    kept.sort(key=lambda g: -(g.get("playtime_forever") or 0))
    return [g["appid"] for g in kept[:rules["top_n"] * rules["candidate_factor"]]]


def min_tag_count_for(seed_games, rules):
    """시드가 적으면 기준을 낮춘다 — 22개에 3이면 Cozy · Puzzle 같은 취향이 전부 빠졌다(2026-09-11 실측)."""
    return rules["min_tag_count"] if seed_games >= rules["large_seed_games"] else rules["min_tag_count_small"]


def is_game(it):
    return it is not None and it.get("success") == 1 and it.get("type") == GAME_TYPE


def pick_seeds(candidates, favorites, items, rules):
    """(시드 appid 목록, 뺀 appid 목록). 라이브러리에서 게임만 top_n개, 그다음 좋아하는 게임을 더한다."""
    seeds, skipped = [], []
    for a in candidates:
        if len(seeds) >= rules["top_n"]:
            break
        (seeds if is_game(items.get(a)) else skipped).append(a)
    for a in favorites:
        if a in seeds:
            continue
        (seeds if is_game(items.get(a)) else skipped).append(a)
    return seeds, skipped


# --- 한 사람 --------------------------------------------------------------------

def build_one(sub, key, rules, stoplist, tags_per_game, names, get=requests.get, sleep=time.sleep):
    """(취향 dict, []) 또는 (None, 문제 목록)."""
    favorites = sub.get("favorite_appids") or []
    library, playtime = None, {}
    if sub.get("steam_profile"):
        if not key:
            return None, ["STEAM_API_KEY가 없다 — 스팀 프로필을 못 읽는다"]
        steamid, problem = resolve_steamid(sub["steam_profile"], key, get=get, sleep=sleep)
        if problem:
            return None, [problem]
        library, problem = owned_games(steamid, key, get=get, sleep=sleep)
        if problem:
            return None, [problem]
        playtime = {g.get("appid"): g.get("playtime_forever") or 0 for g in library}
    candidates = candidate_appids(library or [], rules)

    ask = list(dict.fromkeys(candidates + favorites))
    errors = []
    batches = collect.fetch_items(ask, collect.CallBudget(ITEMS_CALL_BUDGET), errors, get=get, sleep=sleep)
    if any(b["body"] is None for b in batches):
        # collect.call_api의 오류 메시지엔 요청 URL = 이 사람의 라이브러리 appid가 통째로 들어 있다.
        # 커밋되는 _last_run.json · 이슈 본문에 남기지 않게 오류 종류만 쓴다(코드 리뷰 2026-09-11)
        kinds = sorted({str(e.get("error")).split(":")[0] for e in errors})
        return None, ["상점 정보(GetItems) 호출 실패 — " + " / ".join(kinds)]
    items = items_by_appid({"items_batches": batches})
    seeds, skipped = pick_seeds(candidates, favorites, items, rules)

    game_tags = {a: official_tags(items[a], names) for a in seeds}
    with_tags = {a: t for a, t in game_tags.items() if t}
    min_count = min_tag_count_for(len(with_tags), rules)
    # 성인 콘텐츠 태그는 제외목록처럼 먼저 뺀다 — 그 자리를 그 게임의 다른 태그가 채운다(2026-09-11 사람 결정)
    stop = list(stoplist) + [t for t in rules.get("exclude_tags") or [] if t not in stoplist]
    profile, top = compute_profile(with_tags, tags_per_game, min_count, stop)

    problems = []
    if len(with_tags) < rules["min_seed_games"]:
        problems.append(f"태그가 있는 시드 게임 {len(with_tags)}개 < 기준 {rules['min_seed_games']}개")
    if len(profile) < rules["min_profile_tags"]:
        problems.append(f"취향 태그 {len(profile)}개 < 기준 {rules['min_profile_tags']}개")
    if problems:
        return None, problems

    fav = set(favorites)
    return {
        "generated_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
        "subscriber": sub["id"],
        "source": {"steam_library": library is not None, "favorites": len(favorites)},
        "rules": {**rules, "tags_per_game": tags_per_game, "tag_stoplist": stop,
                  "applied_min_tag_count": min_count},
        "metrics": {"library_games": len(library) if library is not None else None, "candidates": len(candidates),
                    "seed_games": len(seeds), "games_with_tags": len(with_tags), "skipped_non_game": skipped},
        "tags": [{"name": t, "games": c} for t, c in profile],
        "games": [{"appid": a, "name": items[a].get("name", ""),
                   "playtime_h": round(playtime[a] / 60, 1) if a in playtime else None,
                   "favorite": a in fav, "top_tags": top[a]} for a in with_tags],
    }, []


# --- 실행 ----------------------------------------------------------------------

def profile_file(sid):
    return f"{PROFILES_DIR}/{sid}.json"          # 리포트에 기록되는 경로 — OS와 상관없이 '/'로


def parse_args(argv):
    ap = argparse.ArgumentParser(description="구독자별 취향 파일 만들기")
    ap.add_argument("--only", help="이 id 한 사람만")
    ap.add_argument("--dry-run", action="store_true", help="파일을 쓰지 않고 결과만 출력")
    return ap.parse_args(argv)


def fetch_tag_names(get, sleep):
    errors = []
    body = collect.call_api(collect.TAGLIST_PATH, {"language": "english"}, "tags",
                            collect.CallBudget(1 + collect.MAX_RETRIES), errors, get=get, sleep=sleep)
    return (tag_names({"tag_list": body}), None) if body else (None, "태그 이름표(GetTagList) 호출 실패")


def main(argv=None, get=requests.get, sleep=time.sleep):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    subs = load_subscribers()
    if args.only:
        subs = [s for s in subs if s["id"] == args.only]
        if not subs:
            sys.exit(f"명단에 없는 id: {args.only}")
    rules, stoplist, per_game = load_user_rules()
    key = os.environ.get("STEAM_API_KEY", "").strip() or None

    # profile_path가 있는 사람(운영자)은 평소엔 건드리지 않는다. 다만 --dry-run이고 steam_profile도 적혀 있으면
    # 스팀으로 만들어 출력만 한다 — 지금 취향과 비교해 기준값을 재는 용도(코드 리뷰 2026-09-11)
    todo = [s for s in subs if not s.get("profile_path") or (args.dry_run and s.get("steam_profile"))]
    names, names_problem = (None, None)
    if todo:
        names, names_problem = fetch_tag_names(get, sleep)

    results = {}
    for s in subs:
        sid = s["id"]
        if s not in todo:
            results[sid] = {"status": ST_FIXED, "path": s["profile_path"], "problems": []}
            continue
        profile, problems = (build_one(s, key, rules, stoplist, per_game, names, get=get, sleep=sleep)
                             if names else (None, [names_problem]))
        path = profile_file(sid)
        if profile:
            status = ST_BUILT
            if not args.dry_run:
                os.makedirs(PROFILES_DIR, exist_ok=True)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(profile, f, ensure_ascii=False, indent=1)
        else:
            status = ST_STALE if os.path.exists(path) else ST_NONE
        results[sid] = {"status": status, "path": path, "problems": problems}
        print(f"[{sid}] {status}" + (f" — {' / '.join(problems)}" if problems else ""))
        if profile:
            m = profile["metrics"]
            print(f"  시드 {m['games_with_tags']}개(라이브러리 {m['library_games']} · 뺀 것 {len(m['skipped_non_game'])}) → "
                  f"태그 {len(profile['tags'])}개: " + ", ".join(f"{t['name']}({t['games']})" for t in profile["tags"]))

    if not args.dry_run:
        os.makedirs(PROFILES_DIR, exist_ok=True)
        with open(LAST_RUN_PATH, "w", encoding="utf-8") as f:
            json.dump({"run_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
                       "results": results}, f, ensure_ascii=False, indent=1)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## 취향 갱신\n\n" + "\n".join(
                f"- {sid}: {r['status']}" + (f" — {' / '.join(r['problems'])}" if r["problems"] else "")
                for sid, r in results.items()) + "\n")
    return 1 if any(r["problems"] for r in results.values()) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
