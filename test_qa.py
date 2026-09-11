"""
test_qa.py — qa.py의 점검 · 적재 로직을 가짜 원본으로 확인한다. 네트워크는 부르지 않는다.

실행:  python -m pytest test_qa.py -q
"""

import datetime
import json

import pytest

import collect
import qa

KST = collect.KST
W0 = int(datetime.datetime(2026, 9, 9, tzinfo=KST).timestamp())    # 대상일 9/9 00:00 KST
DAY = 86400
IN = W0 + 3600
RULES = {"min_tag_coverage": 0.9, "min_auto_resolution": 0.7}
FP = "지문"


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "seed_library.yaml").write_text(
        "profile_rules:\n  min_tag_coverage: 0.9\n  min_auto_resolution: 0.7\n", encoding="utf-8")


def game(appid, ts=IN, **kw):
    g = {"appid": appid, "success": 1, "type": 0, "name": f"G{appid}",
         "release": {"steam_release_date": ts},
         "tags": [{"tagid": 1, "weight": 10}, {"tagid": 2, "weight": 90}],
         "best_purchase_option": {"formatted_final_price": "₩ 1,000", "final_price_in_cents": "100000",
                                  "discount_pct": 0},
         "reviews": {"summary_filtered": {"review_count": 0, "percent_positive": 0}},
         "store_url_path": f"app/{appid}/G"}
    g.update(kw)
    return g


def listing(games):
    return [{"appid": g["appid"], "release": g["release"]} for g in games]


def make_doc(games, pages=None, meta=None, tracked=None, requested=None, tag_list=True, run_day="2026-09-10"):
    return {
        "meta": {"run_day": run_day, "target_day": "2026-09-09", "window_unix": [W0, W0 + DAY],
                 "query_stop_reason": collect.STOP_REACHED, "window_covered": True, "failed_calls": 0,
                 "errors": [], "tracked_from": [], "track_unreadable": [], **(meta or {})},
        "query_pages": pages if pages is not None else [{"start": 0, "body": {"response": {"store_items": listing(games)}}}],
        "items_batches": [{"appids": requested if requested is not None else [g["appid"] for g in games],
                           "body": {"response": {"store_items": games}}}],
        "tag_list": {"response": {"tags": [{"tagid": 1, "name": "Puzzle"}, {"tagid": 2, "name": "Cozy"}]}}
        if tag_list else None,
        "tracked_batches": tracked if tracked is not None else [],
    }


def checks(doc, prev_docs=(), prev_unreadable=(), profile=None, fingerprint=FP):
    return {c["id"]: c for c in qa.run_checks(doc, list(prev_docs), list(prev_unreadable), profile, RULES, fingerprint)}


def status(doc, cid, **kw):
    return checks(doc, **kw)[cid]["status"]


# --- 깨끗한 수집분 --------------------------------------------------------------

def test_clean_collection_has_no_fail_and_every_skip_says_why():
    result = checks(make_doc([game(i) for i in range(10)]))
    assert [c for c in result.values() if c["status"] == qa.FAIL] == []
    assert {cid for cid, c in result.items() if c["status"] == qa.SKIP} == {"V1-2", "V3-3", "V3-4", "V3-5", "V3-7", "Q-2"}
    assert all(c["summary"] for c in result.values() if c["status"] == qa.SKIP)     # 조용한 SKIP 없음


def test_only_todays_data_checks_block_load():
    blocking = {cid for cid, c in checks(make_doc([game(1)])).items() if c["blocks_load"]}
    assert blocking == {"C0", "V1-1", "V3-1", "V3-6", "V3-8", "Q-1", "Q-2", "Q-3", "Q-4"}


# --- C0 · V1 ------------------------------------------------------------------

def test_old_raw_without_window_covered_falls_back_to_stop_reason():
    doc = make_doc([game(1)])
    del doc["meta"]["window_covered"]
    assert status(doc, "C0") == qa.PASS
    doc["meta"]["query_stop_reason"] = "빈 페이지"
    assert status(doc, "C0") == qa.FAIL


