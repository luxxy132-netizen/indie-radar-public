"""
test_report.py — report.py의 점수 · 기준 · 메시지 · 발송 로직을 가짜 데이터로 확인한다. 네트워크는 부르지 않는다.

실행:  python -m pytest test_report.py -q
"""

import datetime
import json

import pytest
import requests

import report

RUN_DAY = datetime.date(2026, 9, 10)
PROFILE = ["Indie", "Casual", "Puzzle", "Survival", "Crafting", "Horror", "Cozy"]
RULES = {"weekly_max": 3, "tags_per_new_game": 5, "rare_max_ratio": 0.10, "min_rare_tags": 2}
TOKEN = "xoxb-000-SECRET"


def rec(appid, tags, **kw):
    r = {"appid": appid, "name": f"G{appid}", "tags": tags, "is_free": False, "is_early_access": False,
         "price": "₩ 10,000", "price_krw": 10000, "discount_pct": 0, "review_count": 0, "percent_positive": 0,
         "release_kst": "2026-09-05T10:00+09:00", "store_url": f"https://store.steampowered.com/app/{appid}/G"}
    r.update(kw)
    return r


def pool(*games, fillers=18):
    """흔한 취향 태그(Indie · Casual · Puzzle)만 가진 신작으로 채운다 — 이 태그들은 '드물지' 않다."""
    return [rec(1000 + i, ["Indie", "Casual", "Puzzle"]) for i in range(fillers)] + list(games)


def picked_ids(games, rules=RULES, stoplist=()):
    passed, _ = report.score_week(games, PROFILE, stoplist, rules)
    return [p["game"]["appid"] for p in passed]


# --- 기간 ----------------------------------------------------------------------

def test_target_days_are_the_7_days_before_run_day():
    days = report.target_days(datetime.date(2026, 9, 14))      # 월요일 발송
    assert days[0] == datetime.date(2026, 9, 7) and days[-1] == datetime.date(2026, 9, 13)
    assert len(days) == 7


# --- 점수 · 기준 --------------------------------------------------------------

def test_two_rare_profile_tags_pass_and_one_does_not():
    games = pool(rec(1, ["Survival", "Crafting", "Indie"]), rec(2, ["Horror", "Indie", "Casual"]))
    assert picked_ids(games) == [1]


def test_many_common_profile_tags_do_not_pass():
    # 취향 태그 3개가 겹쳐도 전부 흔하면(Indie · Casual · Puzzle) 통과 못 한다
    games = pool(rec(3, ["Indie", "Casual", "Puzzle", "Action"]))
    assert picked_ids(games) == []


def test_common_tags_score_near_zero():
    games = pool(rec(1, ["Survival", "Crafting"]), rec(2, ["Survival", "Crafting", "Indie", "Casual"]))
    passed, _ = report.score_week(games, PROFILE, [], RULES)
    scores = {p["game"]["appid"]: p["score"] for p in passed}
    assert scores[2] - scores[1] < 0.5          # 흔한 태그 두 개를 더해도 거의 안 오른다


def test_stoplist_is_removed_before_taking_top_n():
    rules = {**RULES, "tags_per_new_game": 2}
    games = pool(rec(4, ["Multiplayer", "Survival", "Crafting"]))
    # 제외 태그를 먼저 빼야 상위 2개가 Survival · Crafting이 된다
    assert picked_ids(games, rules, stoplist=["Multiplayer"]) == [4]


def test_picks_are_capped_and_ordered_by_score():
    games = pool(
        rec(1, ["Survival", "Crafting", "Horror"]),       # 드문 태그 3개 — 점수 최고
        rec(2, ["Survival", "Crafting"]),
        rec(3, ["Cozy", "Horror"]),
        rec(4, ["Cozy", "Crafting"]),
        rec(5, ["Survival", "Cozy"]),
        fillers=35)
    passed, _ = report.score_week(games, PROFILE, [], RULES)
    picks = passed[:RULES["weekly_max"]]
    assert len(passed) == 5 and len(picks) == 3
    assert picks[0]["game"]["appid"] == 1
    assert [p["score"] for p in picks] == sorted((p["score"] for p in picks), reverse=True)


def test_weak_week_gives_fewer_picks_instead_of_filling():
    games = pool(rec(1, ["Survival", "Crafting"]))
    assert len(picked_ids(games)) == 1                   # weekly_max(3)를 억지로 채우지 않는다


def test_empty_week_scores_nothing():
    assert report.score_week([], PROFILE, [], RULES) == ([], [])


# --- 메시지 --------------------------------------------------------------------

DAYS = report.target_days(RUN_DAY)


def message(picks=(), loaded=DAYS, missing=(), pool_n=100, passed_n=None, reviews=None):
    return report.build_message(DAYS, list(loaded), list(missing), pool_n,
                                len(picks) if passed_n is None else passed_n, list(picks), reviews or {}, RULES)


def pick(g, rare=("Survival", "Crafting")):
    return {"game": g, "score": 1.0, "matched": list(rare), "rare": list(rare), "passed": True}


