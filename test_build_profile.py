"""
test_build_profile.py — build_profile.py의 순수 로직만 확인한다. 네트워크는 부르지 않는다.

실행:  python -m pytest test_build_profile.py -q
"""

import build_profile as bp

RULES = {"min_tag_coverage": 0.9, "min_auto_resolution": 0.7}


# --- 입력 파일 고르기 ----------------------------------------------------------

def test_latest_peek_csv_uses_filename_date_then_suffix():
    files = ["peek_results_2026-09-10.csv", "peek_results_2026-09-10-3.csv", "peek_results_2026-09-10-2.csv",
             "peek_results_2026-09-09.csv", "notes.csv"]
    assert bp.latest_peek_csv(files) == "peek_results_2026-09-10-3.csv"


def test_latest_peek_csv_compares_suffix_as_number():
    # 사전순이면 '-9' > '-10'. 숫자로 비교해야 한다.
    assert bp.latest_peek_csv(["peek_results_2026-09-10-9.csv", "peek_results_2026-09-10-10.csv"]) \
        == "peek_results_2026-09-10-10.csv"


def test_later_date_wins_over_higher_suffix():
    assert bp.latest_peek_csv(["peek_results_2026-09-10-5.csv", "peek_results_2026-09-11.csv"]) \
        == "peek_results_2026-09-11.csv"


# --- 태그 ----------------------------------------------------------------------

def test_official_tags_sorted_by_weight_and_unknown_ids_kept():
    item = {"tags": [{"tagid": 1, "weight": 10}, {"tagid": 999, "weight": 50}, {"tagid": 2, "weight": 30}]}
    assert bp.official_tags(item, {1: "Puzzle", 2: "Cozy"}) == ["?999", "Cozy", "Puzzle"]


def test_official_tags_handles_missing_tags():
    assert bp.official_tags({}, {}) == []


# --- 프로파일 규칙 -------------------------------------------------------------

def test_stoplist_is_removed_before_taking_top_n():
    tags = {1: ["Multiplayer", "A", "B", "C", "D", "E"]}
    _, top = bp.compute_profile(tags, top_n=5, min_count=1, stoplist=["Multiplayer"])
    assert top[1] == ["A", "B", "C", "D", "E"]          # 제외 태그가 top5 자리를 차지하지 않는다


def test_min_count_keeps_tags_seen_in_enough_games():
    tags = {1: ["Puzzle", "Cozy"], 2: ["Puzzle", "Horror"], 3: ["Puzzle", "Cozy"]}
    profile, _ = bp.compute_profile(tags, top_n=5, min_count=2, stoplist=[])
    assert profile == [("Puzzle", 3), ("Cozy", 2)]


# --- 해석률 · 게이트 -----------------------------------------------------------

def test_resolution_rate_excludes_not_on_steam():
    rows = [{"상태": "정상", "해석": "자동"}, {"상태": "정상", "해석": "별칭"},
            {"상태": "검색실패", "해석": "-"}, {"상태": "대상아님", "해석": "-"}]
    assert bp.resolution_rate(rows) == (1, 3)


def test_gate_passes_when_both_metrics_meet_thresholds():
    assert bp.gate({"tag_coverage": 0.95, "auto_resolution": 0.74}, RULES) == []


def test_gate_blocks_steamspy_era_coverage():
    # ④ SteamSpy 시절 수치(33/46)는 게이트에 걸린다 — 걸리는 게 맞다
    problems = bp.gate({"tag_coverage": 33 / 46, "auto_resolution": 0.74}, RULES)
    assert len(problems) == 1 and "태그 커버리지" in problems[0]


def test_gate_blocks_low_auto_resolution():
    problems = bp.gate({"tag_coverage": 1.0, "auto_resolution": 0.5}, RULES)
    assert len(problems) == 1 and "자동 해석률" in problems[0]


# --- 시드 지문 (qa V3-7이 profile.json의 신선도를 대조한다) --------------------

def test_seed_fingerprint_ignores_comments_but_not_content(tmp_path):
    base = tmp_path / "base.yaml"
    commented = tmp_path / "commented.yaml"
    changed = tmp_path / "changed.yaml"
    base.write_text("games:\n  - Hue\nprofile_rules:\n  min_tag_count: 3\n", encoding="utf-8")
    commented.write_text("# 주석\ngames:\n  - Hue   # 메모\nprofile_rules:\n  min_tag_count: 3\n", encoding="utf-8")
    changed.write_text("games:\n  - Hue\nprofile_rules:\n  min_tag_count: 5\n", encoding="utf-8")
    assert bp.seed_fingerprint(base) == bp.seed_fingerprint(commented)
    assert bp.seed_fingerprint(base) != bp.seed_fingerprint(changed)
