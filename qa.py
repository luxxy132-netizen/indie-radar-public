"""
qa.py — /steam-qa: 수집 원본 raw/{날짜}.json 한 개를 V3 체크리스트로 점검하고, 통과하면 적재한다.

결정론적이다 — 같은 원본 · 같은 이전 원본 · 같은 profile.json · 같은 시드면 결과가 같다. API는 부르지 않는다.
판정 로직은 여기에만 있다. /steam-qa 스킬과 GitHub Actions가 둘 다 이 스크립트를 부른다(결과가 갈리지 않게).

실행:  python qa.py [raw/YYYY-MM-DD.json]
       인자가 없으면 raw/ 에서 파일명 날짜가 가장 최근인 수집분(어느 파일인지 stderr에 찍는다)

산출
  qa/{날짜}.json      점검 결과 전체
  qa/{날짜}.md        사람이 읽는 요약 — Actions는 실패 시 이 파일로 이슈를 연다. 점검이 죽어도 반드시 쓴다
                      (같은 원본을 다시 점검하면 -2, -3 … 을 붙인다. 덮어쓰지 않는다)
  data/{대상일}.json  적재 — 오늘 데이터에 관한 점검이 전부 통과할 때만 쓴다(아래). 이미 있으면 건드리지 않는다

상태
  PASS 통과 · FAIL 실패 · SKIP 점검 불가(이유를 반드시 적는다) · INFO 분류 결과(실패 아님)

적재 게이트 — "실패 시 해당 날짜 적재 롤백"
  FAIL은 **전부** 종료 코드 1(알림 · 이슈)이다. 하지만 오늘 적재를 막는 건 **오늘 데이터를 본 점검**뿐이다.
  추적(V3-3 · V3-4)과 시드 프로파일(V3-7)은 다른 날 게임이나 시드의 문제라서, 그 FAIL로 오늘 신작을 버리지
  않는다. 지난주 게임 하나가 비공개되면 7일치 적재가 막히던 문제(코드 리뷰 2026-09-10)의 수정이다.
"""

import datetime
import json
import os
import sys

import yaml

import collect
from build_profile import gate as profile_gate
from build_profile import seed_fingerprint
from peek import SEED_LIBRARY_PATH

QA_DIR = "qa"
DATA_DIR = "data"
PROFILE_PATH = "profile.json"
PASS, FAIL, SKIP, INFO = "PASS", "FAIL", "SKIP", "INFO"
# GetItems의 type: 0 게임 / 1 데모 / 6 소프트웨어 (2026-09-10 실측)
GAME_TYPE = 0
DETAIL_LIMIT = 30              # 상세 목록은 앞 30개만 남긴다 — 전체 건수는 details_total에 있다


def result(cid, title, status, summary, details=()):
    details = list(details)
    return {"id": cid, "title": title, "status": status, "summary": summary,
            "details": details[:DETAIL_LIMIT], "details_total": len(details)}


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --- 원본 읽기 -----------------------------------------------------------------

def items_of(batches):
    return {it.get("appid"): it for b in batches or [] for it in collect.page_items(b.get("body"))}


def raw_store_items(body):
    """page_items와 달리 dict가 아닌 원소도 그대로 돌려준다 — Q-4가 센다."""
    response = body.get("response") if isinstance(body, dict) else None
    items = response.get("store_items") if isinstance(response, dict) else None
    return items if isinstance(items, list) else []


def unique_in_order(doc):
    """목록 페이지 항목을 받은 순서대로, 겹친 구간은 한 번만."""
    seen = {}
    for p in doc.get("query_pages") or []:
        for it in collect.page_items(p.get("body")):
            seen.setdefault(it.get("appid"), it)
    return list(seen.values())


def target_list(doc):
    w0, w1 = doc["meta"]["window_unix"]
    return [it for it in unique_in_order(doc) if w0 <= collect.release_ts(it) < w1]


def target_games(target, items):
    """대상일 출시작 중 상세를 제대로 받은 게임(type 0)."""
    out = []
    for it in target:
        g = items.get(it.get("appid"))
        if g and g.get("success") == 1 and g.get("type") == GAME_TYPE:
            out.append(g)
    return out


