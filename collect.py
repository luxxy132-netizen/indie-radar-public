"""
collect.py — PART 4-2: 어제(KST) 출시된 게임의 원본 JSON을 raw/{실행일}.json 에 떨어뜨린다.

받아서 저장만 한다. 필터 · 중복 제거 · 검증은 하지 않는다(/steam-qa 몫).
섞으면 어디서 깨졌는지 못 찾는다 — HANDOFF "뒤집지 말 것".

실행:  python collect.py
필요:  pip install -r requirements.txt
API 키 필요 없음 (전부 api.steampowered.com 의 공개 Store 서비스, 2026-09-10 확인)

소스
  목록   IStoreQueryService/Query   sort=40(출시일 최신순). 페이지 전체가 어제 00:00(KST) 이전이 될 때까지 넘긴다
  상세   IStoreBrowseService/GetItems  50개씩 일괄. 공식 유저 태그(가중치) · 출시일 · type · 가격 · 리뷰 · categories
  태그표 IStoreService/GetTagList     tagid → 이름
  추적   지난 TRACK_DAYS일 수집분의 대상일 출시작을 GetItems로 다시 받는다 — V3-3(소실) · V3-4(리뷰 수 감소)용

원본 파일 구조
  meta            실행 정보 · 수집 대상일 · 중단 사유 · 대상일을 끝까지 덮었는지 · 추적 현황 · 호출 수 · 오류 목록
  query_pages     Query 응답을 페이지째로 그대로 (페이지끼리 10개씩 겹친다 — 아래 PAGE_STRIDE)
  items_batches   신작 GetItems 응답을 배치째로 그대로 (실패한 배치도 요청한 appid와 함께 남긴다)
  tag_list        GetTagList 응답 그대로
  tracked_batches 추적 GetItems 응답을 배치째로 그대로

종료 코드
  0  모든 호출 성공 + 대상일을 끝까지 덮음
  1  재시도 후에도 끝내 실패한 호출이 있거나(예산 소진 포함), 대상일을 끝까지 덮지 못함
     (빈 페이지 · 페이지 상한 · 호출 실패로 중단). 원본은 그래도 저장한다 — 무엇이 비었는지가 증거다.
     재시도로 복구된 오류는 meta.errors에 남기지만 종료 코드는 0이다. 매일 빨간 불이 켜지면 아무도 안 본다
  2  오늘자 원본이 이미 있음 — 아무것도 호출하지 않고 멈춘다(덮어쓰지 않는다).
     호출 뒤 저장 직전에 다른 실행이 먼저 썼다면, 받은 문서를 .conflict-{시각}.json 으로 옆에 남긴다
"""

import datetime
import json
import os
import re
import sys
import time

import requests

API = "https://api.steampowered.com"
QUERY_PATH = "IStoreQueryService/Query/v1/"
ITEMS_PATH = "IStoreBrowseService/GetItems/v1/"
TAGLIST_PATH = "IStoreService/GetTagList/v1/"

RAW_DIR = "raw"
RAW_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json$")    # 수집 원본만. conflict · seed_* 파일은 안 걸린다
KST = datetime.timezone(datetime.timedelta(hours=9))
CONTEXT = {"language": "english", "country_code": "KR"}

# sort 40 = 출시일 최신순. 문서에 없는 값이다 — 2026-09-10에 0~40을 전부 찔러 40만 내림차순이었다.
# 의미가 예고 없이 바뀔 수 있으므로 원본에 순서를 그대로 남기고, /steam-qa가 내림차순 위반을 센다.
SORT_RELEASE_DESC = 40
PAGE_SIZE = 50
# 50개씩 받되 40개씩만 전진한다 — 페이지끼리 10개가 겹친다. 오프셋 페이징은 수집 도중 앞쪽 항목이
# 빠지면(비공개 전환 · 출시일 수정) 경계 항목 하나를 건너뛴다. 겹친 구간이 그 자리를 덮고, QA는 겹친
# 구간이 두 페이지에서 같은지 대조해 목록이 흔들렸는지 셀 수 있다. 비용은 페이지 하나 안팎.
# (동점 출시 시각의 순서 흔들림도 같은 식으로 덮이지만, 2026-09-10 수집분 200개엔 동점이 0건이었다.)
PAGE_STRIDE = 40
ITEMS_BATCH = 50              # GetItems 한 번에 50개까지 실측 확인 (URL 약 1,800자)
MAX_QUERY_PAGES = 12          # 하루 게임 출시 약 100~150개. 480개 범위에서 끊는다
TRACK_DAYS = 7                # 주간 리포트가 보는 기간. 하루 약 100개 × 7일 → GetItems 약 15회
MAX_RETRIES = 2               # 실패한 호출 하나당 재시도 횟수
MAX_CALLS = 70                # 한 실행 전체 호출 예산(재시도 포함). 정상이면 신작 10회 + 추적 15회 안팎
SLEEP_SEC = 1.1
TIMEOUT_SEC = 30

