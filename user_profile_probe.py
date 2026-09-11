"""
user_profile_probe.py — 구독자 취향 기준값(seed_library.yaml user_profile_rules) 재기. 파일은 쓰지 않는다(출력만).

profile_path(지금 취향)와 steam_profile이 둘 다 있는 사람(운영자)으로 돌린다. 스팀 라이브러리로 만든 취향이
지금 취향(직접 고른 게임 54개 → 태그 29개)과 얼마나 비슷한지, 이번 주 추천이 얼마나 겹치는지를 조합별로 보여준다.
좋아하는 게임 링크만 쓰는 사람도 흉내 낸다 — 지금 취향의 시드 게임에서 몇 개만 골라 만들어 본다(API 안 부름).

키(STEAM_API_KEY)는 GitHub Secrets에만 있어서 measure.yml(수동 실행)에서 돈다. 로컬에선 키가 없으면 멈춘다.

실행:  python user_profile_probe.py --only ID
"""

import argparse
import datetime
import json
import os
import random
import sys
import time

import requests

import build_user_profiles as bup
import collect
import report
from build_profile import GAME_TYPE, compute_profile, items_by_appid, official_tags

TOP_NS = (10, 20, 30, 50)
MIN_PLAYTIMES = (0, 60, 300)          # 분
MIN_COUNTS = (2, 3)
FAV_SIZES = (3, 5, 8, 15)
FAV_TRIALS = 20
PROBE_CALL_BUDGET = 20


def out(lines, path=None):
    text = "\n".join(lines) + "\n"
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")


def week_picks(games, tags, stoplist, match_rules):
    passed, _ = report.score_week(games, tags, stoplist, match_rules)
    return [p["game"]["appid"] for p in passed[:match_rules["weekly_max"]]], len(passed)