def review_count(item):
    n = (((item or {}).get("reviews") or {}).get("summary_filtered") or {}).get("review_count")
    return n if isinstance(n, int) else None


def tag_names(doc):
    tags = (((doc.get("tag_list") or {}).get("response")) or {}).get("tags") or []
    return {t.get("tagid"): t.get("name") for t in tags if isinstance(t, dict)}


def label(g):
    return f"{g.get('appid')} {g.get('name', '')}".strip()


# --- 점검: 오늘 데이터 (FAIL이면 오늘 적재를 막는다) --------------------------------

def check_collection(doc):
    title = "수집 완결 — 대상일을 끝까지 받았나"
    m = doc["meta"]
    if "failed_calls" not in m:
        return result("C0", title, FAIL, "meta.failed_calls 없음 — 호출이 실패했는지 알 수 없다")
    # 페이지 겹침 도입 전 수집분(2026-09-10 첫 수집)엔 window_covered가 없다 — 중단 사유로 판단한다
    covered = m.get("window_covered", m.get("query_stop_reason") == collect.STOP_REACHED)
    tracked_failed = m.get("failed_calls_tracked", 0)
    today_failed = m["failed_calls"] - tracked_failed
    return result("C0", title, PASS if covered and not today_failed else FAIL,
                  f"중단 사유: {m.get('query_stop_reason')} · 끝내 실패한 호출 {m['failed_calls']}건"
                  f" (그중 추적분 {tracked_failed}건은 V3-3에서 본다)",
                  [f"{e.get('path')} 시도{e.get('attempt')}: {e.get('error')}" for e in m.get("errors", [])])


def check_requested(doc, items):
    requested = [a for b in doc.get("items_batches") or [] for a in b.get("appids", [])]
    missing = [a for a in requested if a not in items]
    return result("V1-1", "원본 대조 — 요청한 appid 대비 받은 항목", FAIL if missing else PASS,
                  f"요청 {len(requested)} · 받음 {len(requested) - len(missing)} · 없음 {len(missing)}", missing)


def check_success(target, items):
    bad = [f"{a} success={items[a].get('success')}" for a in (it.get("appid") for it in target)
           if a in items and items[a].get("success") != 1]
    return result("V3-1", "상세 조회 실패(success≠1) — 대상일 출시작", FAIL if bad else PASS,
                  f"대상일 {len(target)}개 중 {len(bad)}개", bad)


def check_price(games):
    def unpriced(g):
        bpo = g.get("best_purchase_option")
        # 가격 필드가 있어도 금액을 못 읽으면 결측이다 — price_krw가 None으로 적재된다
        return not bpo or to_int(bpo.get("final_price_in_cents")) is None
    missing = [label(g) for g in games if not g.get("is_free") and unpriced(g)]
    free = sum(1 for g in games if g.get("is_free"))
    return result("V3-6", "가격 결측 — 무료(is_free)는 결측 아님", FAIL if missing else PASS,
                  f"게임 {len(games)}개 · 무료 {free} · 결측 {len(missing)}", missing)


def check_tag_coverage(games, rules):
    threshold = rules["min_tag_coverage"]
    if not games:
        return result("V3-8", "신작 태그 커버리지", FAIL, "대상일 게임 0개 — 목록 소스 이상 의심")
    no_tags = [label(g) for g in games if not g.get("tags")]
    rate = (len(games) - len(no_tags)) / len(games)
    return result("V3-8", "신작 태그 커버리지 — 매칭 재료가 있나", FAIL if rate < threshold else PASS,
                  f"{len(games) - len(no_tags)}/{len(games)} = {rate:.0%} (기준 {threshold:.0%})", no_tags)


def check_order(doc):
    seq = [(it.get("appid"), collect.release_ts(it)) for it in unique_in_order(doc) if collect.release_ts(it)]
    bad = [f"#{i + 1} {a1}({t1}) < #{i + 2} {a2}({t2})"
           for i, ((a1, t1), (a2, t2)) in enumerate(zip(seq, seq[1:])) if t1 < t2]
    return result("Q-1", f"목록 순서 — sort={collect.SORT_RELEASE_DESC}가 아직 출시일 최신순인가",
                  FAIL if bad else PASS, f"출시일 있는 {len(seq)}개 중 내림차순 위반 {len(bad)}건", bad)