def test_message_lists_reason_tags_and_store_link():
    text = message([pick(rec(1, [], name="Valheim"))])
    assert "1. *<https://store.steampowered.com/app/1/G|Valheim>*" in text
    assert "🎯 추천 이유: Survival · Crafting" in text
    assert "💰 ₩ 10,000" in text and "📅 9/5 출시" in text and "⭐ 아직 리뷰 없음 (출시일 기준)" in text


def test_message_has_no_em_dash():
    # 2026-09-11 사람 요청 — 어느 경우의 메시지에도 줄표(—)가 없어야 한다
    texts = [message([pick(rec(1, [], is_early_access=True))], loaded=DAYS[:5], missing=DAYS[5:]),
             message([]), message([], pool_n=5), message([], loaded=[], missing=DAYS)]
    assert all("\u2014" not in t for t in texts)


def test_zero_picks_still_sends_a_message():
    assert "이번 주는 추천 없음" in message([])


def test_status_lines_stay_out_of_the_slack_message():
    # 2026-09-11 사람 요청 — 적재 없는 날 · "신작 N개 중 기준 통과 M개"는 슬랙에 안 보낸다
    text = report.build_message(DAYS, DAYS[:5], DAYS[5:], 100, 40, [pick(rec(1, []))], {}, RULES, {}, [DAYS[0]])
    assert "적재 없는 날" not in text and "기준 통과" not in text and "0개인 날" not in text
    assert text.startswith("🎮 인디게임 레이더\n9/3(목) ~ 9/9(수) 출시작\n\n1. ")


def test_status_line_keeps_the_numbers_for_the_report_file():
    line = report.status_line(DAYS[:5], DAYS[5:], [DAYS[0]], 237, 46, 10)
    assert line == ("기록: 신작 237개 · 기준 통과 46개 · 상위 10개 · 7일 중 5일치 적재 · "
                    "적재 없는 날 9/8, 9/9 · 적재됐지만 0개인 날 9/3")


def test_no_loaded_day_says_no_data():
    assert "이번 주 데이터 없음" in message([], loaded=[], missing=DAYS)


def test_slack_special_characters_in_names_are_escaped():
    text = message([pick(rec(1, [], name="A<B>&C"))])
    assert "A&lt;B&gt;&amp;C" in text and "A<B>" not in text


@pytest.mark.parametrize("fields, expected", [
    ({"is_free": True, "price": None}, "무료"),
    ({"price": "₩ 22,500", "discount_pct": 10, "price_original": "₩ 25,000"}, "₩ 25,000 → ₩ 22,500 (-10%)"),
    ({"price": "₩ 22,500", "discount_pct": 10}, "₩ 22,500 (-10%)"),      # 할인 전 가격이 없는 옛 적재분
    ({"price": "₩ 10,000"}, "₩ 10,000"),
    ({"price": None}, "가격 없음"),
])
def test_price_text(fields, expected):
    assert report.price_text(rec(1, [], **fields)) == expected


def test_review_text_prefers_latest_and_marks_fallback():
    g = rec(7, [], review_count=2, percent_positive=50)
    assert report.review_text(g, {7: (41, 88)}) == "리뷰 41개 (긍정 88%)"
    assert report.review_text(g, {}) == "리뷰 2개 (긍정 50%) (출시일 기준)"
    assert report.review_text(rec(8, []), {}) == "아직 리뷰 없음 (출시일 기준)"
    assert report.review_text(g, {7: (460624, 94)}) == "리뷰 460,624개 (긍정 94%)"   # 천 단위 쉼표


def test_early_access_is_shown():
    assert "🚧 얼리 액세스" in message([pick(rec(1, [], is_early_access=True))])


# --- 발송 (슬랙 개인 DM, 2026-09-11) --------------------------------------------

class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


def OK():
    return Resp(200, {"ok": True})


def ERR(code, status=200):
    return Resp(status, {"ok": False, "error": code})


class FakePost:
    def __init__(self, *responses):
        self.responses, self.calls, self.urls, self.headers = list(responses), [], [], []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        self.urls.append(url)
        self.headers.append(headers)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def dm(fake, blocks=None, sleep=lambda s: None):
    return report.post_dm(TOKEN, "U01", "hi", blocks=blocks, post=fake, sleep=sleep)


def test_post_dm_sends_to_the_member_with_the_token_in_the_header():
    fake = FakePost(OK())
    assert dm(fake) == (True, [])
    assert fake.calls == [{"channel": "U01", "text": "hi", "unfurl_links": False, "unfurl_media": False}]
    assert fake.urls == [report.SLACK_POST_URL] and fake.headers[0]["Authorization"] == f"Bearer {TOKEN}"


def test_post_dm_sends_blocks_with_fallback_text():
    fake = FakePost(OK())
    dm(fake, blocks=[{"type": "divider"}])
    assert fake.calls[0]["blocks"] == [{"type": "divider"}] and fake.calls[0]["text"] == "hi"


