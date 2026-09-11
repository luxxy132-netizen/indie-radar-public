"""
profile_probe.py — 다음 작업 ④: 태그 프로파일 규칙을 실측으로 정한다.

peek 산출물에서 상태=정상인 게임만 골라 SteamSpy 태그 전체(득표 포함)를 받고,
  - 게임당 태그를 몇 개까지 볼지 (top N)
  - 몇 개 게임 이상에서 나와야 취향으로 인정할지 (min_tag_count)
조합별로 살아남는 태그 수를 표로 찍은 뒤, seed_library.yaml의 profile_rules
(tags_per_game / min_tag_count / tag_stoplist)로 확정 프로파일을 찍는다.

실행:  python profile_probe.py [peek_results_*.csv]
       인자를 안 주면 가장 최근에 수정된 peek_results_*.csv 를 쓴다.
원본:  raw/seed_tags_{날짜}.json 에 SteamSpy 응답을 가공 없이 보존한다.
       같은 날 파일이 있으면 API를 다시 부르지 않고 그 파일을 쓴다(규칙만 바꿔 재실험 가능).
"""

import csv
import datetime
import glob
import json
import os
import sys
import time
from collections import Counter

import yaml

from peek import SEED_LIBRARY_PATH, SPY_OK, ST_OK, ST_SOFTWARE, fetch_spy, spy_status

RAW_DIR = "raw"
TOP_N_OPTIONS = (5, 10, None)          # None = SteamSpy가 준 태그 전부
MIN_COUNT_OPTIONS = range(2, 9)
SPY_SLEEP_SEC = 1.0                    # SteamSpy appdetails는 초당 1회 권장
REQUIRED_COLUMNS = ("appid", "상태")   # 2026-09-10 이후 peek 산출물 형식


def latest_peek_csv():
    files = glob.glob("peek_results_*.csv")
    if not files:
        sys.exit("peek_results_*.csv 가 없다. peek.py 를 먼저 돌릴 것.")
    # 파일명 정렬은 쓰면 안 된다: '-2.csv' 가 '.csv' 보다 사전순으로 앞선다.
    return max(files, key=os.path.getmtime)


def load_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            sys.exit(f"{path} 에 {missing} 칸이 없다 — 2026-09-10 이전 형식의 peek 산출물이다. "
                     f"새 형식 파일을 인자로 직접 지정할 것.")
        return list(reader)


def load_rules():
    with open(SEED_LIBRARY_PATH, encoding="utf-8") as f:
        rules = yaml.safe_load(f)["profile_rules"]
    return rules["tags_per_game"], rules["min_tag_count"], list(rules.get("tag_stoplist") or [])


def load_or_fetch_raw(appids):
    """오늘자 원본이 있으면 그대로 쓰고, 없으면 받아서 저장한다."""
    os.makedirs(RAW_DIR, exist_ok=True)
    raw_path = os.path.join(RAW_DIR, f"seed_tags_{datetime.date.today():%Y-%m-%d}.json")

    if os.path.exists(raw_path):
        with open(raw_path, encoding="utf-8") as f:
            raw = json.load(f)
        missing = [a for a in appids if a not in raw]
        if missing:
            sys.exit(f"{raw_path} 에 없는 appid {len(missing)}건: {missing[:5]} — "
                     f"시드가 바뀌었으면 파일을 옮기고 다시 받을 것(덮어쓰지 않는다).")
        print(f"원본 재사용: {raw_path}")
        return raw, raw_path

    raw = {}
    for i, appid in enumerate(appids, 1):
        raw[appid] = fetch_spy(appid)
        print(f"  SteamSpy {i}/{len(appids)} {appid}", end="\r")
        time.sleep(SPY_SLEEP_SEC)
    print()
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=1)
    print(f"원본 저장: {raw_path}")
    return raw, raw_path


def sorted_tags(spy_response):
    """득표순 태그 이름 목록. 태그가 없거나 스텁·에러면 빈 리스트."""
    if spy_status(spy_response) != SPY_OK:
        return []
    tags = spy_response["tags"]
    return [name for name, _v in sorted(tags.items(), key=lambda kv: kv[1], reverse=True)]


def count_tags(tag_lists, top_n):
    """태그별로 '몇 개 게임에서 나왔는지'를 센다. 득표수가 아니라 게임 수."""
    counter = Counter()
    for tags in tag_lists:
        counter.update(tags[:top_n] if top_n else tags)
    return counter


def surviving(counter, min_count):
    return sorted(((t, c) for t, c in counter.items() if c >= min_count),
                  key=lambda tc: (-tc[1], tc[0]))