def test_uncovered_window_or_failed_call_fails_c0():
    assert status(make_doc([game(1)], meta={"window_covered": False}), "C0") == qa.FAIL
    assert status(make_doc([game(1)], meta={"failed_calls": 1}), "C0") == qa.FAIL


def test_missing_failed_calls_is_not_treated_as_zero():
    doc = make_doc([game(1)])
    del doc["meta"]["failed_calls"]
    assert status(doc, "C0") == qa.FAIL


def test_tracked_call_failure_alone_does_not_fail_c0():
    assert status(make_doc([game(1)], meta={"failed_calls": 1, "failed_calls_tracked": 1}), "C0") == qa.PASS


def test_requested_but_not_received_fails_v1_1():
    c = checks(make_doc([game(1)], requested=[1, 999]))["V1-1"]
    assert c["status"] == qa.FAIL and c["details"] == [999]


# --- V3 (오늘) ----------------------------------------------------------------

def test_success_not_1_fails_v3_1():
    assert status(make_doc([game(1), game(2, success=0)]), "V3-1") == qa.FAIL


def test_software_is_info_and_excluded_from_games():
    doc = make_doc([game(1), game(2, type=6, tags=[])])      # 소프트웨어는 태그가 없어도 커버리지에 안 들어간다
    result = checks(doc)
    assert result["V3-2"]["status"] == qa.INFO and result["V3-2"]["details_total"] == 1
    assert result["V3-8"]["status"] == qa.PASS


def test_free_game_without_price_is_not_missing_but_paid_one_is():
    free = game(1, is_free=True, best_purchase_option=None)
    assert status(make_doc([free]), "V3-6") == qa.PASS
    paid = game(2, best_purchase_option=None)
    assert status(make_doc([free, paid]), "V3-6") == qa.FAIL


def test_paid_game_with_unreadable_price_fails_v3_6():
    broken = game(1, best_purchase_option={"formatted_final_price": "₩ ?", "final_price_in_cents": None})
    assert status(make_doc([broken]), "V3-6") == qa.FAIL


def test_tag_coverage_below_threshold_fails_v3_8():
    games = [game(i) for i in range(8)] + [game(8, tags=[]), game(9, tags=[])]     # 80% < 90%
    assert status(make_doc(games), "V3-8") == qa.FAIL


def test_zero_games_on_target_day_fails_v3_8():
    assert status(make_doc([]), "V3-8") == qa.FAIL


# --- V3-7 시드 프로파일 -------------------------------------------------------

def profile(coverage=1.0, auto=0.74, fingerprint=FP):
    return {"metrics": {"auto_resolution": auto, "tag_coverage": coverage}, "rules": {"min_tag_coverage": 0.0,
            "min_auto_resolution": 0.0}, "generated_at": "x", "seed_fingerprint": fingerprint}


def test_v3_7_passes_for_fresh_profile_meeting_current_rules():
    assert status(make_doc([game(1)]), "V3-7", profile=profile()) == qa.PASS


def test_v3_7_judges_with_current_rules_not_the_ones_written_in_the_file():
    # 파일 안 규칙(0.0)으론 통과했던 프로파일이라도, 지금 규칙(0.9)엔 미달이면 FAIL
    assert status(make_doc([game(1)]), "V3-7", profile=profile(coverage=0.8)) == qa.FAIL


def test_v3_7_fails_when_profile_was_built_from_a_different_seed():
    c = checks(make_doc([game(1)]), profile=profile(fingerprint="옛 지문"))["V3-7"]
    assert c["status"] == qa.FAIL and "build_profile" in c["details"][0]


def test_v3_7_fails_on_unreadable_profile():
    assert status(make_doc([game(1)]), "V3-7", profile={"_error": "ValueError"}) == qa.FAIL


# --- 추적 (V3-3 · V3-4) --------------------------------------------------------