def test_post_dm_retries_server_errors_then_succeeds():
    ok, errors = dm(FakePost(Resp(500, "boom"), OK()))
    assert ok and len(errors) == 1


def test_post_dm_gives_up_after_retries_without_leaking_the_token():
    fake = FakePost(requests.ConnectionError(TOKEN), Resp(500, "boom"), Resp(502, TOKEN))
    ok, errors = dm(fake)
    assert not ok and len(fake.calls) == 1 + report.MAX_RETRIES
    assert "SECRET" not in " ".join(errors)                 # 토큰은 비밀이다


@pytest.mark.parametrize("code", ["invalid_blocks", "channel_not_found", "invalid_auth", "not_authed"])
def test_post_dm_does_not_repeat_a_rejected_request(code):
    fake = FakePost(ERR(code))
    ok, errors = dm(fake)
    assert not ok and len(fake.calls) == 1 and code in errors[0]


def test_post_dm_waits_for_retry_after_on_rate_limit():
    waits = []
    fake = FakePost(Resp(429, {"ok": False, "error": "ratelimited"}, {"Retry-After": "7"}), OK())
    assert dm(fake, sleep=waits.append)[0] and waits == [7]


def test_error_body_that_is_not_a_slack_code_is_not_kept():
    page = "<html>404 for /api/chat.postMessage?token=SECRET</html>"
    ok, errors = dm(FakePost(Resp(404, page)))
    assert not ok and "SECRET" not in " ".join(errors) and "(본문 생략)" in errors[0]


IMAGE_BLOCKS = [{"type": "image", "image_url": "x", "alt_text": "G"}, {"type": "divider"}]


def test_send_with_fallback_drops_images_only_for_invalid_blocks():
    sent = []
    accepted, errors, fell_back = report.send_with_fallback(
        lambda b: (sent.append(b), (False, ["시도0: HTTP 200 channel_not_found"]))[1], IMAGE_BLOCKS)
    assert accepted is None and not fell_back and len(sent) == 1       # 받는 사람이 없으면 이미지를 빼도 같다


def test_send_with_fallback_gives_up_when_text_only_also_fails():
    responses = [(False, ["시도0: HTTP 200 invalid_blocks"]), (False, ["시도0: HTTP 200 invalid_blocks"])]
    accepted, errors, fell_back = report.send_with_fallback(lambda b: responses.pop(0), IMAGE_BLOCKS)
    assert accepted is None and fell_back and len(errors) == 2


def test_already_sent_matches_the_exact_subscriber(tmp_path):
    label = "2026-09-03_2026-09-09"
    (tmp_path / f"{label}_ab.json").write_text('{"sent": true}', encoding="utf-8")
    assert not report.already_sent(label, "a", str(tmp_path))          # 'a'가 'ab'의 기록에 걸리지 않는다
    assert report.already_sent(label, "ab", str(tmp_path))
    (tmp_path / f"{label}_a-2.json").write_text('{"sent": true}', encoding="utf-8")
    assert report.already_sent(label, "a", str(tmp_path))


# --- main ---------------------------------------------------------------------