def check_overlap(doc):
    title = "겹친 구간 일치 — 수집 중 목록이 흔들렸나"
    pages = [(p.get("start", 0), collect.page_items(p.get("body"))) for p in doc.get("query_pages") or []]
    if len(pages) < 2:
        return result("Q-2", title, SKIP, f"페이지 {len(pages)}개 — 비교할 경계가 없다")
    compared, bad = 0, []
    for (s1, a), (s2, b) in zip(pages, pages[1:]):
        offset = s2 - s1
        if offset <= 0:
            compared += 1
            bad.append(f"start {s1}→{s2} 순서 이상")
            continue
        n = min(len(a) - offset, len(b))
        if n <= 0:
            continue
        compared += 1
        # 앞 페이지의 offset번째부터가 다음 페이지 첫 항목과 겹친다. 다음 페이지가 짧아도 같은 자리를 비교한다
        left = [x.get("appid") for x in a[offset:offset + n]]
        right = [x.get("appid") for x in b[:n]]
        if left != right:
            bad.append(f"start {s1}→{s2}: {left} ≠ {right}")
    if not compared:
        return result("Q-2", title, SKIP, "겹쳐 받지 않은 수집분 — 페이지 겹침 도입 전 형식")
    return result("Q-2", title, FAIL if bad else PASS, f"경계 {compared}곳 중 불일치 {len(bad)}곳", bad)


def check_tag_names(games, names):
    if not names:
        return result("Q-3", "태그 이름표", FAIL, "태그 이름표가 비었다 — 적재하면 태그가 전부 ?id가 된다")
    unknown = sorted({t.get("tagid") for g in games for t in g.get("tags") or [] if t.get("tagid") not in names})
    return result("Q-3", "태그 이름표에 없는 tagid", INFO, f"이름표 {len(names)}개 · 없는 tagid {len(unknown)}개", unknown)


def check_structure(doc, games):
    """다른 점검이 조용히 건너뛰는 것들 — 출시일 없는 목록 항목은 대상일 집계에서 소리 없이 빠진다."""
    problems = []
    no_date = [str(it.get("appid")) for it in unique_in_order(doc) if not collect.release_ts(it)]
    if no_date:
        problems.append(f"출시일 없는 목록 항목 {len(no_date)}개 — 어느 날 출시작인지 몰라 대상일 집계에서 빠진다: "
                        + ", ".join(no_date[:10]))
    non_dict = sum(1 for key in ("query_pages", "items_batches") for p in doc.get(key) or []
                   for x in raw_store_items(p.get("body")) if not isinstance(x, dict))
    if non_dict:
        problems.append(f"dict가 아닌 store_items 원소 {non_dict}개")
    no_reviews = [label(g) for g in games if review_count(g) is None]
    if no_reviews:
        problems.append(f"리뷰 수가 없는 대상일 게임 {len(no_reviews)}개: " + ", ".join(no_reviews[:10]))
    return result("Q-4", "원본 형식 — 출시일 · 원소 모양 · 리뷰 필드", FAIL if problems else PASS,
                  f"문제 {len(problems)}종", problems)


# --- 점검: 다른 날 · 시드 · 분류 (FAIL이어도 오늘 적재는 막지 않는다) -----------------

def check_crosscheck():
    return result("V1-2", "교차 대조 — Steam 공식 vs SteamSpy 동시접속", SKIP,
                  "동시접속 공식 API(GetNumberOfCurrentPlayers) 키 미발급. 신작은 SteamSpy도 미수집 스텁이라 "
                  "대조 상대가 없다(실패 9번)")


def check_types(target, items):
    non_game = [f"{label(items[a])} type={items[a].get('type')}" for a in (it.get("appid") for it in target)
                if a in items and items[a].get("success") == 1 and items[a].get("type") != GAME_TYPE]
    return result("V3-2", "게임 아님(type≠0) — 적재에서 제외", INFO,
                  f"{len(non_game)}개 제외 (0 게임 · 1 데모 · 6 소프트웨어)", non_game)