def tracked_batch(requested, items):
    return [{"appids": requested, "body": {"response": {"store_items": items}}}]


def test_tracked_appid_missing_or_not_success_fails_v3_3():
    doc = make_doc([game(1)], tracked=tracked_batch([9000, 9001, 9002], [game(9000), game(9001, success=0)]))
    c = checks(doc)["V3-3"]
    assert c["status"] == qa.FAIL and c["details_total"] == 2


def test_unreadable_previous_raw_fails_v3_3():
    doc = make_doc([game(1)], meta={"track_unreadable": [{"path": "2026-09-08.json", "error": "ValueError"}]})
    assert status(doc, "V3-3") == qa.FAIL


def test_raw_from_before_tracking_is_skipped_not_failed():
    doc = make_doc([game(1)])
    del doc["tracked_batches"]
    assert status(doc, "V3-3") == qa.SKIP


def previous_with_reviews(appid, count):
    return make_doc([game(appid, reviews={"summary_filtered": {"review_count": count}})], run_day="2026-09-09")


def test_review_count_drop_fails_v3_4():
    doc = make_doc([game(1)], tracked=tracked_batch([9000], [game(9000, reviews={"summary_filtered": {"review_count": 7}})]))
    c = checks(doc, prev_docs=[previous_with_reviews(9000, 10)])["V3-4"]
    assert c["status"] == qa.FAIL and c["details"] == ["9000: 10 → 7"]


def test_review_count_rise_passes_v3_4_and_latest_previous_wins():
    doc = make_doc([game(1)], tracked=tracked_batch([9000], [game(9000, reviews={"summary_filtered": {"review_count": 12}})]))
    older, newer = previous_with_reviews(9000, 20), previous_with_reviews(9000, 11)   # 오래된 순
    assert status(doc, "V3-4", prev_docs=[older, newer]) == qa.PASS


def test_tracked_item_without_reviews_fails_v3_4_instead_of_skipping():
    doc = make_doc([game(1)], tracked=tracked_batch([9000], [game(9000, reviews=None)]))
    assert status(doc, "V3-4") == qa.FAIL


# --- 목록 (Q) -----------------------------------------------------------------

def test_out_of_order_listing_fails_q1():
    games = [game(1, ts=IN), game(2, ts=IN + 100)]            # 내림차순이어야 하는데 올라간다
    assert status(make_doc(games), "Q-1") == qa.FAIL


def pages_of(*spec):
    """spec = (start, appids), ... — 출시 시각은 appid가 클수록 이르게(내림차순 유지)."""
    item = lambda a: {"appid": a, "release": {"steam_release_date": IN - a}}
    return [{"start": s, "body": {"response": {"store_items": [item(a) for a in ids]}}} for s, ids in spec]


def test_matching_overlap_passes_q2():
    assert status(make_doc([game(1)], pages=pages_of((0, range(50)), (40, range(40, 90)))), "Q-2") == qa.PASS


def test_shifted_overlap_fails_q2():
    pages = pages_of((0, range(50)), (40, range(41, 91)))     # 한 칸 밀렸다
    assert status(make_doc([game(1)], pages=pages), "Q-2") == qa.FAIL


def test_short_last_page_is_compared_at_the_right_offset():
    # 다음 페이지가 5개뿐이면 앞 페이지의 40~44와 비교해야 한다(끝의 45~49가 아니라)
    pages = pages_of((0, range(50)), (40, range(40, 45)))
    assert status(make_doc([game(1)], pages=pages), "Q-2") == qa.PASS


def test_q2_skip_reasons_are_true():
    one = checks(make_doc([game(1)]))["Q-2"]
    assert one["status"] == qa.SKIP and "경계가 없다" in one["summary"]
    no_overlap = checks(make_doc([game(1)], pages=pages_of((0, range(50)), (50, range(50, 100)))))["Q-2"]
    assert no_overlap["status"] == qa.SKIP and "도입 전" in no_overlap["summary"]