def main(argv=None):
    ap = argparse.ArgumentParser(description="구독자 취향 기준값 재기")
    ap.add_argument("--only", required=True, help="잴 사람 id")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    sub = next((s for s in bup.load_subscribers() if s["id"] == args.only), None)
    if not sub or not sub.get("steam_profile") or not sub.get("profile_path"):
        sys.exit(f"{args.only}: 명단에 없거나 steam_profile · profile_path가 둘 다 있지 않다(비교할 기준이 없다)")
    key = os.environ.get("STEAM_API_KEY", "").strip()
    if not key:
        sys.exit("STEAM_API_KEY가 없다 — measure.yml(GitHub Actions)에서 돌릴 것")

    rules, stoplist, per_game = bup.load_user_rules()
    match_rules, match_stop = report.load_rules()
    with open(sub["profile_path"], encoding="utf-8") as f:
        base = json.load(f)
    base_tags = [t["name"] for t in base["tags"]]

    names, problem = bup.fetch_tag_names(requests.get, time.sleep)
    steamid, problem = (None, problem) if problem else bup.resolve_steamid(sub["steam_profile"], key)
    library, problem = (None, problem) if problem else bup.owned_games(steamid, key)
    if problem:
        sys.exit(problem)

    wide = {**rules, "top_n": max(TOP_NS), "min_playtime_min": 0}
    cands = bup.candidate_appids(library, wide)
    errors = []
    batches = collect.fetch_items(cands, collect.CallBudget(PROBE_CALL_BUDGET), errors)
    if any(b["body"] is None for b in batches):
        sys.exit("상점 정보(GetItems) 호출 실패 — 다시 돌릴 것")
    items = items_by_appid({"items_batches": batches})
    minutes = {g["appid"]: g.get("playtime_forever") or 0 for g in library}
    lib_names = {g["appid"]: g.get("name") or "" for g in library}

    # --- 라이브러리 모양 ---
    top30 = cands[:30]
    lines = [f"# 취향 기준값 재기 — {args.only}", "",
             f"보유 {len(library)}개 · 60분 이상 {sum(1 for m in minutes.values() if m >= 60)}개 · "
             f"300분 이상 {sum(1 for m in minutes.values() if m >= 300)}개", "",
             "## 플레이 시간 상위 30 (게임이 아닌 것 표시)", "", "| # | 이름 | 시간 | 종류 |", "|---|---|---|---|"]
    for i, a in enumerate(top30, 1):
        it = items.get(a)
        kind = "게임" if bup.is_game(it) else ("상점 정보 없음" if it is None else f"게임 아님(type {it.get('type')})")
        lines.append(f"| {i} | {lib_names.get(a)} | {minutes[a] / 60:.0f}h | {kind} |")
    out(lines)

    # --- 조합별 ---
    days = report.target_days(datetime.datetime.now(collect.KST).date())
    games, loaded, *_ = report.load_week(days)
    base_picks, base_passed = week_picks(games, base_tags, match_stop, match_rules)
    lines = [f"## 스팀 라이브러리 취향 vs 지금 취향(태그 {len(base_tags)}개)", "",
             f"이번 주 비교 구간 {days[0]:%m/%d}~{days[-1]:%m/%d} · 적재 {len(loaded)}일 · 신작 {len(games)}개 · "
             f"지금 취향으로 기준 통과 {base_passed}개 → 추천 {len(base_picks)}개", "",
             "| 최소 시간 | top_n | min_tag_count | 시드 | 태그 수 | 지금 취향과 겹침 | 기준 통과 | 추천 겹침 |",
             "|---|---|---|---|---|---|---|---|"]
    details = {}
    for mp in MIN_PLAYTIMES:
        for n in TOP_NS:
            r = {**rules, "top_n": n, "min_playtime_min": mp}
            seeds, _ = bup.pick_seeds(bup.candidate_appids(library, r), [], items, r)
            with_tags = {a: t for a in seeds if (t := official_tags(items[a], names))}
            for mc in MIN_COUNTS:
                prof, _ = compute_profile(with_tags, per_game, mc, stoplist)
                tags = [t for t, _ in prof]
                picks, passed = week_picks(games, tags, match_stop, match_rules)
                lines.append(f"| {mp}분 | {n} | {mc} | {len(with_tags)} | {len(tags)} | "
                             f"{len(set(tags) & set(base_tags))}/{len(base_tags)} | {passed} | "
                             f"{len(set(picks) & set(base_picks))}/{len(base_picks)} |")
                details[(mp, n, mc)] = tags
    out(lines)

    seeds, _ = bup.pick_seeds(bup.candidate_appids(library, rules), [], items, rules)
    seeded = sum(1 for a in seeds if official_tags(items[a], names))
    now = (rules["min_playtime_min"], rules["top_n"], bup.min_tag_count_for(seeded, rules))
    if now in details:
        tags = details[now]
        out([f"## 지금 규칙(최소 {now[0]}분 · top {now[1]} · 시드 {seeded}개 → min {now[2]})의 태그", "",
             "- 스팀 취향: " + ", ".join(tags),
             "- 지금 취향에만 있음: " + (", ".join(sorted(set(base_tags) - set(tags))) or "없음"),
             "- 스팀 취향에만 있음: " + (", ".join(sorted(set(tags) - set(base_tags))) or "없음")])

    # --- 좋아하는 게임 링크만 쓰는 사람 흉내 ---
    seed_games = [g for g in base.get("games") or [] if g.get("top_tags")]
    rng = random.Random(0)
    lines = ["## 좋아하는 게임 링크만 있을 때(지금 취향의 시드에서 무작위, 20회 평균)", "",
             "| 링크 수 | min_tag_count | 태그 수 평균 | 지금 취향과 겹침 평균 | 태그 0개인 경우 |", "|---|---|---|---|---|"]
    for size in FAV_SIZES:
        if size > len(seed_games):
            continue
        for mc in MIN_COUNTS:
            counts, overlaps = [], []
            for _ in range(FAV_TRIALS):
                pick = rng.sample(seed_games, size)
                prof, _ = compute_profile({g["appid"]: g["top_tags"] for g in pick}, per_game, mc, [])
                counts.append(len(prof))
                overlaps.append(len({t for t, _ in prof} & set(base_tags)))
            lines.append(f"| {size} | {mc} | {sum(counts) / FAV_TRIALS:.1f} | {sum(overlaps) / FAV_TRIALS:.1f} | "
                         f"{sum(1 for c in counts if c == 0)}/{FAV_TRIALS} |")
    out(lines)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