def check_tracked(doc, tracked_items):
    m = doc["meta"]
    if "tracked_batches" not in doc:
        return result("V3-3", "추적 appid 소실", SKIP, "추적 기능 도입 전 수집분 — 비교 대상을 받지 않았다")
    requested = [a for b in doc["tracked_batches"] for a in b.get("appids", [])]
    unreadable = [f"이전 원본 읽기 실패 {u.get('path')}: {u.get('error')}" for u in m.get("track_unreadable", [])]
    if not requested and not unreadable:
        return result("V3-3", "추적 appid 소실", SKIP,
                      f"추적할 이전 수집분 출시작 없음 (읽은 이전 수집분 {len(m.get('tracked_from', []))}개)")
    gone = [f"{a} 응답 없음" if a not in tracked_items else f"{a} success={tracked_items[a].get('success')}"
            for a in requested if (tracked_items.get(a) or {}).get("success") != 1]
    return result("V3-3", "추적 appid 소실 — 지난 7일 출시작이 오늘도 조회되나", FAIL if gone or unreadable else PASS,
                  f"추적 {len(requested)}개 중 소실 {len(gone)}개 · 읽지 못한 이전 원본 {len(unreadable)}개",
                  unreadable + gone)


def latest_reviews(prev_docs):
    """이전 수집분들에서 appid별 마지막으로 본 리뷰 수. prev_docs는 오래된 순 — 나중 것이 덮는다."""
    latest = {}
    for d in prev_docs:
        for it in list(items_of(d.get("items_batches")).values()) + list(items_of(d.get("tracked_batches")).values()):
            n = review_count(it)
            if n is not None:
                latest[it.get("appid")] = n
    return latest


def check_reviews(prev_docs, prev_unreadable, items, tracked_items):
    before = latest_reviews(prev_docs)
    now = {a: review_count(it) for a, it in {**items, **tracked_items}.items() if review_count(it) is not None}
    pairs = [(a, before[a], now[a]) for a in now if a in before]
    unreadable = [f"이전 원본 읽기 실패 {u['path']}: {u['error']}" for u in prev_unreadable]
    # 추적했는데 리뷰 수가 없으면 비교 자체가 안 된다 — SKIP("비교할 게 없다")으로 가리면 안 된다
    no_review = [f"{a} 리뷰 필드 없음" for a, it in tracked_items.items()
                 if it.get("success") == 1 and review_count(it) is None]
    if not pairs and not unreadable and not no_review:
        return result("V3-4", "리뷰 수 감소", SKIP, f"비교할 이전 리뷰 수 없음 (이전 수집분 {len(prev_docs)}개)")
    dropped = [f"{a}: {b} → {n}" for a, b, n in pairs if n < b]
    return result("V3-4", "리뷰 수 감소 — 단조증가 위반", FAIL if dropped or unreadable or no_review else PASS,
                  f"비교 {len(pairs)}개 중 감소 {len(dropped)}개 · 리뷰 필드 없음 {len(no_review)}개 · "
                  f"읽지 못한 이전 원본 {len(unreadable)}개", unreadable + no_review + dropped)


def check_ccu():
    return result("V3-5", "동시접속 전일 대비 3배 급변", SKIP,
                  "동시접속 공식 API(GetNumberOfCurrentPlayers) 키 미발급 — 받는 값이 없다")


def check_profile(profile, rules, fingerprint):
    title = "시드 프로파일 — 지금 시드로 만든 것인가 · 자동 해석률 · 태그 커버리지"
    if profile is None:
        return result("V3-7", title, SKIP, "profile.json 없음 — build_profile.py를 먼저 돌릴 것")
    if "_error" in profile:
        return result("V3-7", title, FAIL, f"profile.json을 읽지 못했다 — {profile['_error']}")
    problems = []
    if profile.get("seed_fingerprint") != fingerprint:
        problems.append("profile.json이 지금 seed_library.yaml로 만든 것이 아니다 — 시드나 규칙을 고친 뒤 "
                        "build_profile.py를 안 돌렸다")
    m = profile.get("metrics") or {}
    # 파일에 적힌 규칙이 아니라 **지금** 규칙으로 판정한다. 파일의 규칙은 만들 때 이미 통과한 규칙이라
    # 그걸로 다시 보면 절대 FAIL이 나지 않는다(코드 리뷰 2026-09-10).
    problems += profile_gate(m, rules)
    return result("V3-7", title, FAIL if problems else PASS,
                  f"자동 해석률 {m.get('auto_resolution', 0):.0%} · 태그 커버리지 {m.get('tag_coverage', 0):.0%}"
                  f" ({profile.get('generated_at')} 산출)", problems)