STOP_REACHED = "어제 이전 출시작 도달"   # 이 사유로 멈춰야만 대상일을 끝까지 덮은 것이다


class CallBudget:
    """한 실행에서 쓸 수 있는 호출 수. 다 쓰면 더 부르지 않는다."""

    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def take(self):
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


def call_api(path, params, expect, budget, errors, get=requests.get, sleep=time.sleep):
    """논리적 호출 하나. response.{expect} 가 리스트로 와야 성공이다.
    HTTP 200이라도 모양이 다르면 실패로 기록하고 재시도한다 — Steam은 잘못된 요청에 {"response": {}}를 준다.
    끝내 실패하면 None. 재시도는 예산 안에서만."""
    for attempt in range(1 + MAX_RETRIES):
        if not budget.take():
            errors.append({"path": path, "attempt": attempt, "error": "호출 예산 소진"})
            return None
        sleep(SLEEP_SEC)
        try:
            r = get(f"{API}/{path}", params=params, timeout=TIMEOUT_SEC)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            errors.append({"path": path, "attempt": attempt, "error": f"{type(e).__name__}: {e}"})
            continue
        response = body.get("response") if isinstance(body, dict) else None
        if isinstance(response, dict) and isinstance(response.get(expect), list):
            return body
        errors.append({"path": path, "attempt": attempt,
                       "error": f"응답 형식 이상: response.{expect} 없음 ({str(body)[:80]})"})
    return None


def target_window(now_kst):
    """어제(KST) 00:00 ~ 오늘 00:00. 수집이 한국 시간 08:00에 도므로 날짜 경계도 KST로 잡는다."""
    today = now_kst.astimezone(KST).date()
    start = datetime.datetime.combine(today - datetime.timedelta(days=1), datetime.time(), KST)
    end = start + datetime.timedelta(days=1)
    return start, end


def release_ts(item):
    release = item.get("release")
    ts = release.get("steam_release_date") if isinstance(release, dict) else None
    return ts if isinstance(ts, int) else 0


def page_items(body):
    """응답의 store_items 중 dict만. 이상한 원소는 원본에 그대로 남아 QA가 센다."""
    items = ((body or {}).get("response") or {}).get("store_items") or []
    return [it for it in items if isinstance(it, dict)]


def fetch_release_pages(window_start_ts, budget, errors, **io):
    """페이지 전체가 어제 00:00(KST) 이전이 될 때까지 넘긴다. (pages, 중단 사유, 호출 실패 여부)"""
    pages = []
    for n in range(MAX_QUERY_PAGES):
        start = n * PAGE_STRIDE
        payload = {
            "query": {
                "start": start,
                "count": PAGE_SIZE,
                "sort": SORT_RELEASE_DESC,
                "filters": {"released_only": True, "type_filters": {"include_games": True}},
            },
            "context": CONTEXT,
            "data_request": {"include_release": True},
        }
        body = call_api(QUERY_PATH, {"input_json": json.dumps(payload)}, "store_items", budget, errors, **io)
        if body is None:
            return pages, f"{n + 1}번째 페이지 호출 실패", True
        pages.append({"start": start, "body": body})

        items = page_items(body)
        if not items:
            return pages, "빈 페이지", False
        # 페이지 **전체**가 어제 이전일 때만 멈춘다. 순서가 어긋난 항목 하나에 멈추면 그 뒤의
        # 어제 출시작은 받지도 못해서 QA조차 셀 수 없다. 출시일이 없는 항목은 판단에서 뺀다.
        known = [release_ts(it) for it in items if release_ts(it)]
        if known and max(known) < window_start_ts:
            return pages, STOP_REACHED, False
    return pages, f"페이지 상한 {MAX_QUERY_PAGES} 도달", False


def fetch_items(appids, budget, errors, **io):
    """GetItems를 50개씩 일괄 호출. 실패한 배치도 어떤 appid를 요청했는지와 함께 남긴다."""
    batches = []
    for i in range(0, len(appids), ITEMS_BATCH):
        chunk = appids[i:i + ITEMS_BATCH]
        payload = {
            "ids": [{"appid": a} for a in chunk],
            "context": CONTEXT,
            "data_request": {
                "include_basic_info": True,
                "include_release": True,
                "include_tag_count": 20,
                "include_reviews": True,
            },
        }
        body = call_api(ITEMS_PATH, {"input_json": json.dumps(payload)}, "store_items", budget, errors, **io)
        batches.append({"appids": chunk, "body": body})
    return batches


