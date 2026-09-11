"""
build_profile.py — 다음 작업 ⑥: 시드 게임의 Steam 공식 태그로 취향 프로파일을 만든다 → profile.json

입력    가장 최근 peek 산출물(파일명의 날짜 · 접미사 기준 — mtime 아님)에서 상태=정상인 게임
원본    raw/seed_items_{날짜}.json — GetItems · GetTagList 응답 그대로. 같은 날 파일이 있으면 API를 다시 부르지 않는다
규칙    seed_library.yaml profile_rules — tags_per_game · min_tag_count · tag_stoplist (④에서 확정)
게이트  태그 커버리지 < min_tag_coverage 이거나 자동 해석률 < min_auto_resolution 이면
        profile.json을 쓰지 않고 종료 코드 1 (HANDOFF: 임계치 미달 시 프로파일 생성을 중단한다)

실행:  python build_profile.py [peek_results_*.csv]
"""

import datetime
import hashlib
import json
import os
import re
import sys

import yaml

import collect
from peek import ROUTE_AUTO, SEED_LIBRARY_PATH, ST_NOT_TARGET, ST_OK
from profile_probe import count_tags, load_rows, sorted_tags, surviving

PROFILE_PATH = "profile.json"
PEEK_PATTERN = re.compile(r"^peek_results_(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.csv$")
SEED_CALL_BUDGET = 10
# GetItems의 type: 0 게임 / 1 데모 / 6 소프트웨어 — 2026-09-10 실측. appdetails와 달리
# OBS · Wallpaper Engine 같은 소프트웨어를 6으로 정확히 준다(실패 6번이 여기선 반복되지 않는다).
GAME_TYPE = 0


def latest_peek_csv(files=None):
    """파일명의 날짜, 그다음 -N 접미사(숫자로)가 가장 큰 것. 엑셀로 열었다 저장해도 바뀌지 않는다."""
    files = os.listdir(".") if files is None else files
    dated = [(m.group(1), int(m.group(2) or 1), f) for f in files if (m := PEEK_PATTERN.match(f))]
    if not dated:
        sys.exit("peek_results_*.csv 가 없다. peek.py 를 먼저 돌릴 것.")
    return max(dated)[2]