# --- 실행 ----------------------------------------------------------------------

def guarded(cid, title, blocks_load, fn, *args):
    """점검 하나. 예외가 나도 그 점검을 FAIL로 기록한다 — 요약이 안 써지면 Actions 이슈가 빈다."""
    try:
        r = fn(*args)
    except Exception as e:  # noqa: BLE001 — 어떤 예외든 FAIL 한 줄로 드러낸다
        r = result(cid, title, FAIL, f"점검 중 오류 — {type(e).__name__}: {e}")
    r["blocks_load"] = blocks_load
    return r


def run_checks(doc, prev_docs, prev_unreadable, profile, rules, fingerprint):
    try:
        items = items_of(doc.get("items_batches"))
        tracked_items = items_of(doc.get("tracked_batches"))
        target = target_list(doc)
        games = target_games(target, items)
    except Exception as e:  # noqa: BLE001
        r = result("C0", "원본 형식", FAIL, f"원본을 해석하지 못했다 — {type(e).__name__}: {e}")
        r["blocks_load"] = True
        return [r]
    today, other = True, False
    return [
        guarded("C0", "수집 완결", today, check_collection, doc),
        guarded("V1-1", "원본 대조", today, check_requested, doc, items),
        guarded("V1-2", "교차 대조", other, check_crosscheck),
        guarded("V3-1", "상세 조회 실패", today, check_success, target, items),
        guarded("V3-2", "게임 아님", other, check_types, target, items),
        guarded("V3-3", "추적 appid 소실", other, check_tracked, doc, tracked_items),
        guarded("V3-4", "리뷰 수 감소", other, check_reviews, prev_docs, prev_unreadable, items, tracked_items),
        guarded("V3-5", "동시접속 급변", other, check_ccu),
        guarded("V3-6", "가격 결측", today, check_price, games),
        guarded("V3-7", "시드 프로파일", other, check_profile, profile, rules, fingerprint),
        guarded("V3-8", "신작 태그 커버리지", today, check_tag_coverage, games, rules),
        guarded("Q-1", "목록 순서", today, check_order, doc),
        guarded("Q-2", "겹친 구간", today, check_overlap, doc),
        guarded("Q-3", "태그 이름표", today, check_tag_names, games, tag_names(doc)),
        guarded("Q-4", "원본 형식", today, check_structure, doc, games),
    ]


# --- 적재 ----------------------------------------------------------------------

def release_fields(release):
    """GetItems release 객체 → (첫 출시일 'YYYY-MM-DD' 또는 None, 얼리 액세스 졸업 여부).
    졸업일이 출시일보다 뒤면 '졸업 예정'이라 졸업으로 치지 않는다(When We Arrive: 출시 9/9, 졸업 11/13 — 2026-09-10 실측)."""
    release = release if isinstance(release, dict) else {}
    orig, grad, rel = (release.get(k) for k in ("original_steam_release_date", "release_from_early_access_date",
                                                  "steam_release_date"))
    original = (datetime.datetime.fromtimestamp(orig, collect.KST).strftime("%Y-%m-%d")
                if isinstance(orig, int) and orig else None)
    graduated = isinstance(grad, int) and grad > 0 and (not isinstance(rel, int) or grad <= rel + 86400)
    return original, graduated