def previous_raw_files(run_day, raw_dir=RAW_DIR, days=TRACK_DAYS):
    """run_day 직전 days일 안의 raw/{날짜}.json (오래된 순). 이름 규칙에 안 맞는 파일은 보지 않는다."""
    if not os.path.isdir(raw_dir):
        return []
    run = datetime.date.fromisoformat(run_day)
    found = []
    for name in os.listdir(raw_dir):
        m = RAW_NAME.match(name)
        if not m:
            continue
        try:
            day = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if run - datetime.timedelta(days=days) <= day < run:
            found.append(os.path.join(raw_dir, name))
    return sorted(found)


def target_day_appids(doc):
    """수집분 하나의 대상일 출시작 appid — 목록 페이지 중 그 수집분의 window 안에 드는 것."""
    w0, w1 = doc["meta"]["window_unix"]
    return [it["appid"] for p in doc.get("query_pages") or [] for it in page_items(p.get("body"))
            if "appid" in it and w0 <= release_ts(it) < w1]


def trackable_appids(doc):
    """추적할 appid — 그 수집분의 대상일 출시작 중 그날 상세를 제대로 받은 게임(success 1 · type 0)만.
    그날부터 조회가 안 되던 appid까지 추적하면 그 한 개가 이후 7일 내내 V3-3을 실패시킨다(코드 리뷰 2026-09-10)."""
    ok = {it.get("appid") for b in doc.get("items_batches") or [] for it in page_items(b.get("body"))
          if it.get("success") == 1 and it.get("type") == 0}
    return [a for a in target_day_appids(doc) if a in ok]


def tracked_appids(run_day, exclude, raw_dir=RAW_DIR):
    """지난 TRACK_DAYS일 수집분의 대상일 출시작(그날 제대로 조회된 게임만). 오늘 목록에 이미 있는 appid는 뺀다.
    읽지 못한 이전 원본은 건너뛰되 (경로, 이유)를 돌려준다 — 추적 대상이 조용히 줄지 않게."""
    ids, sources, unreadable = [], [], []
    for path in previous_raw_files(run_day, raw_dir):
        try:
            with open(path, encoding="utf-8") as f:
                found = trackable_appids(json.load(f))
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            unreadable.append({"path": os.path.basename(path), "error": f"{type(e).__name__}: {e}"})
            continue
        ids += found
        sources.append(os.path.basename(path))
    return [a for a in dict.fromkeys(ids) if a not in exclude], sources, unreadable


def collect(now_kst, budget_limit=MAX_CALLS, get=requests.get, sleep=time.sleep, raw_dir=RAW_DIR):
    """한 번의 수집. 파일은 쓰지 않고 원본 문서만 만든다(테스트 가능하도록 IO 분리).
    이전 수집분은 추적 대상을 고르려고 읽기만 한다."""
    io = {"get": get, "sleep": sleep}
    budget = CallBudget(budget_limit)
    errors = []
    run_day = f"{now_kst.astimezone(KST).date():%Y-%m-%d}"
    win_start, win_end = target_window(now_kst)

    pages, stop_reason, query_failed = fetch_release_pages(int(win_start.timestamp()), budget, errors, **io)

    # 요청 목록만 순서 유지하며 겹침을 없앤다. 페이지를 겹쳐 받으니 같은 appid가 두 번 나온다.
    # 이건 데이터 중복 제거가 아니다 — query_pages 원본에는 겹친 그대로 남아 QA가 대조할 수 있다.
    appids = list(dict.fromkeys(it["appid"] for p in pages for it in page_items(p["body"]) if "appid" in it))

    batches = fetch_items(appids, budget, errors, **io)
    tag_list = call_api(TAGLIST_PATH, {"language": "english"}, "tags", budget, errors, **io)

    # 추적은 맨 뒤다 — 예산이 모자라면 신작 · 태그표보다 추적이 먼저 굶는다.
    tracked, tracked_from, unreadable = tracked_appids(run_day, set(appids), raw_dir)
    tracked_batches = fetch_items(tracked, budget, errors, **io)

    received_ids = {it.get("appid") for b in batches for it in page_items(b["body"])}
    tracked_ids = {it.get("appid") for b in tracked_batches for it in page_items(b["body"])}

    # 끝내 실패한 논리적 호출 수. errors는 재시도까지 전부 담고, 이건 데이터가 실제로 빈 곳만 센다.
    failed_calls = (query_failed
                    + sum(1 for b in batches + tracked_batches if b["body"] is None)
                    + (tag_list is None))

    return {
        "meta": {
            "collected_at": now_kst.astimezone(KST).isoformat(timespec="seconds"),
            "run_day": run_day,
            "target_day": f"{win_start.date():%Y-%m-%d}",
            "window_kst": [win_start.isoformat(), win_end.isoformat()],
            "window_unix": [int(win_start.timestamp()), int(win_end.timestamp())],
            "source": {
                "list": f"{QUERY_PATH} sort={SORT_RELEASE_DESC} (count {PAGE_SIZE}, stride {PAGE_STRIDE})",
                "details": ITEMS_PATH,
                "tags": TAGLIST_PATH,
            },
            "query_stop_reason": stop_reason,
            "window_covered": stop_reason == STOP_REACHED,
            "query_pages": len(pages),
            "requested_appids": len(appids),
            # 판정은 QA 몫이지만, 요청 대비 무엇이 빠졌는지는 여기서 센다 — 원본만 봐선 요청 목록을 되짚기 번거롭다.
            "items_received": len(received_ids),
            "items_missing": len(set(appids) - received_ids),
            "items_not_success": sum(1 for b in batches for it in page_items(b["body"]) if it.get("success") != 1),
            "tracked_from": tracked_from,
            "tracked_appids": len(tracked),
            "tracked_missing": len(set(tracked) - tracked_ids),
            "track_unreadable": unreadable,
            "calls_used": budget.used,
            "call_budget": budget.limit,
            "failed_calls": failed_calls,
            # 그중 추적분. 오늘 적재를 막을지는 이걸 뺀 나머지로 판단한다 — 추적은 다른 날 게임이다(qa C0)
            "failed_calls_tracked": sum(1 for b in tracked_batches if b["body"] is None),
            "errors": errors,
        },
        "query_pages": pages,
        "items_batches": batches,
        "tag_list": tag_list,
        "tracked_batches": tracked_batches,
    }