def load_rules():
    with open(SEED_LIBRARY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["profile_rules"]


SEED_KEYS = ("games", "not_on_steam", "aliases", "pinned", "profile_rules")


def seed_fingerprint(path=SEED_LIBRARY_PATH):
    """시드 내용의 지문. 주석 · 줄바꿈은 무시하고 내용만 본다. qa V3-7이 profile.json이 **지금** 시드로
    만든 것인지 대조한다 — 시드나 규칙을 고치고 build_profile.py를 안 돌리면 FAIL."""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    blob = json.dumps({k: cfg.get(k) for k in SEED_KEYS}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fetch_seed_raw(appids, day):
    """시드 게임의 GetItems · GetTagList 원본. 같은 날 파일이 있으면 재사용한다.
    호출이 하나라도 끝내 실패하면 저장하지 않는다 — 깨진 원본을 하루 종일 재사용하지 않기 위해서."""
    path = os.path.join(collect.RAW_DIR, f"seed_items_{day}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        have = {a for b in doc["items_batches"] for a in b["appids"]}
        missing = [a for a in appids if a not in have]
        if missing:
            sys.exit(f"{path} 에 없는 appid {len(missing)}건: {missing[:5]} — "
                     f"시드가 바뀌었으면 파일을 옮기고 다시 받을 것(덮어쓰지 않는다).")
        print(f"원본 재사용: {path}")
        return doc, path

    budget = collect.CallBudget(SEED_CALL_BUDGET)
    errors = []
    batches = collect.fetch_items(appids, budget, errors)
    tag_list = collect.call_api(collect.TAGLIST_PATH, {"language": "english"}, "tags", budget, errors)
    failed = sum(1 for b in batches if b["body"] is None) + (tag_list is None)
    if failed:
        for e in errors:
            print(f"  ! {e['path']} 시도{e['attempt']}: {e['error']}", file=sys.stderr)
        sys.exit(f"시드 원본 호출 {failed}건 실패 — 저장하지 않고 멈춘다.")

    doc = {
        "meta": {
            "collected_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
            "requested_appids": len(appids),
            "calls_used": budget.used,
            "errors": errors,
        },
        "items_batches": batches,
        "tag_list": tag_list,
    }
    collect.write_raw(doc, path)
    print(f"원본 저장: {path}")
    return doc, path


def tag_names(doc):
    return {t["tagid"]: t["name"] for t in doc["tag_list"]["response"]["tags"]}


def official_tags(item, names):
    """가중치순 태그 이름. 이름표에 없는 tagid는 '?{id}'로 남긴다 — 버리지 않고 드러낸다."""
    ordered = sorted(item.get("tags") or [], key=lambda t: -t.get("weight", 0))
    return [names.get(t.get("tagid"), f"?{t.get('tagid')}") for t in ordered]


def items_by_appid(doc):
    return {it.get("appid"): it for b in doc["items_batches"] for it in collect.page_items(b["body"])}


def compute_profile(game_tags, top_n, min_count, stoplist):
    """game_tags = {appid: 가중치순 태그}. 제외목록을 **먼저** 빼고 top N을 뽑은 뒤,
    min_count개 이상 게임에서 나온 태그만 남긴다. (순서를 거꾸로 하면 20개 → 14개 — ④ 실측)"""
    stop = set(stoplist)
    top = {a: [t for t in tags if t not in stop][:top_n] for a, tags in game_tags.items()}
    return surviving(count_tags(list(top.values()), None), min_count), top


def resolution_rate(rows):
    """자동 해석 수 / 해석 대상 수. not_on_steam(대상아님)은 분모에서 뺀다."""
    target = [r for r in rows if r["상태"] != ST_NOT_TARGET]
    auto = sum(1 for r in target if r["해석"] == ROUTE_AUTO)
    return auto, len(target)


def gate(metrics, rules):
    """프로파일을 만들면 안 되는 이유 목록. 비어 있어야 통과."""
    problems = []
    if metrics["tag_coverage"] < rules["min_tag_coverage"]:
        problems.append(f"태그 커버리지 {metrics['tag_coverage']:.0%} < 기준 {rules['min_tag_coverage']:.0%}")
    if metrics["auto_resolution"] < rules["min_auto_resolution"]:
        problems.append(f"자동 해석률 {metrics['auto_resolution']:.0%} < 기준 {rules['min_auto_resolution']:.0%}")
    return problems


def steamspy_profile(ok_appids, rules):
    """④의 SteamSpy 기준 프로파일(같은 규칙) — 전후 비교용. 원본이 없으면 None."""
    files = sorted(f for f in os.listdir(collect.RAW_DIR) if re.match(r"^seed_tags_\d{4}-\d{2}-\d{2}\.json$", f))
    if not files:
        return None
    with open(os.path.join(collect.RAW_DIR, files[-1]), encoding="utf-8") as f:
        raw = json.load(f)
    tags = {a: sorted_tags(raw.get(str(a), {})) for a in ok_appids}
    profile, _ = compute_profile({a: t for a, t in tags.items() if t}, rules["tags_per_game"],
                                 rules["min_tag_count"], rules["tag_stoplist"])
    return dict(profile)


def main():
    peek_path = sys.argv[1] if len(sys.argv) > 1 else latest_peek_csv()
    rules = load_rules()
    rows = load_rows(peek_path)
    if not rows or "해석" not in rows[0]:
        sys.exit(f"{peek_path} 에 '해석' 칸이 없다 — 별칭·pinned 도입(2026-09-10) 이전 형식이다.")

    ok_rows = [r for r in rows if r["상태"] == ST_OK]
    appids = [int(r["appid"]) for r in ok_rows]
    day = f"{datetime.datetime.now(collect.KST).date():%Y-%m-%d}"
    print(f"입력: {peek_path}  (정상 {len(appids)})")
    doc, raw_path = fetch_seed_raw(appids, day)

    names = tag_names(doc)
    items = items_by_appid(doc)
    game_tags, missing, non_game = {}, [], []
    for a in appids:
        it = items.get(a)
        if it is None or it.get("success") != 1:
            missing.append(a)                   # 못 받은 게임은 커버리지 분모에 남는다 — 조용히 빠지지 않게
        elif it.get("type") != GAME_TYPE:
            non_game.append(a)                  # 정상으로 분류했던 시드가 공식 API에선 게임이 아님
        else:
            game_tags[a] = official_tags(it, names)
    with_tags = {a: t for a, t in game_tags.items() if t}

    profile, top = compute_profile(with_tags, rules["tags_per_game"], rules["min_tag_count"], rules["tag_stoplist"])
    auto, target = resolution_rate(rows)
    vocabulary = set(names.values())
    metrics = {
        "seed_games": len(appids),
        "games_with_tags": len(with_tags),
        "tag_coverage": round(len(with_tags) / len(appids), 3) if appids else 0.0,
        "items_missing": missing,
        "non_game": non_game,
        "auto_resolved": auto,
        "resolution_target": target,
        "auto_resolution": round(auto / target, 3) if target else 0.0,
        "unknown_tagids": sorted({t for tags in with_tags.values() for t in tags if t.startswith("?")}),
        "stoplist_not_in_vocabulary": [t for t in rules["tag_stoplist"] if t not in vocabulary],
    }

    print(f"태그 반영 게임 {metrics['games_with_tags']}/{metrics['seed_games']} = {metrics['tag_coverage']:.0%}"
          f"  (못 받음 {len(missing)} · 게임 아님 {len(non_game)})")
    print(f"자동 해석률 {auto}/{target} = {metrics['auto_resolution']:.0%}")
    if metrics["unknown_tagids"]:
        print(f"경고: 이름표에 없는 tagid {metrics['unknown_tagids']}")
    if metrics["stoplist_not_in_vocabulary"]:
        print(f"경고: 제외목록 중 공식 태그에 없는 이름 {metrics['stoplist_not_in_vocabulary']} — 오타인지 확인")
    print(f"\n[공식 태그 프로파일] top{rules['tags_per_game']} · min {rules['min_tag_count']}"
          f" · 제외목록 {len(rules['tag_stoplist'])}개 — {len(profile)}개")
    print("  " + ", ".join(f"{t}({c})" for t, c in profile))

    before = steamspy_profile([int(r["appid"]) for r in ok_rows], rules)
    if before is not None:
        after = dict(profile)
        print(f"\n[④ SteamSpy 기준과 비교] {len(before)}개 → {len(after)}개")
        print(f"  새로 들어옴: {sorted(set(after) - set(before)) or '없음'}")
        print(f"  빠짐:       {sorted(set(before) - set(after)) or '없음'}")

    problems = gate(metrics, rules)
    if problems:
        print("\n게이트 미달 — profile.json을 쓰지 않는다:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1

    names_by_appid = {a: items[a].get("name", "") for a in with_tags}
    out = {
        "generated_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
        "source": {"peek_csv": peek_path, "raw": raw_path, "tags": collect.ITEMS_PATH},
        "seed_fingerprint": seed_fingerprint(),
        "rules": {k: rules[k] for k in ("tags_per_game", "min_tag_count", "tag_stoplist",
                                         "min_tag_coverage", "min_auto_resolution")},
        "metrics": metrics,
        "tags": [{"name": t, "games": c} for t, c in profile],
        "games": [{"appid": a, "name": names_by_appid[a], "top_tags": top[a]} for a in with_tags],
    }
    with open(PROFILE_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"\n저장: {PROFILE_PATH}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
