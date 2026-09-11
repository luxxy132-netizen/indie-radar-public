"""
peek.py — seed_library.yaml의 games를 Steam에서 찾아 상태 / type / genres / tags 를 표로 찍어본다.
검증 안 함. 눈으로 보는 용도. 결과 표는 날짜를 붙인 CSV로 남긴다(덮어쓰지 않는다).

실행:  python peek.py
필요:  pip install requests pyyaml
API 키 필요 없음 (storesearch, appdetails, SteamSpy 모두 공개 엔드포인트)
(ISteamApps/GetAppList/v2 폐지로 storesearch 기반 검색으로 교체됨)
tags는 SteamSpy(appdetails)의 유저 태그 상위 5개. 실패해도 표는 계속 진행한다.
not_on_steam 항목은 해석하지 않고 행만 남긴다(분모에서도 뺀다).

검색실패의 사유도 대응이 다르므로 뭉치지 않는다:
  후보0건            storesearch가 아무것도 못 돌려줌 (한글 term은 전부 여기)
  일치없음(후보N건)   후보는 왔는데 정확히 맞는 이름이 없음 → 별칭으로 해결 가능
  모호(N건)          정규화 후 같은 이름이 복수 → 사람이 골라야 한다

수작업 보정은 seed의 aliases(이름 교체) / pinned(appid 직접)로 들어오고,
CSV의 `해석` 칸에 경로가 남는다. 자동 해석률에 수작업분을 섞지 않기 위한 장치다.

상태(status)는 대응 방법이 다른 것끼리 절대 섞지 않는다:
  대상아님    seed의 not_on_steam. 애초에 Steam 게임이 아님.        → 해석을 시도하지 않는다
  검색실패    이름 → appid 해석 실패                                → 사유별로 대응이 다르다(아래)
  조회실패    appdetails success=false. API가 데이터를 못 줌.        → 나중에 재시도(미출시작은 출시되면 살아남)
  제외        type이 game이 아님(demo/dlc/…). 정상 응답.            → 영구 제외
  소프트웨어  type=game이지만 장르가 소프트웨어(Utilities 등).      → 영구 제외 (아래 주석 참고)
  정상        프로파일 산출 대상
"""

import csv
import datetime
import os
import re
import sys
import time
import unicodedata

import requests
import yaml

SEED_LIBRARY_PATH = "seed_library.yaml"


def next_output_path():
    """날짜별 산출물은 덮어쓰지 않는다. 비교가 곧 시계열이다.
    같은 날 두 번 돌리면 -2, -3 을 붙인다. 조용히 덮어쓰느니 파일이 하나 더 생기는 게 낫다."""
    base = f"peek_results_{datetime.date.today():%Y-%m-%d}"
    path = f"{base}.csv"
    n = 2
    while os.path.exists(path):
        path = f"{base}-{n}.csv"
        n += 1
    return path


OUTPUT_CSV_PATH = next_output_path()

# ISteamApps/GetAppList/v2 was retired by Valve; storesearch is the public,
# no-key replacement for looking up a single game by name.
SEARCH_URL = "https://store.steampowered.com/api/storesearch/"
DETAILS_URL = "https://store.steampowered.com/api/appdetails"
SPY_URL = "https://steamspy.com/api.php"

# Steam 장르 ID 50~59는 소프트웨어 전용 구간이다.
# (50 Accounting / 51 Animation & Modeling / 52 Audio Production / 53 Design & Illustration /
#  54 Education / 55 Photo Editing / 56 Software Training / 57 Utilities / 58 Video Production /
#  59 Web Publishing)
# appdetails가 OBS Studio·Wallpaper Engine·VRoid Studio·VTuber Editor를 전부 type="game"으로
# 돌려주기 때문에 type만으로는 소프트웨어를 걸러낼 수 없다. 2026-09-10 실측으로 확인.
SOFTWARE_GENRE_IDS = {str(i) for i in range(50, 60)}

# 상태 라벨 — 카운터 키와 CSV 값이 같아야 나중에 대조가 된다.
ST_OK = "정상"
ST_EXCLUDED = "제외"
ST_SOFTWARE = "소프트웨어"
ST_DETAIL_FAIL = "조회실패"
ST_SEARCH_FAIL = "검색실패"
ST_NOT_TARGET = "대상아님"

# 해석 경로 — 자동 해석률의 분자에 수작업분이 섞이지 않게 구분한다.
ROUTE_AUTO = "자동"
ROUTE_ALIAS = "별칭"
ROUTE_PINNED = "appid고정"


def load_seed(path):
    """seed_library.yaml을 통째로 읽는다. games / not_on_steam / aliases / pinned."""
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    return (
        config.get("games", []),
        config.get("not_on_steam", []),
        config.get("aliases") or {},
        config.get("pinned") or {},
    )