def exit_code(meta):
    return 1 if meta["failed_calls"] or not meta["window_covered"] else 0


def raw_path(name):
    return os.path.join(RAW_DIR, f"{name}.json")


def write_raw(doc, path):
    """'x' 모드 — 이미 있으면 FileExistsError. 같은 날 두 번 돌아도 조용히 덮지 않는다."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)


def summarize(doc, path):
    m = doc["meta"]
    failed_batches = sum(1 for b in doc["items_batches"] if b["body"] is None)
    tags = len(((doc["tag_list"] or {}).get("response") or {}).get("tags") or [])
    covered = "예" if m["window_covered"] else "아니오 ⚠️"
    print(f"수집 대상일 {m['target_day']} (KST)  |  실행일 {m['run_day']}")
    print(f"Query 페이지 {m['query_pages']} (중단 사유: {m['query_stop_reason']}, 대상일 끝까지 덮음: {covered})"
          f" → 요청 appid {m['requested_appids']}")
    print(f"GetItems 배치 {len(doc['items_batches'])} (실패 {failed_batches}) → 받은 항목 {m['items_received']}"
          f" · 요청했는데 없음 {m['items_missing']} · success≠1 {m['items_not_success']}")
    print(f"태그 이름표 {tags}개")
    print(f"추적 {m['tracked_appids']}개 (이전 수집분 {len(m['tracked_from'])}개에서) → 없음 {m['tracked_missing']}")
    for u in m["track_unreadable"]:
        print(f"  ! 이전 원본 읽기 실패 {u['path']}: {u['error']}")
    print(f"호출 {m['calls_used']}/{m['call_budget']}, 끝내 실패 {m['failed_calls']}건 "
          f"(재시도 포함 오류 기록 {len(m['errors'])}건)")
    for e in m["errors"]:
        print(f"  ! {e['path']} 시도{e['attempt']}: {e['error']}")
    print(f"저장: {path}")
    print("※ 건수만 센 것이다. 검증(어제 출시분 추리기 · 결측 · 순서 위반)은 /steam-qa 몫.")


def main():
    now = datetime.datetime.now(KST)
    day = f"{now.date():%Y-%m-%d}"
    path = raw_path(day)
    if os.path.exists(path):
        # 호출하기 전에 멈춘다. 다시 받으려면 기존 파일을 옮길 것 — 날짜별 원본은 덮어쓰지 않는다.
        print(f"{path} 가 이미 있다. 다시 받으려면 기존 파일을 다른 이름으로 옮길 것.", file=sys.stderr)
        return 2

    doc = collect(now)
    try:
        write_raw(doc, path)
    except FileExistsError:
        # 존재 확인과 저장 사이에 다른 실행이 먼저 썼다. 받은 문서를 버리지 않고 옆에 남긴다.
        conflict = raw_path(f"{day}.conflict-{now:%H%M%S}")
        write_raw(doc, conflict)
        summarize(doc, conflict)
        print(f"{path} 를 다른 실행이 먼저 썼다. 이번 결과는 {conflict} 에 남겼다.", file=sys.stderr)
        return 2
    summarize(doc, path)
    return exit_code(doc["meta"])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