def label(top_n):
    return f"top{top_n}" if top_n else "전부"


def print_matrix(tag_lists):
    print("\n[살아남는 태그 수 — 제외목록 적용 전]  행=min_tag_count, 열=게임당 태그를 몇 개까지 보나")
    print("min  | " + " | ".join(f"{label(n):>5}" for n in TOP_N_OPTIONS))
    counters = {n: count_tags(tag_lists, n) for n in TOP_N_OPTIONS}
    for m in MIN_COUNT_OPTIONS:
        cells = " | ".join(f"{len(surviving(counters[n], m)):>5}" for n in TOP_N_OPTIONS)
        print(f"{m:>4} | {cells}")


def print_tag_list(title, items):
    print(f"\n{title} — {len(items)}개")
    print("  " + ", ".join(f"{t}({c})" for t, c in items))


def main():
    peek_path = sys.argv[1] if len(sys.argv) > 1 else latest_peek_csv()
    top_n, min_count, stoplist = load_rules()

    rows = load_rows(peek_path)
    ok_rows = [r for r in rows if r["상태"] == ST_OK]
    sw_rows = [r for r in rows if r["상태"] == ST_SOFTWARE]
    print(f"입력: {peek_path}  (정상 {len(ok_rows)} / 소프트웨어 {len(sw_rows)})")

    appids = [r["appid"] for r in ok_rows + sw_rows]
    raw, raw_path = load_or_fetch_raw(appids)

    # 태그가 없는 이유를 뭉치지 않는다. 미수집(SteamSpy가 앱을 모름)과 오류는 대응이 다르다.
    ok_status = Counter(spy_status(raw[r["appid"]]) for r in ok_rows)
    ok_tags = [sorted_tags(raw[r["appid"]]) for r in ok_rows]
    tagless = ", ".join(f"{k} {v}" for k, v in sorted(ok_status.items()) if k != SPY_OK)
    # 프로파일이 몇 개 게임을 반영한 건지가 곧 실패 5번의 감시 지표다.
    print(f"프로파일 반영 게임: {ok_status[SPY_OK]}/{len(ok_rows)}  (태그 없음: {tagless or '0'})")
    tag_len = [len(t) for t in ok_tags if t]
    if tag_len:
        print(f"게임당 태그 수: 최소 {min(tag_len)} / 최대 {max(tag_len)}")

    print_matrix(ok_tags)
    print_tag_list(f"[제외목록 적용 전] top{top_n} · min {min_count}",
                   surviving(count_tags(ok_tags, top_n), min_count))

    # 제외목록 오타 감시 — 데이터에 한 번도 안 나오는 이름은 조용히 아무것도 안 뺀다.
    seen = {t for tags in ok_tags for t in tags}
    unknown = [t for t in stoplist if t not in seen]
    if unknown:
        print(f"\n경고: 제외목록 중 데이터에 없는 태그 {unknown} — 오타인지 확인할 것")

    # 확정 규칙: 제외목록을 먼저 빼고 top N을 뽑는다.
    # 거꾸로 하면 제외 태그가 top N 자리를 차지해 그 게임의 취향 태그가 줄어든다.
    stop = set(stoplist)
    filtered = [[t for t in tags if t not in stop] for tags in ok_tags]
    profile = surviving(count_tags(filtered, top_n), min_count)
    print_tag_list(f"[확정 규칙] 제외목록 {len(stoplist)}개 → top{top_n} · min {min_count} = 프로파일",
                   profile)
    cut_first = [[t for t in tags[:top_n] if t not in stop] for tags in ok_tags]
    print(f"  (비교: top{top_n}을 먼저 뽑고 제외목록을 빼면 "
          f"{len(surviving(count_tags(cut_first, None), min_count))}개)")

    # 실패 6번의 영향 — 소프트웨어를 칸으로 안 뺐다면 프로파일이 어떻게 달라졌을지.
    sw_tags = [sorted_tags(raw[r["appid"]]) for r in sw_rows]
    for n in (top_n, None):
        before = dict(surviving(count_tags(ok_tags, n), min_count))
        after = dict(surviving(count_tags(ok_tags + sw_tags, n), min_count))
        added = sorted(set(after) - set(before))
        print(f"\n[소프트웨어 {len(sw_rows)}개를 섞었다면, min={min_count}, {label(n)}] "
              f"{len(before)} → {len(after)}개, 새로 들어오는 태그: {added or '없음'}")

    print(f"\n원본: {raw_path}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    main()