OWNER = "  - {id: owner, slack_user: U0OWNER, profile_path: profile.json}\n"
FRIEND = "  - {id: friend, slack_user: U0FRIEND, steam_profile: 'https://steamcommunity.com/id/friend'}\n"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """임시 저장소: 시드 · 운영자 취향 · 명단(운영자) · 9/9 적재분 하나. 발송일 9/10이면 대상은 9/3~9/9, 적재는 하루치."""
    monkeypatch.chdir(tmp_path)
    for var in ("SLACK_BOT_TOKEN", "GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(report, "PAUSE_SEC", 0)
    monkeypatch.setattr(report, "SLEEP_SEC", 0)
    # 발송 때 상점 이미지 · 소개를 부르는 네트워크 호출은 막는다 — 필요한 테스트만 따로 흉내 낸다
    monkeypatch.setattr(report, "fetch_store_extras", lambda appids, **kw: ({}, []))
    (tmp_path / "seed_library.yaml").write_text(
        "profile_rules:\n  tag_stoplist: [Multiplayer]\n"
        "match_rules:\n  weekly_max: 3\n  tags_per_new_game: 5\n  rare_max_ratio: 0.10\n  min_rare_tags: 2\n",
        encoding="utf-8")
    (tmp_path / "profile.json").write_text(json.dumps({"tags": [{"name": t, "games": 3} for t in PROFILE]}),
                                           encoding="utf-8")
    (tmp_path / "subscribers.yaml").write_text("subscribers:\n" + OWNER, encoding="utf-8")
    (tmp_path / "data").mkdir()
    games = pool(rec(1, ["Survival", "Crafting"], name="Valheim"))
    (tmp_path / "data" / "2026-09-09.json").write_text(json.dumps({"games": games}), encoding="utf-8")
    return tmp_path


def add_friend(repo, tags=("Indie", "Casual", "Puzzle"), last_run=None, run_at="2026-09-10T08:00:00+09:00"):
    """친구 한 명 — 흔한 태그만 있는 취향이라 이번 주 추천이 운영자와 다르다(0개)."""
    (repo / "subscribers.yaml").write_text("subscribers:\n" + OWNER + FRIEND, encoding="utf-8")
    if tags is not None:
        (repo / "profiles").mkdir(exist_ok=True)
        (repo / "profiles" / "friend.json").write_text(
            json.dumps({"generated_at": "2026-09-10T08:00:00+09:00", "tags": [{"name": t, "games": 3} for t in tags]}),
            encoding="utf-8")
    if last_run is not None:
        (repo / "profiles").mkdir(exist_ok=True)
        (repo / "profiles" / "_last_run.json").write_text(json.dumps({"run_at": run_at, "results": last_run}),
                                                         encoding="utf-8")


def slack(monkeypatch, *responses):
    monkeypatch.setenv("SLACK_BOT_TOKEN", TOKEN)
    fake = FakePost(*responses)
    monkeypatch.setattr(report.requests, "post", fake)
    return fake


def saved(repo, name="2026-09-03_2026-09-09_owner.json"):
    return json.loads((repo / "report" / name).read_text(encoding="utf-8"))


def test_main_without_token_saves_report_and_is_loud_on_actions(repo, monkeypatch, capsys):
    # 리뷰(2026-09-11): 토큰을 빠뜨리면 명단 전원이 못 받는다 — Actions에선 경고가 아니라 실패(이슈)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert report.main(["--date", "2026-09-10"]) == 1
    out = saved(repo)
    assert out["sent"] is False and "봇 토큰 없음" in out["send_note"]
    assert out["subscriber"] == "owner" and out["profile"]["path"] == "profile.json"
    assert [p["name"] for p in out["picks"]] == ["Valheim"]
    assert out["loaded_days"] == ["2026-09-09"] and len(out["missing_days"]) == 6
    assert "::error::봇 토큰 없음" in capsys.readouterr().out
    md = (repo / "report" / "2026-09-03_2026-09-09_owner.md").read_text(encoding="utf-8")
    assert "적재 없는 날" not in out["message"]                          # 슬랙 메시지엔 없고
    assert "기록: 신작 19개 · 기준 통과 1개 · 상위 1개 · 7일 중 1일치 적재" in md   # 리포트 파일엔 남는다


def test_main_sends_a_dm_and_marks_sent(repo, monkeypatch, capsys):
    fake = slack(monkeypatch, OK())
    assert report.main(["--date", "2026-09-10"]) == 0
    assert saved(repo)["sent"] is True
    assert fake.calls[0]["channel"] == "U0OWNER"                                      # 개인 DM
    assert fake.calls[0]["text"] == "🎮 인디게임 레이더 · 9/3~9/9 추천 1개"          # 알림 한 줄
    assert "Valheim" in json.dumps(fake.calls[0]["blocks"], ensure_ascii=False)     # 게임은 블록(카드) 안에


def test_main_does_not_resend_the_same_week_unless_forced(repo, monkeypatch, capsys):
    fake = slack(monkeypatch, OK(), OK())
    report.main(["--date", "2026-09-10"])
    assert report.main(["--date", "2026-09-10"]) == 0
    assert len(fake.calls) == 1                                   # 두 번째는 안 보냈다
    assert "이미 보낸 주" in saved(repo, "2026-09-03_2026-09-09_owner-2.json")["send_note"]
    report.main(["--date", "2026-09-10", "--force"])
    assert len(fake.calls) == 2


def test_main_send_failure_exits_1_and_keeps_report(repo, monkeypatch, capsys):
    slack(monkeypatch, *[Resp(500, "boom")] * 3)
    assert report.main(["--date", "2026-09-10"]) == 1
    assert saved(repo)["sent"] is False and "발송 실패" in saved(repo)["send_note"]


def test_main_with_no_loaded_day_exits_1_and_sends_nothing(repo, monkeypatch, capsys):
    fake = slack(monkeypatch)
    (repo / "data" / "2026-09-09.json").unlink()
    assert report.main(["--date", "2026-09-10"]) == 1
    assert "이번 주 데이터 없음" in saved(repo)["message"] and fake.calls == []   # 운영 문제는 DM으로 안 알린다


def test_latest_reviews_come_from_run_day_raw_new_and_tracked(repo):
    (repo / "raw").mkdir()
    item = lambda a, n: {"appid": a, "reviews": {"summary_filtered": {"review_count": n, "percent_positive": 90}}}
    doc = {"items_batches": [{"body": {"response": {"store_items": [item(1, 5)]}}}],
           "tracked_batches": [{"body": {"response": {"store_items": [item(9000, 41)]}}}]}
    (repo / "raw" / "2026-09-10.json").write_text(json.dumps(doc), encoding="utf-8")
    assert report.latest_reviews(RUN_DAY) == {1: (5, 90), 9000: (41, 90)}


def test_unreadable_day_counts_as_missing(repo):
    (repo / "data" / "2026-09-08.json").write_text("{깨짐", encoding="utf-8")
    games, loaded, missing, unreadable, empty = report.load_week(DAYS)
    assert [u["day"] for u in unreadable] == ["2026-09-08"]
    assert datetime.date(2026, 9, 8) in missing and loaded == [datetime.date(2026, 9, 9)]


# --- 코드 리뷰 3차(2026-09-10) 반영 ---------------------------------------------

def test_main_never_writes_the_token_into_report_files(repo, monkeypatch, capsys):
    slack(monkeypatch, *[requests.ConnectionError(TOKEN)] * 3)
    assert report.main(["--date", "2026-09-10"]) == 1
    for f in (repo / "report").iterdir():
        assert "SECRET" not in f.read_text(encoding="utf-8")
    assert "SECRET" not in capsys.readouterr().out


def test_small_pool_explains_why_there_are_no_picks():
    text = message([], pool_n=5)
    assert "5개뿐이라 드문 태그를 가를 수 없다" in text and "최소 10개" in text


def test_empty_and_null_days_are_not_silent(repo):
    (repo / "data" / "2026-09-07.json").write_text('{"games": []}', encoding="utf-8")
    (repo / "data" / "2026-09-08.json").write_text('{"games": null}', encoding="utf-8")
    games, loaded, missing, unreadable, empty = report.load_week(DAYS)
    assert empty == [datetime.date(2026, 9, 7)]
    assert [u["day"] for u in unreadable] == ["2026-09-08"]
    assert "적재됐지만 0개인 날 9/7" in report.status_line(loaded, missing, empty, len(games), 0, 0)   # 슬랙 대신 기록에


# --- 얼리 액세스 졸업 · 재출시 표시 (2026-09-10 결정: 남기되 표시) ------------------

@pytest.mark.parametrize("history, expected", [
    (("2021-02-02", True), "얼리 액세스 졸업 (첫 출시 2021년)"),
    (("2026-04-20", True), "얼리 액세스 졸업 (첫 출시 4/20)"),       # 같은 해면 날짜로
    ((None, True), "얼리 액세스 졸업"),
    (("2019-03-01", False), "첫 출시 2019년"),
    ((None, False), None),
])
def test_history_text(history, expected):
    g = rec(1, [], release_kst="2026-09-09T10:00+09:00")
    assert report.history_text(g, {1: history}) == expected


def test_graduation_is_shown_in_the_message():
    g = rec(1, [], name="Valheim", release_kst="2026-09-09T10:00+09:00")
    text = report.build_message(DAYS, DAYS, [], 100, 1, [pick(g)], {}, RULES, {1: ("2021-02-02", True)})
    assert "🌱 얼리 액세스 졸업 (첫 출시 2021년)" in text
    assert "📅 9/9 출시\n" in text                          # 졸업작은 출시일 줄에 덧붙이지 않는다


def test_rerelease_first_date_goes_on_the_release_line():
    g = rec(1, [], release_kst="2026-09-09T10:00+09:00")
    text = report.build_message(DAYS, DAYS, [], 100, 1, [pick(g)], {}, RULES, {1: ("2019-03-01", False)})
    assert "📅 9/9 출시 (첫 출시 2019년)" in text and "🌱" not in text


def test_release_history_reads_old_loads_from_their_source_raw(repo):
    ts = lambda y, m, d: int(datetime.datetime(y, m, d, 10, tzinfo=report.collect.KST).timestamp())
    (repo / "raw").mkdir()
    release = {"steam_release_date": ts(2026, 9, 9), "original_steam_release_date": ts(2021, 2, 2),
               "release_from_early_access_date": ts(2026, 9, 9)}
    doc = {"items_batches": [{"body": {"response": {"store_items": [{"appid": 1, "release": release}]}}}]}
    (repo / "raw" / "2026-09-10.json").write_text(json.dumps(doc), encoding="utf-8")
    old = {**rec(1, []), "_source_raw": "raw/2026-09-10.json"}             # 필드 없는 옛 적재분
    new = {**rec(2, []), "original_release_kst": "2019-03-01", "ea_graduated": False, "_source_raw": None}
    assert report.release_history([old, new]) == {1: ("2021-02-02", True), 2: ("2019-03-01", False)}


# --- A안: 상점 대표 이미지 카드 (2026-09-11) --------------------------------------

ASSETS = {"asset_url_format": "steam/apps/7/${FILENAME}?t=1", "header": "abc/header.jpg", "header_2x": "abc/header_2x.jpg"}


def test_asset_url_prefers_2x_and_uses_the_given_format():
    assert report.asset_url(ASSETS) == report.ASSET_BASE + "steam/apps/7/abc/header_2x.jpg?t=1"
    assert report.asset_url({**ASSETS, "header_2x": None}) == report.ASSET_BASE + "steam/apps/7/abc/header.jpg?t=1"
    assert report.asset_url({}) is None


def test_short_desc_flattens_strips_bold_and_truncates():
    assert report.short_desc("**진짜 주기율표** 위의\n샌드박스") == "진짜 주기율표 위의 샌드박스"
    long = report.short_desc("가" * 150)
    assert len(long) == report.DESC_LIMIT and long.endswith("…")
    assert report.short_desc(None) == ""


class FakeGet:
    def __init__(self, response=None, status=200):
        self.response, self.status, self.params = response, status, []

    def __call__(self, url, params=None, timeout=None):
        self.params.append(json.loads(params["input_json"]))
        return Resp2(self.response, self.status)


class Resp2:
    def __init__(self, payload, status):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self.payload


def test_fetch_store_extras_asks_in_korean_and_reads_image_and_desc():
    body = {"response": {"store_items": [
        {"appid": 7, "assets": ASSETS, "basic_info": {"short_description": "한국어 소개"}},
        {"appid": 8, "basic_info": {"short_description": "English only"}}]}}      # 이미지 없는 게임
    fake = FakeGet(body)
    extras, errors = report.fetch_store_extras([7, 8], get=fake, sleep=lambda s: None)
    assert fake.params[0]["context"]["language"] == "koreana"
    assert extras[7] == {"image": report.ASSET_BASE + "steam/apps/7/abc/header_2x.jpg?t=1", "desc": "한국어 소개"}
    assert extras[8] == {"image": None, "desc": "English only"} and errors == []


def test_fetch_store_extras_failure_is_recorded_not_raised():
    extras, errors = report.fetch_store_extras([7], get=FakeGet(None, status=503), sleep=lambda s: None)
    assert extras == {} and len(errors) == 1 + report.collect.MAX_RETRIES


def test_fetch_store_extras_skips_the_call_when_nothing_is_picked():
    fake = FakeGet({"response": {"store_items": []}})
    assert report.fetch_store_extras([], get=fake) == ({}, []) and fake.params == []


def picks_of(n):
    return [pick(rec(i, [], name=f"G{i}")) for i in range(1, n + 1)]


def blocks_for(picks, extras):
    return report.build_blocks(DAYS, DAYS, [], 100, len(picks), picks, {}, RULES, {}, [], extras)


def test_blocks_list_links_then_image_cards_with_a_divider_every_two_games():
    picks = picks_of(10)
    extras = {i: {"image": f"https://img/{i}.jpg", "desc": f"소개 {i}"} for i in range(1, 11)}
    blocks = blocks_for(picks, extras)
    kinds = [b["type"] for b in blocks]
    assert blocks[0]["text"]["text"] == "*🎮 인디게임 레이더*\n9/3(목) ~ 9/9(수) 출시작"
    assert blocks[1]["text"]["text"].startswith("1. <https://store.steampowered.com/app/1/G|G1>\n2. ")
    assert kinds.count("image") == 10 and kinds.count("divider") == 1 + 4     # 목록 뒤 1 + 두 게임마다 4(마지막 뒤 없음)
    assert kinds[-1] == "context" and len(blocks) <= 50                       # 슬랙 한 메시지 블록 한도
    card = blocks[kinds.index("image") + 1]["text"]["text"]
    assert card.startswith("1. *<https://store.steampowered.com/app/1/G|G1>*") and "📝 소개 1" in card


def test_blocks_without_extras_fall_back_to_text_cards():
    kinds = [b["type"] for b in blocks_for(picks_of(3), {})]
    assert "image" not in kinds and kinds.count("section") == 2 + 3          # 제목 · 목록 + 카드 3


def test_blocks_with_no_picks_send_the_text_message_once():
    blocks = report.build_blocks(DAYS, DAYS, [], 100, 0, [], {}, RULES, {}, [], {})
    assert len(blocks) == 1 and "이번 주는 추천 없음" in blocks[0]["text"]["text"]


def test_notification_text_is_one_line():
    assert report.notification_text(DAYS, picks_of(10)) == "🎮 인디게임 레이더 · 9/3~9/9 추천 10개"
    assert report.notification_text(DAYS, []) == "🎮 인디게임 레이더 · 9/3~9/9 이번 주는 추천 없음"


def test_main_does_not_fetch_extras_when_not_sending(repo, monkeypatch, capsys):
    # 리뷰(2026-09-11): 보내지 않는 실행에도 상점을 부르고 "글로만 보냈다"고 남겼다
    def boom(appids, **kw):
        raise AssertionError("보내지 않는데 상점을 불렀다")
    monkeypatch.setattr(report, "fetch_store_extras", boom)
    assert report.main(["--date", "2026-09-10"]) == 0
    out = saved(repo)
    assert out["sent"] is False and out["images"] == 0 and out["blocks"] is None
    assert out["extras_note"] == "보내지 않아 이미지 · 소개는 받지 않았다"


def test_main_records_store_lookup_failure(repo, monkeypatch, capsys):
    monkeypatch.setattr(report, "fetch_store_extras", lambda appids, **kw: ({}, [{"error": "HTTPError: 503"}]))
    slack(monkeypatch, OK())
    assert report.main(["--date", "2026-09-10"]) == 0
    out = saved(repo)
    assert out["sent"] is True and out["extras_errors"] == ["HTTPError: 503"]
    assert "대표 이미지 0/1개 · 소개 0/1개 · 상점 정보 조회 실패" in out["extras_note"]
    assert "상점 정보 조회 실패" in (repo / "report" / "2026-09-03_2026-09-09_owner.md").read_text(encoding="utf-8")


EXTRA = {1: {"image": "https://img/1.jpg", "desc": "굽고 & <섞는> 게임"}}


def test_main_sends_the_image_and_desc_it_fetched_for_the_picks(repo, monkeypatch, capsys):
    # 리뷰(2026-09-11): 픽스처가 늘 ({}, [])라 main이 extras를 버려도 모든 테스트가 통과했다
    asked = []
    monkeypatch.setattr(report, "fetch_store_extras", lambda appids, **kw: (asked.append(appids), (EXTRA, []))[1])
    fake = slack(monkeypatch, OK())
    assert report.main(["--date", "2026-09-10"]) == 0
    assert asked == [[1]]                                              # 추천된 게임의 appid로 물었다
    blocks = fake.calls[0]["blocks"]
    image = next(b for b in blocks if b["type"] == "image")
    assert image == {"type": "image", "image_url": "https://img/1.jpg", "alt_text": "Valheim"}
    assert "📝 굽고 &amp; &lt;섞는&gt; 게임" in json.dumps(blocks, ensure_ascii=False)   # 소개도 mrkdwn 이스케이프
    out = saved(repo)
    assert out["images"] == 1 and out["blocks"] == blocks
    assert out["extras_note"] == "대표 이미지 1/1개 · 소개 1/1개"


def test_main_resends_without_images_when_slack_rejects_the_blocks(repo, monkeypatch, capsys):
    # 리뷰(2026-09-11): 이미지 주소 하나를 슬랙이 못 가져오면 invalid_blocks로 메시지 전체가 거절된다
    monkeypatch.setattr(report, "fetch_store_extras", lambda appids, **kw: (EXTRA, []))
    fake = slack(monkeypatch, ERR("invalid_blocks"), OK())
    assert report.main(["--date", "2026-09-10"]) == 0
    assert len(fake.calls) == 2                                        # 거절은 같은 걸 다시 보내지 않는다
    assert [b["type"] for b in fake.calls[1]["blocks"]] == [b["type"] for b in fake.calls[0]["blocks"] if b["type"] != "image"]
    out = saved(repo)
    assert out["sent"] is True and out["images"] == 0
    assert "이미지를 빼고 다시 보냈다" in out["extras_note"] and "invalid_blocks" in out["send_note"]


# --- 여러 사람 (2026-09-11) ----------------------------------------------------

def test_each_subscriber_gets_their_own_dm_and_report(repo, monkeypatch, capsys):
    add_friend(repo)
    monkeypatch.setenv("GITHUB_OUTPUT", str(repo / "gh_output.txt"))
    fake = slack(monkeypatch, OK(), OK())
    assert report.main(["--date", "2026-09-10"]) == 0
    assert [c["channel"] for c in fake.calls] == ["U0OWNER", "U0FRIEND"]
    assert [p["name"] for p in saved(repo)["picks"]] == ["Valheim"]
    friend = saved(repo, "2026-09-03_2026-09-09_friend.json")
    assert friend["picks"] == [] and "이번 주는 추천 없음" in friend["message"]         # 취향이 다르면 추천도 다르다
    assert friend["profile"]["path"] == "profiles/friend.json"                       # OS와 상관없이 '/'
    summary = (repo / "report" / "2026-09-03_2026-09-09-summary.md").read_text(encoding="utf-8")
    assert "**owner**" in summary and "**friend**" in summary
    assert "2026-09-03_2026-09-09-summary.md" in (repo / "gh_output.txt").read_text(encoding="utf-8")


def test_one_failure_does_not_stop_the_others(repo, monkeypatch, capsys):
    add_friend(repo)
    fake = slack(monkeypatch, ERR("channel_not_found"), OK())
    assert report.main(["--date", "2026-09-10"]) == 1                 # 누구라도 실패하면 1(이슈)
    assert len(fake.calls) == 2
    assert saved(repo)["sent"] is False and "channel_not_found" in saved(repo)["send_note"]
    assert saved(repo, "2026-09-03_2026-09-09_friend.json")["sent"] is True


def test_missing_profile_fails_only_that_person(repo, monkeypatch, capsys):
    add_friend(repo, tags=None)                                       # 취향 파일을 한 번도 못 만든 친구
    fake = slack(monkeypatch, OK())
    assert report.main(["--date", "2026-09-10"]) == 1
    assert [c["channel"] for c in fake.calls] == ["U0OWNER"]
    friend = saved(repo, "2026-09-03_2026-09-09_friend.json")
    assert friend["sent"] is False and "취향 파일" in friend["send_note"]


def test_stale_profile_is_recorded(repo, monkeypatch, capsys):
    add_friend(repo, last_run={"friend": {"status": report.ST_STALE, "problems": ["게임 세부 정보 비공개"]}})
    slack(monkeypatch, OK(), OK())
    assert report.main(["--date", "2026-09-10"]) == 0
    friend = saved(repo, "2026-09-03_2026-09-09_friend.json")
    assert friend["profile"]["stale"] is True and friend["profile"]["reason"] == "게임 세부 정보 비공개"
    assert "지난 취향으로 골랐다" in friend["send_note"]


def test_profile_without_this_weeks_refresh_is_marked_stale(repo, monkeypatch, capsys):
    # 리뷰(2026-09-11): 갱신 단계가 중간에 죽으면 지난주 _last_run.json을 이번 주 결과로 믿었다
    add_friend(repo, last_run={"friend": {"status": "갱신", "problems": []}}, run_at="2026-09-03T08:00:00+09:00")
    slack(monkeypatch, OK(), OK())
    report.main(["--date", "2026-09-10"])
    friend = saved(repo, "2026-09-03_2026-09-09_friend.json")
    assert friend["profile"]["stale"] is True and "갱신 기록이 없다" in friend["profile"]["reason"]
    assert saved(repo)["profile"]["stale"] is False                  # 운영자(고정 취향)는 갱신 대상이 아니다


def test_one_subscribers_crash_does_not_stop_the_next(repo, monkeypatch, capsys):
    # 리뷰(2026-09-11): report_one의 예외가 main을 통째로 죽여 뒤 사람이 못 받았다
    add_friend(repo)
    real = report.read_profile

    def flaky(path):
        if path == "profile.json":
            raise RuntimeError("boom")
        return real(path)
    monkeypatch.setattr(report, "read_profile", flaky)
    fake = slack(monkeypatch, OK())
    assert report.main(["--date", "2026-09-10"]) == 1
    assert [c["channel"] for c in fake.calls] == ["U0FRIEND"]
    summary = (repo / "report" / "2026-09-03_2026-09-09-summary.md").read_text(encoding="utf-8")
    assert "예외 RuntimeError" in summary


def test_read_last_run_ignores_odd_entries(tmp_path):
    p = tmp_path / "last.json"
    p.write_text(json.dumps({"run_at": "2026-09-10T08:00", "results": {"a": "x", "b": {"status": "갱신"}}}),
                 encoding="utf-8")
    assert report.read_last_run(str(p)) == ("2026-09-10T08:00", {"b": {"status": "갱신"}})
    p.write_text("[1]", encoding="utf-8")
    assert report.read_last_run(str(p)) == (None, {})


def test_empty_subscriber_list_is_loud(repo, monkeypatch, capsys):
    (repo / "subscribers.yaml").write_text("subscribers: []\n", encoding="utf-8")
    fake = slack(monkeypatch)
    assert report.main(["--date", "2026-09-10"]) == 1
    assert fake.calls == [] and "명단이 비었다" in capsys.readouterr().out


def test_asset_url_rejects_a_format_without_the_filename_slot():
    assert report.asset_url({**ASSETS, "asset_url_format": "steam/apps/7/header.jpg"}) is None


def test_fetch_store_extras_survives_odd_shapes():
    body = {"response": {"store_items": [{"appid": 7, "assets": ["x"], "basic_info": "y"}]}}
    extras, errors = report.fetch_store_extras([7], get=FakeGet(body), sleep=lambda s: None)
    assert extras == {7: {"image": None, "desc": ""}} and errors == []


def test_weekly_max_is_capped_by_the_slack_block_limit(tmp_path):
    picks = picks_of(report.MAX_WEEKLY_PICKS)
    extras = {i: {"image": f"https://img/{i}.jpg", "desc": "d"} for i in range(1, report.MAX_WEEKLY_PICKS + 1)}
    assert len(blocks_for(picks, extras)) <= 50
    seed = tmp_path / "seed.yaml"
    seed.write_text("profile_rules:\n  tag_stoplist: []\nmatch_rules:\n  weekly_max: 19\n", encoding="utf-8")
    with pytest.raises(ValueError, match="weekly_max"):
        report.load_rules(str(seed))


def test_excluded_tags_are_dropped_from_a_fixed_profile(repo, monkeypatch, capsys):
    # 운영자 profile.json은 다시 만들지 않는다 — 리포트가 읽을 때 exclude_tags를 뺀다(2026-09-11 사람 결정)
    (repo / "seed_library.yaml").write_text(
        (repo / "seed_library.yaml").read_text(encoding="utf-8") + "user_profile_rules:\n  exclude_tags: [Survival]\n",
        encoding="utf-8")
    assert report.main(["--date", "2026-09-10"]) == 0
    out = saved(repo)
    assert "Survival" not in out["profile_tags"]
    assert out["picks"] == []                 # Valheim은 Survival · Crafting 두 개로 통과했었다