def test_empty_tag_table_fails_q3():
    doc = make_doc([game(1)])
    doc["tag_list"] = {"response": {"tags": []}}
    assert status(doc, "Q-3") == qa.FAIL


def test_listing_item_without_release_date_fails_q4():
    pages = [{"start": 0, "body": {"response": {"store_items": [*listing([game(1)]), {"appid": 77}]}}}]
    c = checks(make_doc([game(1)], pages=pages))["Q-4"]
    assert c["status"] == qa.FAIL and "출시일 없는" in c["details"][0]


def test_non_dict_store_item_fails_q4():
    pages = [{"start": 0, "body": {"response": {"store_items": [*listing([game(1)]), "oops"]}}}]
    assert status(make_doc([game(1)], pages=pages), "Q-4") == qa.FAIL


def test_target_game_without_reviews_fails_q4():
    assert status(make_doc([game(1, reviews=None)]), "Q-4") == qa.FAIL


# --- 점검이 죽어도 결과는 남는다 ------------------------------------------------

def test_check_that_raises_becomes_a_fail(monkeypatch):
    monkeypatch.setattr(qa, "check_order", lambda doc: 1 / 0)
    c = checks(make_doc([game(1)]))["Q-1"]
    assert c["status"] == qa.FAIL and "ZeroDivisionError" in c["summary"]


def test_missing_window_meta_becomes_a_fail_not_a_crash():
    doc = make_doc([game(1)])
    del doc["meta"]["window_unix"]
    result = qa.run_checks(doc, [], [], None, RULES, FP)
    assert [c["status"] for c in result] == [qa.FAIL]


# --- 적재 ---------------------------------------------------------------------

def test_load_record_normalizes_price_and_tags():
    rec = qa.load_record(game(7, best_purchase_option={"formatted_final_price": "₩ 9,450",
                                                        "formatted_original_price": "₩ 10,500",
                                                        "final_price_in_cents": "945000", "discount_pct": 10}),
                         {1: "Puzzle", 2: "Cozy"})
    assert rec["price_krw"] == 9450 and rec["discount_pct"] == 10
    assert rec["price_original"] == "₩ 10,500"               # 주간 리포트가 "할인 전 → 후"로 쓴다
    assert rec["tags"] == ["Cozy", "Puzzle"]                  # 가중치순
    assert rec["is_free"] is False                            # None으로 와도 False로 맞춘다
    assert rec["store_url"] == "https://store.steampowered.com/app/7/G"


def test_release_fields_mark_early_access_graduation_but_not_planned_one():
    ts = lambda y, m, d: int(datetime.datetime(y, m, d, 10, tzinfo=KST).timestamp())
    valheim = {"steam_release_date": ts(2026, 9, 9), "original_steam_release_date": ts(2021, 2, 2),
               "release_from_early_access_date": ts(2026, 9, 9)}
    planned = {"steam_release_date": ts(2026, 9, 9), "original_steam_release_date": ts(2026, 5, 30),
               "release_from_early_access_date": ts(2026, 11, 13)}     # 졸업 예정일 — 아직 졸업 아님
    assert qa.release_fields(valheim) == ("2021-02-02", True)
    assert qa.release_fields(planned) == ("2026-05-30", False)
    assert qa.release_fields({"steam_release_date": ts(2026, 9, 9)}) == (None, False)
    rec = qa.load_record(game(1, release=valheim), {})
    assert rec["original_release_kst"] == "2021-02-02" and rec["ea_graduated"] is True


def write_raw(tmp_path, doc, name="2026-09-10.json"):
    (tmp_path / "raw").mkdir(exist_ok=True)
    body = doc if isinstance(doc, str) else json.dumps(doc)
    (tmp_path / "raw" / name).write_text(body, encoding="utf-8")
    return f"raw/{name}"