def normalize(s):
    """비교용으로 이름을 단순화한다. 억양·대소문자·기호·공백 차이를 없앤다."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^\w\s]", " ", s)     # : ! ' - 같은 기호는 공백으로 ("A:B"와 "A: B"를 같게)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def search_candidates(name):
    """storesearch로 이름 후보 목록을 받는다 (앱 타입만)."""
    r = requests.get(SEARCH_URL, params={"term": name, "cc": "kr", "l": "english"}, timeout=30)
    r.raise_for_status()
    items = r.json().get("items", [])
    return [a for a in items if a.get("type") == "app"]


def find_appid(name, candidates):
    """이름으로 appid를 찾는다. 완전일치 → 정규화 일치 순. 실패 사유는 대응이 다르므로 나눠서 돌려준다."""
    if not candidates:                                  # 검색어가 엔드포인트에 안 걸림
        return None, "후보0건"

    exact = [a for a in candidates if a["name"] == name]  # 1차: 그대로 일치
    if len(exact) == 1:
        return exact[0]["id"], "완전일치"
    if len(exact) > 1:                                  # 같은 이름이 둘이면 첫 번째를 고르지 않는다
        return None, f"모호({len(exact)}건)"

    target = normalize(name)
    hits = [a for a in candidates if normalize(a["name"]) == target]
    if len(hits) == 1:                                  # 2차: 기호 무시하고 일치
        return hits[0]["id"], "정규화일치"
    if len(hits) > 1:                                   # 사람이 골라야 함
        return None, f"모호({len(hits)}건)"

    return None, f"일치없음(후보{len(candidates)}건)"    # 이름 보강으로 해결 가능


def resolve(name, aliases, pinned):
    """이름 → (appid, 해석경로, 사유, 실제 검색어). 자동 → 별칭 순으로만 시도한다."""
    if name in pinned:                                  # 검색으로는 못 찾는 것. 사람이 박아둔 값.
        return pinned[name], ROUTE_PINNED, "seed pinned", "-"

    appid, how = find_appid(name, search_candidates(name))
    if appid is not None:
        return appid, ROUTE_AUTO, how, name

    alias = aliases.get(name)
    if alias is None:
        return None, "-", how, name

    alias_appid, alias_how = find_appid(alias, search_candidates(alias))
    if alias_appid is not None:
        return alias_appid, ROUTE_ALIAS, alias_how, alias
    return None, "-", f"{how} → 별칭도 {alias_how}", alias


def get_details(appid):
    """appdetails 호출. 응답은 {"앱ID": {"success": ..., "data": {...}}} 모양."""
    r = requests.get(DETAILS_URL, params={"appids": appid, "cc": "kr"}, timeout=30)
    r.raise_for_status()
    entry = r.json().get(str(appid), {})
    if not entry.get("success") or not entry.get("data"):
        # success=true인데 data가 비어 오는 경우도 '제외'가 아니라 재시도 대상이다.
        return None
    return entry["data"]


def software_genres(data):
    """소프트웨어 전용 장르(ID 50~59)만 뽑는다. 게임이면 빈 리스트."""
    return [g["description"] for g in data.get("genres", [])
            if str(g.get("id")) in SOFTWARE_GENRE_IDS]


def classify(data):
    """상세 응답 하나를 (상태, 사유)로 판정한다. 대응이 다른 것끼리 섞지 않기 위한 유일한 지점."""
    app_type = data.get("type", "-")
    if app_type != "game":
        return ST_EXCLUDED, f"type={app_type}"

    sw = software_genres(data)
    if sw:
        return ST_SOFTWARE, "type=game이지만 소프트웨어 장르: " + ", ".join(sw)

    return ST_OK, ""


# SteamSpy 응답 상태 — "태그 없음"을 한 칸에 뭉치지 않는다. 원인이 다르면 대응도 다르다.
SPY_OK = "있음"
SPY_STUB = "미수집"      # SteamSpy가 앱을 추적하지 않음. 리뷰 0/0에 owners "0 .. 20,000" 스텁이 온다
SPY_EMPTY = "빈태그"     # 데이터는 있는데 태그만 없음
SPY_ERROR = "오류"       # 네트워크·JSON 오류 또는 에러 응답


def fetch_spy(appid):
    """SteamSpy 응답을 그대로 돌려준다. 실패도 버리지 않고 기록으로 남긴다."""
    try:
        r = requests.get(SPY_URL, params={"request": "appdetails", "appid": appid}, timeout=30)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def spy_status(resp):
    """SteamSpy 응답 하나를 있음 / 미수집 / 빈태그 / 오류로 판정한다."""
    if not isinstance(resp, dict) or "_error" in resp or "error" in resp:
        return SPY_ERROR
    tags = resp.get("tags")
    if isinstance(tags, dict) and tags:
        return SPY_OK
    # 스텁은 owners에 그럴듯한 값("0 .. 20,000")을 채워 보낸다. 리뷰 수 0/0으로 가려낸다.
    if resp.get("positive", 0) == 0 and resp.get("negative", 0) == 0:
        return SPY_STUB
    return SPY_EMPTY


def get_top_tags(appid, limit=5):
    """(득표순 상위 N개 태그 문자열, SteamSpy 상태). 태그가 없으면 왜 없는지가 핵심이다."""
    resp = fetch_spy(appid)
    status = spy_status(resp)
    if status != SPY_OK:
        return f"태그 없음({status})", status
    top = sorted(resp["tags"].items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return ", ".join(name for name, _votes in top), status


def main():
    games, not_on_steam, aliases, pinned = load_seed(SEED_LIBRARY_PATH)
    rows = []

    stats = {
        "시드전체": len(games) + len(not_on_steam),
        "해석대상": len(games),
        ST_NOT_TARGET: len(not_on_steam),
        ROUTE_AUTO: 0,
        ROUTE_ALIAS: 0,
        ROUTE_PINNED: 0,
        ST_SEARCH_FAIL: 0,
        ST_DETAIL_FAIL: 0,
        ST_EXCLUDED: 0,
        ST_SOFTWARE: 0,
        ST_OK: 0,
        "태그있음": 0,
        "태그없음": 0,
    }
    fail_reasons = {}                                  # 검색실패 내역. 사유별로 대응이 다르다.
    tagless = {}                                       # 태그없음 내역. 미수집과 오류는 대응이 다르다.

    # not_on_steam은 해석하지 않는다. "이름 매칭 실패"와 "애초에 Steam 게임이 아님"은
    # 스크립트에게 똑같이 보이지만 대응이 전혀 다르다. 분모에서도 뺀다.
    for name in not_on_steam:
        rows.append([name, "-", "-", "-", ST_NOT_TARGET, "seed not_on_steam", "-", "-", "-", "-"])

    for name in games:
        appid, route, reason, term = resolve(name, aliases, pinned)

        if appid is None:
            stats[ST_SEARCH_FAIL] += 1
            fail_reasons[reason] = fail_reasons.get(reason, 0) + 1
            rows.append([name, "-", term, "-", ST_SEARCH_FAIL, reason, "-", "-", "-", "-"])
            continue

        stats[route] += 1
        data = get_details(appid)
        time.sleep(1)                                  # 레이트 리밋 여유 (5분당 200회)

        if data is None:
            # 재시도 대상. 영구 제외인 '제외'와 절대 같은 칸에 넣지 않는다.
            stats[ST_DETAIL_FAIL] += 1
            rows.append([name, route, term, str(appid), ST_DETAIL_FAIL, "success=false",
                         "-", "-", "-", "-"])
            continue

        status, sreason = classify(data)
        stats[status] += 1

        genres = ", ".join(g["description"] for g in data.get("genres", [])) or "-"
        tags, spy = get_top_tags(appid)
        if spy == SPY_OK:
            stats["태그있음"] += 1
        else:
            stats["태그없음"] += 1
            tagless[spy] = tagless.get(spy, 0) + 1
        rows.append([
            name,
            route,
            term,
            str(appid),
            status,
            sreason,
            data.get("type", "-"),
            data.get("name", "-"),
            genres,
            tags,
        ])

    headers = ["입력한 이름", "해석", "검색어", "appid", "상태", "사유",
               "type", "Steam 이름", "genres", "tags(top5)"]
    widths = [max(len(str(r[i])) for r in ([headers] + rows)) for i in range(len(headers))]

    def line(cells):
        return " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells))

    print(line(headers))
    print("-+-".join("-" * w for w in widths))
    for r in rows:
        print(line(r))

    with open(OUTPUT_CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    print()
    print("CSV 저장: " + OUTPUT_CSV_PATH)

    summary = " / ".join(f"{k} {v}" for k, v in stats.items())
    print(f"[{summary}]")

    if fail_reasons:
        detail = " / ".join(f"{k} {v}" for k, v in sorted(fail_reasons.items()))
        print(f"검색실패 내역: {detail}")
    if tagless:
        detail = " / ".join(f"{k} {v}" for k, v in sorted(tagless.items()))
        print(f"태그없음 내역: {detail}  (미수집 = SteamSpy가 앱 자체를 추적하지 않음)")

    # 분모가 다른 비율은 따로 찍는다. 하나로 뭉치면 무엇이 나빠졌는지 못 본다.
    target = stats["해석대상"]
    resolved = stats[ROUTE_AUTO] + stats[ROUTE_ALIAS] + stats[ROUTE_PINNED]
    detail_ok = stats[ST_OK] + stats[ST_EXCLUDED] + stats[ST_SOFTWARE]
    if target:
        # 성과 숫자는 이쪽. 수작업(별칭·pinned)을 섞으면 거짓말이 된다.
        print(f"자동 해석률   {stats[ROUTE_AUTO]}/{target}"
              f" = {stats[ROUTE_AUTO] / target:.0%}  (이름 매칭 로직의 품질 — 수작업 제외)")
        print(f"최종 해석률   {resolved}/{target}"
              f" = {resolved / target:.0%}  (수작업 별칭 {stats[ROUTE_ALIAS]} + pinned {stats[ROUTE_PINNED]} 포함)")
    if detail_ok:
        print(f"태그 커버리지 {stats['태그있음']}/{detail_ok}"
              f" = {stats['태그있음'] / detail_ok:.0%}  (SteamSpy 데이터 상태)")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    try:
        main()
    except requests.RequestException as e:
        print(f"네트워크 오류: {e}", file=sys.stderr)
        sys.exit(1)