def load_record(g, names):
    """적재 한 행. 가격 '센트'는 문자열로 온다("945000" = ₩9,450) — 정수로 바꾼다."""
    bpo = g.get("best_purchase_option") or {}
    rv = (g.get("reviews") or {}).get("summary_filtered") or {}
    ts = collect.release_ts(g)
    cents = to_int(bpo.get("final_price_in_cents"))
    original, graduated = release_fields(g.get("release"))
    return {
        "appid": g.get("appid"),
        "name": g.get("name"),
        "release_kst": datetime.datetime.fromtimestamp(ts, collect.KST).isoformat(timespec="minutes") if ts else None,
        "is_free": bool(g.get("is_free")),              # True 아니면 None으로 온다 — False로 맞춘다
        "is_early_access": bool(g.get("is_early_access")),
        "original_release_kst": original,               # 첫 출시일 — 얼리 액세스 졸업 · 재출시면 출시일보다 이르다
        "ea_graduated": graduated,                      # 주간 리포트가 "얼리 액세스 졸업"으로 표시한다
        "price": bpo.get("formatted_final_price"),
        "price_original": bpo.get("formatted_original_price"),   # 주간 리포트의 "할인 전 → 후" 표기용
        "price_krw": cents // 100 if cents is not None else None,
        "discount_pct": bpo.get("discount_pct") or 0,
        "tags": [names.get(t.get("tagid"), f"?{t.get('tagid')}")
                 for t in sorted(g.get("tags") or [], key=lambda t: -t.get("weight", 0))],
        "review_count": rv.get("review_count"),
        "percent_positive": rv.get("percent_positive"),
        "store_url": f"https://store.steampowered.com/{g['store_url_path']}" if g.get("store_url_path") else None,
    }


# --- 입출력 --------------------------------------------------------------------

def latest_raw(raw_dir=collect.RAW_DIR):
    names = sorted(f for f in os.listdir(raw_dir) if collect.RAW_NAME.match(f)) if os.path.isdir(raw_dir) else []
    if not names:
        sys.exit(f"{raw_dir}/ 에 수집 원본(YYYY-MM-DD.json)이 없다. collect.py 를 먼저 돌릴 것.")
    return os.path.join(raw_dir, names[-1])


def load_previous(doc, raw_dir):
    """이 수집분 직전 TRACK_DAYS일의 원본(오래된 순). 읽지 못한 건 (이름, 이유)로 돌려준다."""
    run_day = (doc.get("meta") or {}).get("run_day")
    if not run_day:
        return [], [{"path": "-", "error": "meta.run_day 없음 — 이전 수집분을 고를 수 없다"}]
    docs, unreadable = [], []
    for path in collect.previous_raw_files(run_day, raw_dir):
        try:
            with open(path, encoding="utf-8") as f:
                docs.append(json.load(f))
        except (OSError, ValueError) as e:
            unreadable.append({"path": os.path.basename(path), "error": f"{type(e).__name__}: {e}"})
    return docs, unreadable


def load_profile():
    if not os.path.exists(PROFILE_PATH):
        return None
    try:
        with open(PROFILE_PATH, encoding="utf-8") as f:
            profile = json.load(f)
        return profile if isinstance(profile, dict) else {"_error": "JSON 객체가 아니다"}
    except (OSError, ValueError) as e:
        return {"_error": f"{type(e).__name__}: {e}"}


def load_rules():
    with open(SEED_LIBRARY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)["profile_rules"]


def free_stem(directory, stem):
    """stem.json · stem.md 둘 다 없는 이름. 있으면 -2, -3 … — 점검 결과도 덮어쓰지 않는다."""
    n = 1
    while True:
        s = stem if n == 1 else f"{stem}-{n}"
        if not any(os.path.exists(os.path.join(directory, s + ext)) for ext in (".json", ".md")):
            return s
        n += 1


def cell(text):
    return str(text).replace("|", "／").replace("\n", " ")


def to_markdown(report):
    verdict = "❌ 실패" if report["failed"] else "✅ 통과"
    lines = [f"# /steam-qa {report['raw']} — {verdict}", "",
             f"수집 대상일 **{report['target_day']}** · 대상일 게임 {report['target_games']}개 · 적재: {report['load']}", "",
             "| 항목 | 상태 | 요약 |", "|---|---|---|"]
    mark = {PASS: "✅ PASS", FAIL: "❌ FAIL", SKIP: "⏭ SKIP", INFO: "ℹ INFO"}
    for c in report["checks"]:
        status = mark[c["status"]] + (" · 적재 차단" if c["status"] == FAIL and c.get("blocks_load") else "")
        lines.append(f"| {c['id']} {cell(c['title'])} | {status} | {cell(c['summary'])} |")
    for c in report["checks"]:
        if c["status"] == FAIL and c["details"]:
            more = f" (외 {c['details_total'] - len(c['details'])}건)" if c["details_total"] > len(c["details"]) else ""
            lines += ["", f"## {c['id']} 상세{more}", ""] + [f"- {cell(d)}" for d in c["details"]]
    return "\n".join(lines) + "\n"