def test_main_passes_and_loads(tmp_path, capsys):
    raw = write_raw(tmp_path, make_doc([game(1), game(2), game(3, type=6)]))
    assert qa.main([raw]) == 0
    loaded = json.loads((tmp_path / "data" / "2026-09-09.json").read_text(encoding="utf-8"))
    assert [g["appid"] for g in loaded["games"]] == [1, 2]    # 소프트웨어는 적재하지 않는다
    assert (tmp_path / "qa" / "2026-09-10.json").exists() and (tmp_path / "qa" / "2026-09-10.md").exists()


def test_main_todays_fail_rolls_back_load(tmp_path, capsys):
    raw = write_raw(tmp_path, make_doc([game(1)], meta={"window_covered": False}))
    assert qa.main([raw]) == 1
    assert not (tmp_path / "data").exists()                   # 적재 안 함 = 롤백
    md = (tmp_path / "qa" / "2026-09-10.md").read_text(encoding="utf-8")
    assert "롤백" in md and "적재 차단" in md


def test_main_tracked_fail_alerts_but_keeps_todays_load(tmp_path, capsys):
    doc = make_doc([game(1)], tracked=tracked_batch([9000], []))     # 지난주 게임이 사라졌다
    raw = write_raw(tmp_path, doc)
    assert qa.main([raw]) == 1                                # 알림은 한다
    assert (tmp_path / "data" / "2026-09-09.json").exists()   # 오늘 신작 적재는 한다
    assert "막지 않는다" in (tmp_path / "qa" / "2026-09-10.md").read_text(encoding="utf-8")


def test_rerun_keeps_previous_report_and_existing_load(tmp_path, capsys):
    raw = write_raw(tmp_path, make_doc([game(1)]))
    assert qa.main([raw]) == 0
    first_load = (tmp_path / "data" / "2026-09-09.json").read_text(encoding="utf-8")
    assert qa.main([raw]) == 0
    assert (tmp_path / "qa" / "2026-09-10-2.json").exists()   # 점검 결과는 덮어쓰지 않는다
    assert (tmp_path / "data" / "2026-09-09.json").read_text(encoding="utf-8") == first_load


def test_main_reads_previous_collections_for_review_check(tmp_path, capsys):
    write_raw(tmp_path, previous_with_reviews(9000, 10), name="2026-09-09.json")
    doc = make_doc([game(1)], tracked=tracked_batch([9000], [game(9000, reviews={"summary_filtered": {"review_count": 3}})]))
    raw = write_raw(tmp_path, doc)
    assert qa.main([raw]) == 1
    report = json.loads((tmp_path / "qa" / "2026-09-10.json").read_text(encoding="utf-8"))
    assert {c["id"]: c["status"] for c in report["checks"]}["V3-4"] == qa.FAIL


def test_broken_raw_still_writes_a_report(tmp_path, capsys):
    raw = write_raw(tmp_path, "{깨진 json")
    assert qa.main([raw]) == 1
    md = (tmp_path / "qa" / "2026-09-10.md").read_text(encoding="utf-8")   # 이슈 본문이 빈 채로 열리지 않는다
    assert "읽지 못했다" in md


def test_raw_missing_window_meta_still_writes_a_report(tmp_path, capsys):
    doc = make_doc([game(1)])
    del doc["meta"]["window_unix"]
    raw = write_raw(tmp_path, doc)
    assert qa.main([raw]) == 1
    assert (tmp_path / "qa" / "2026-09-10.md").exists()
    assert not (tmp_path / "data").exists()


def test_github_outputs_written_when_running_in_actions(tmp_path, monkeypatch, capsys):
    out, summary = tmp_path / "gh_out", tmp_path / "gh_summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    raw = write_raw(tmp_path, make_doc([game(1)]))
    qa.main([raw])
    assert "summary=qa" in out.read_text(encoding="utf-8") and "failed=0" in out.read_text(encoding="utf-8")
    assert "/steam-qa" in summary.read_text(encoding="utf-8")