def write_json(path, obj, mode="w"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def github_outputs(md_path, md, failed):
    """Actions에서 돌 때만: 다음 단계가 쓸 경로 · 결과, 그리고 실행 요약 화면."""
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"summary={md_path}\nfailed={len(failed)}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)


def read_raw(raw):
    """(doc, 오류). 못 읽어도 멈추지 않는다 — 점검 결과로 FAIL을 남겨야 이슈 본문이 생긴다."""
    try:
        with open(raw, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"{type(e).__name__}: {e}"
    if not isinstance(doc, dict) or not isinstance(doc.get("meta"), dict):
        return None, "JSON 최상위나 meta가 객체가 아니다"
    return doc, None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        raw = argv[0]
    else:
        raw = latest_raw()
        print(f"인자 없음 — 파일명 날짜가 가장 최근인 수집분을 점검한다: {raw}", file=sys.stderr)

    doc, read_error = read_raw(raw)
    if read_error:
        checks = [dict(result("C0", "원본 읽기", FAIL, f"{raw} 를 읽지 못했다 — {read_error}"), blocks_load=True)]
        doc = {"meta": {}}
    else:
        try:
            rules = load_rules()
        except (OSError, ValueError, KeyError, TypeError) as e:
            rules = {}
            print(f"seed_library.yaml 규칙을 읽지 못했다 — {type(e).__name__}: {e}", file=sys.stderr)
        try:
            fingerprint = seed_fingerprint()
        except (OSError, ValueError) as e:
            fingerprint = f"(시드를 읽지 못함: {type(e).__name__})"
        prev_docs, prev_unreadable = load_previous(doc, os.path.dirname(raw) or ".")
        checks = run_checks(doc, prev_docs, prev_unreadable, load_profile(), rules, fingerprint)

    failed = [c for c in checks if c["status"] == FAIL]
    blocking = [c for c in failed if c.get("blocks_load")]
    target_day = doc["meta"].get("target_day")
    games = []
    if not read_error and not blocking:
        try:
            games = target_games(target_list(doc), items_of(doc.get("items_batches")))
        except Exception:  # noqa: BLE001 — run_checks에서 이미 FAIL로 잡혔을 경로
            games = []
    stem = free_stem(QA_DIR, os.path.splitext(os.path.basename(raw))[0])
    qa_json, qa_md = os.path.join(QA_DIR, stem + ".json"), os.path.join(QA_DIR, stem + ".md")

    data_path = os.path.join(DATA_DIR, f"{target_day}.json")
    if blocking:
        load = f"안 함 — 오늘 데이터 점검 FAIL {len(blocking)}건({', '.join(c['id'] for c in blocking)}), 그날 적재 롤백"
    elif not target_day:
        load = "안 함 — meta.target_day 없음"
    elif os.path.exists(data_path):
        load = f"이미 있음 — {data_path.replace(os.sep, '/')} 를 건드리지 않았다"
    else:
        names = tag_names(doc)
        write_json(data_path, {
            "target_day": target_day,
            "source_raw": raw.replace("\\", "/"),
            "qa_report": qa_json.replace("\\", "/"),
            "loaded_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
            "games": [load_record(g, names) for g in games],
        }, mode="x")
        load = f"{data_path.replace(os.sep, '/')} (게임 {len(games)}개)"
    if failed and not blocking:
        load += f" · FAIL {len(failed)}건({', '.join(c['id'] for c in failed)})은 다른 날 게임 · 시드 문제라 오늘 적재를 막지 않는다"

    report = {
        "raw": raw.replace("\\", "/"),
        "checked_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
        "target_day": target_day,
        "target_games": len(games),
        "failed": len(failed),
        "blocking": len(blocking),
        "load": load,
        "checks": checks,
    }
    md = to_markdown(report)
    write_json(qa_json, report, mode="x")
    with open(qa_md, "x", encoding="utf-8") as f:
        f.write(md)
    github_outputs(qa_md, md, failed)
    print(md)
    print(f"저장: {qa_json} · {qa_md}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
