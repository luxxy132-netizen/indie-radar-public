"""
test_build_user_profiles.py — 스팀 프로필 → 사람별 취향 파일. 네트워크는 부르지 않는다(가짜 get).

실행:  python -m pytest test_build_user_profiles.py -q
"""

import json

import pytest
import requests

import build_user_profiles as bup

KEY = "SECRETKEY123"
STEAMID = "76561197960435530"
TAGS = {1: "Puzzle", 2: "Cozy", 3: "Horror", 4: "Crafting", 5: "Survival", 6: "Multiplayer"}
RULES = {"top_n": 4, "min_playtime_min": 60, "candidate_factor": 3, "large_seed_games": 30,
         "min_tag_count": 3, "min_tag_count_small": 2, "min_seed_games": 3, "min_profile_tags": 2}
STOP = ["Multiplayer"]


class Resp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for url: https://x/?key={KEY}")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSteam:
    """경로 조각 → 응답 목록. 마지막 응답은 계속 돌려준다."""

    def __init__(self, routes):
        self.routes, self.calls = {k: list(v) for k, v in routes.items()}, []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        for part, seq in self.routes.items():
            if part in url:
                r = seq.pop(0) if len(seq) > 1 else seq[0]
                if isinstance(r, Exception):
                    raise r
                return r
        raise AssertionError(f"예상 못 한 호출: {url}")


def item(appid, tagids, type_=0, name=None):
    return {"appid": appid, "success": 1, "type": type_, "name": name or f"G{appid}",
            "tags": [{"tagid": t, "weight": 100 - i} for i, t in enumerate(tagids)]}


def items_body(*its):
    return {"response": {"store_items": list(its)}}


def owned(*games):
    return {"response": {"game_count": len(games), "games": [
        {"appid": a, "name": f"G{a}", "playtime_forever": m} for a, m in games]}}


def taglist():
    return {"response": {"tags": [{"tagid": k, "name": v} for k, v in TAGS.items()]}}


NOSLEEP = {"sleep": lambda s: None}


# --- 주소 해석 -----------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://steamcommunity.com/profiles/76561197960435530", ("steamid", STEAMID)),
    ("https://steamcommunity.com/profiles/76561197960435530/?l=korean", ("steamid", STEAMID)),
    ("steamcommunity.com/id/robinwalker/", ("vanity", "robinwalker")),
    ("https://steamcommunity.com/id/robin_walker-1", ("vanity", "robin_walker-1")),
])
def test_parse_profile_url(url, expected):
    assert bup.parse_profile_url(url) == expected


@pytest.mark.parametrize("url", ["https://steamcommunity.com/profiles/123", "https://example.com/id/x",
                                 "https://store.steampowered.com/app/10/", ""])
def test_parse_profile_url_rejects_other_addresses(url):
    with pytest.raises(ValueError):
        bup.parse_profile_url(url)


def test_favorite_appid():
    assert bup.favorite_appid("https://store.steampowered.com/app/892970/Valheim/") == 892970
    with pytest.raises(ValueError):
        bup.favorite_appid("https://store.steampowered.com/search/?term=valheim")


# --- 명단 검증 -----------------------------------------------------------------

def write_subs(tmp_path, text):
    p = tmp_path / "subscribers.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_subscribers_ok(tmp_path):
    path = write_subs(tmp_path, """subscribers:
  - {id: owner, slack_user: U01ABC, profile_path: profile.json}
  - id: friend1
    slack_user: W02XYZ
    steam_profile: https://steamcommunity.com/id/robinwalker
    favorites: [https://store.steampowered.com/app/892970/Valheim/]
""")
    subs = bup.load_subscribers(path)
    assert [s["id"] for s in subs] == ["owner", "friend1"]
    assert subs[1]["favorite_appids"] == [892970]


@pytest.mark.parametrize("entry, why", [
    ("{id: Friend, slack_user: U01, favorites: [https://store.steampowered.com/app/1/]}", "id"),
    ("{id: a_b, slack_user: U01, favorites: [https://store.steampowered.com/app/1/]}", "id"),
    ("{id: a, slack_user: u01, favorites: [https://store.steampowered.com/app/1/]}", "slack_user"),
    ("{id: a, slack_user: U01}", "취향 재료"),
    ("{id: a, slack_user: U01, steam_profile: 'https://example.com/x'}", "steam_profile"),
    ("{id: a, slack_user: U01, favorites: ['https://example.com/app/1']}", "favorites"),
])
def test_load_subscribers_rejects_bad_entries(tmp_path, entry, why):
    with pytest.raises(ValueError, match=why):
        bup.load_subscribers(write_subs(tmp_path, f"subscribers:\n  - {entry}\n"))


def test_load_subscribers_rejects_duplicate_ids(tmp_path):
    e = "{id: a, slack_user: U01, favorites: [https://store.steampowered.com/app/1/]}"
    with pytest.raises(ValueError, match="중복"):
        bup.load_subscribers(write_subs(tmp_path, f"subscribers:\n  - {e}\n  - {e}\n"))


# --- 키가 들어가는 호출 --------------------------------------------------------

def test_steam_web_get_never_records_the_key():
    fake = FakeSteam({"Owned": [requests.ConnectionError(f"https://x/?key={KEY}"), Resp({}, 503), Resp({}, 503)]})
    errors = []
    assert bup.steam_web_get("IPlayerService/GetOwnedGames/v1/", {}, KEY, errors, get=fake, **NOSLEEP) is None
    assert len(fake.calls) == 1 + bup.MAX_RETRIES and fake.calls[0][1]["key"] == KEY
    assert KEY not in json.dumps(errors, ensure_ascii=False)
    assert "ConnectionError" in errors[0] and "HTTP 503" in errors[1]


def test_steam_web_get_does_not_retry_a_rejected_key():
    fake = FakeSteam({"Owned": [Resp({}, 401)]})
    errors = []
    assert bup.steam_web_get("IPlayerService/GetOwnedGames/v1/", {}, KEY, errors, get=fake, **NOSLEEP) is None
    assert len(fake.calls) == 1 and "HTTP 401" in errors[0]


def test_resolve_steamid_direct_and_vanity():
    assert bup.resolve_steamid("https://steamcommunity.com/profiles/" + STEAMID, KEY, get=None) == (STEAMID, None)
    fake = FakeSteam({"Vanity": [Resp({"response": {"success": 1, "steamid": STEAMID}})]})
    assert bup.resolve_steamid("https://steamcommunity.com/id/robinwalker", KEY, get=fake, **NOSLEEP) == (STEAMID, None)
    assert fake.calls[0][1]["vanityurl"] == "robinwalker"


def test_resolve_steamid_unknown_vanity_is_explained():
    fake = FakeSteam({"Vanity": [Resp({"response": {"success": 42, "message": "No match"}})]})
    steamid, problem = bup.resolve_steamid("https://steamcommunity.com/id/nobody", KEY, get=fake, **NOSLEEP)
    assert steamid is None and "찾지 못했다" in problem


def test_owned_games_private_library_is_explained():
    fake = FakeSteam({"Owned": [Resp({"response": {}})]})
    games, problem = bup.owned_games(STEAMID, KEY, get=fake, **NOSLEEP)
    assert games is None and "게임 세부 정보" in problem and "비공개" in problem


def test_owned_games_hidden_playtime_is_explained():
    fake = FakeSteam({"Owned": [Resp(owned((10, 0), (11, 0)))]})
    games, problem = bup.owned_games(STEAMID, KEY, get=fake, **NOSLEEP)
    assert games is None and "플레이 시간" in problem


# --- 시드 고르기 ---------------------------------------------------------------

def test_candidates_are_by_playtime_above_the_minimum():
    games = owned((10, 50), (11, 5000), (12, 300), (13, 900))["response"]["games"]
    assert bup.candidate_appids(games, {**RULES, "top_n": 1, "candidate_factor": 2}) == [11, 13]
    assert bup.candidate_appids(games, RULES) == [11, 13, 12]            # 60분 미만(10)은 뺀다


def test_pick_seeds_skips_software_then_adds_favorites():
    items = {99: item(99, [], type_=6), 10: item(10, [1]), 11: item(11, [1]), 12: item(12, [1]), 13: item(13, [1]),
             14: item(14, [1]), 50: item(50, [1])}
    seeds, skipped = bup.pick_seeds([99, 10, 11, 12, 13, 14], [50, 11], items, {**RULES, "top_n": 3})
    assert seeds == [10, 11, 12, 50]          # 소프트웨어(99) 빼고 3개 + 좋아하는 게임(11은 이미 있음)
    assert skipped == [99]


def test_pick_seeds_counts_missing_items_as_skipped():
    seeds, skipped = bup.pick_seeds([10, 11], [], {10: item(10, [1])}, RULES)
    assert seeds == [10] and skipped == [11]


# --- 한 사람 취향 만들기 ---------------------------------------------------------

LIBRARY = owned((99, 90000), (10, 3000), (11, 2000), (12, 1000), (13, 500), (14, 30))
ITEMS = items_body(item(99, [], type_=6, name="Wallpaper Engine"),
                   item(10, [6, 1, 2]), item(11, [1, 2, 3]), item(12, [1, 4, 5]), item(13, [2, 4]), item(50, [4, 5]))


def friend(**kw):
    return {"id": "friend1", "slack_user": "U01", "steam_profile": "https://steamcommunity.com/profiles/" + STEAMID,
            "favorite_appids": [50], **kw}


def steam(library=LIBRARY, items=ITEMS):
    return FakeSteam({"Owned": [Resp(library)], "GetItems": [Resp(items)]})


def test_build_one_makes_a_profile_from_library_and_favorites():
    profile, problems = bup.build_one(friend(), KEY, RULES, STOP, 5, TAGS, get=steam(), **NOSLEEP)
    assert problems == []
    assert [g["appid"] for g in profile["games"]] == [10, 11, 12, 13, 50]
    assert {t["name"]: t["games"] for t in profile["tags"]} == {"Puzzle": 3, "Cozy": 3, "Crafting": 3, "Survival": 2}
    assert profile["metrics"]["skipped_non_game"] == [99]
    g10 = profile["games"][0]
    assert g10["playtime_h"] == 50.0 and g10["favorite"] is False and "Multiplayer" not in g10["top_tags"]
    assert profile["games"][-1]["favorite"] is True
    assert "steamid" not in json.dumps(profile) and STEAMID not in json.dumps(profile)   # 개인 식별값은 안 남긴다


def test_build_one_favorites_only():
    fake = FakeSteam({"GetItems": [Resp(ITEMS)]})
    sub = {"id": "f", "slack_user": "U01", "favorite_appids": [10, 11, 12, 13]}
    profile, problems = bup.build_one(sub, None, RULES, STOP, 5, TAGS, get=fake, **NOSLEEP)
    assert problems == [] and len(profile["games"]) == 4
    assert all("Owned" not in url for url, _ in fake.calls)


def test_build_one_gate_blocks_a_thin_profile():
    profile, problems = bup.build_one(friend(favorite_appids=[]), KEY, {**RULES, "min_seed_games": 10},
                                      STOP, 5, TAGS, get=steam(), **NOSLEEP)
    assert profile is None and "시드 게임" in problems[0]


def test_build_one_without_key_does_not_fall_back_to_favorites_only():
    profile, problems = bup.build_one(friend(), None, RULES, STOP, 5, TAGS, get=steam(), **NOSLEEP)
    assert profile is None and "STEAM_API_KEY" in problems[0]


def test_build_one_private_library_is_a_problem_not_an_empty_profile():
    fake = FakeSteam({"Owned": [Resp({"response": {}})], "GetItems": [Resp(ITEMS)]})
    profile, problems = bup.build_one(friend(), KEY, RULES, STOP, 5, TAGS, get=fake, **NOSLEEP)
    assert profile is None and "비공개" in problems[0]


# --- main ----------------------------------------------------------------------

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STEAM_API_KEY", KEY)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (tmp_path / "seed_library.yaml").write_text(
        "profile_rules:\n  tags_per_game: 5\n  tag_stoplist: [Multiplayer]\n"
        "user_profile_rules:\n  top_n: 4\n  min_playtime_min: 60\n  candidate_factor: 3\n  large_seed_games: 30\n"
        "  min_tag_count: 3\n  min_tag_count_small: 2\n  min_seed_games: 3\n  min_profile_tags: 2\n",
        encoding="utf-8")
    (tmp_path / "subscribers.yaml").write_text(
        "subscribers:\n"
        "  - {id: owner, slack_user: U01, profile_path: profile.json}\n"
        f"  - {{id: friend1, slack_user: U02, steam_profile: 'https://steamcommunity.com/profiles/{STEAMID}',"
        " favorites: ['https://store.steampowered.com/app/50/']}\n", encoding="utf-8")
    return tmp_path


def full_steam(library=LIBRARY):
    return FakeSteam({"GetTagList": [Resp(taglist())], "Owned": [Resp(library)], "GetItems": [Resp(ITEMS)]})


def test_main_writes_profiles_and_run_status(repo, capsys):
    assert bup.main([], get=full_steam(), **NOSLEEP) == 0
    prof = json.loads((repo / "profiles" / "friend1.json").read_text(encoding="utf-8"))
    assert prof["subscriber"] == "friend1" and {t["name"] for t in prof["tags"]} >= {"Puzzle", "Cozy"}
    assert not (repo / "profiles" / "owner.json").exists()               # 운영자는 profile.json 그대로
    status = json.loads((repo / "profiles" / "_last_run.json").read_text(encoding="utf-8"))
    assert status["results"]["friend1"]["status"] == bup.ST_BUILT
    assert status["results"]["owner"]["status"] == bup.ST_FIXED


def test_main_keeps_the_last_profile_when_rebuild_fails(repo, capsys):
    bup.main([], get=full_steam(), **NOSLEEP)
    before = (repo / "profiles" / "friend1.json").read_text(encoding="utf-8")
    fake = FakeSteam({"GetTagList": [Resp(taglist())], "Owned": [Resp({"response": {}})], "GetItems": [Resp(ITEMS)]})
    assert bup.main([], get=fake, **NOSLEEP) == 1                           # 조용히 넘어가지 않는다
    assert (repo / "profiles" / "friend1.json").read_text(encoding="utf-8") == before
    status = json.loads((repo / "profiles" / "_last_run.json").read_text(encoding="utf-8"))["results"]["friend1"]
    assert status["status"] == bup.ST_STALE and "비공개" in status["problems"][0]


def test_main_without_any_profile_reports_no_profile(repo, capsys):
    fake = FakeSteam({"GetTagList": [Resp(taglist())], "Owned": [Resp({"response": {}})], "GetItems": [Resp(ITEMS)]})
    assert bup.main([], get=fake, **NOSLEEP) == 1
    status = json.loads((repo / "profiles" / "_last_run.json").read_text(encoding="utf-8"))["results"]["friend1"]
    assert status["status"] == bup.ST_NONE


def test_main_dry_run_and_only(repo, capsys):
    assert bup.main(["--only", "friend1", "--dry-run"], get=full_steam(), **NOSLEEP) == 0
    assert not (repo / "profiles").exists()
    assert "Puzzle" in capsys.readouterr().out


def test_main_unknown_only_id_fails(repo):
    with pytest.raises(SystemExit):
        bup.main(["--only", "nobody"], get=full_steam(), **NOSLEEP)


def test_main_never_writes_or_prints_the_key(repo, capsys):
    fake = FakeSteam({"GetTagList": [Resp(taglist())], "Owned": [requests.ConnectionError(f"u?key={KEY}")],
                      "GetItems": [Resp(ITEMS)]})
    bup.main([], get=fake, **NOSLEEP)
    assert KEY not in capsys.readouterr().out
    for f in (repo / "profiles").iterdir():
        assert KEY not in f.read_text(encoding="utf-8")


# --- 코드 리뷰 반영 (2026-09-11) --------------------------------------------------

def test_items_failure_keeps_only_the_error_kind():
    # collect.call_api 오류엔 요청 URL(= 라이브러리 appid 목록)이 들어 있다 — 커밋되는 기록에 남기지 않는다
    fake = FakeSteam({"Owned": [Resp(LIBRARY)], "GetItems": [Resp({}, 503)]})
    profile, problems = bup.build_one(friend(), KEY, RULES, STOP, 5, TAGS, get=fake, **NOSLEEP)
    assert profile is None and problems == ["상점 정보(GetItems) 호출 실패 — HTTPError"]


def test_min_profile_tags_gate():
    profile, problems = bup.build_one(friend(), KEY, {**RULES, "min_profile_tags": 10}, STOP, 5, TAGS,
                                      get=steam(), **NOSLEEP)
    assert profile is None and "취향 태그" in problems[0]


def test_steam_web_get_retries_rate_limit_and_non_json():
    fake = FakeSteam({"Owned": [Resp({}, 429), Resp(ValueError("html")), Resp({"response": {}})]})
    errors = []
    assert bup.steam_web_get(bup.OWNED_PATH, {}, KEY, errors, get=fake, **NOSLEEP) == {"response": {}}
    assert len(fake.calls) == 3 and "HTTP 429" in errors[0] and "JSON" in errors[1]


def test_load_subscribers_rejects_non_text_profile_path(tmp_path):
    with pytest.raises(ValueError, match="profile_path"):
        bup.load_subscribers(write_subs(tmp_path, "subscribers:\n  - {id: a, slack_user: U01, profile_path: true}\n"))


def test_main_tag_list_failure_is_reported_per_person(repo, capsys):
    fake = FakeSteam({"GetTagList": [Resp({}, 503)], "Owned": [Resp(LIBRARY)], "GetItems": [Resp(ITEMS)]})
    assert bup.main([], get=fake, **NOSLEEP) == 1
    status = json.loads((repo / "profiles" / "_last_run.json").read_text(encoding="utf-8"))["results"]["friend1"]
    assert status["status"] == bup.ST_NONE and "GetTagList" in status["problems"][0]


def test_main_without_key_fails_steam_subscribers(repo, monkeypatch, capsys):
    monkeypatch.delenv("STEAM_API_KEY")
    assert bup.main([], get=full_steam(), **NOSLEEP) == 1
    status = json.loads((repo / "profiles" / "_last_run.json").read_text(encoding="utf-8"))["results"]["friend1"]
    assert "STEAM_API_KEY" in status["problems"][0]


def test_main_records_no_library_details_on_failure(repo, capsys):
    fake = FakeSteam({"GetTagList": [Resp(taglist())], "Owned": [Resp(LIBRARY)], "GetItems": [Resp({}, 503)]})
    bup.main([], get=fake, **NOSLEEP)
    text = (repo / "profiles" / "_last_run.json").read_text(encoding="utf-8")
    assert KEY not in text and "input_json" not in text and "appid" not in text


def test_dry_run_can_measure_the_owner_from_steam(repo, capsys):
    # 운영자는 평소 profile_path(고정) — --dry-run이면 스팀으로 만들어 출력만 한다(기준값 재기)
    (repo / "subscribers.yaml").write_text(
        f"subscribers:\n  - {{id: owner, slack_user: U01, profile_path: profile.json,"
        f" steam_profile: 'https://steamcommunity.com/profiles/{STEAMID}'}}\n", encoding="utf-8")
    assert bup.main(["--only", "owner", "--dry-run"], get=full_steam(), **NOSLEEP) == 0
    assert "Puzzle" in capsys.readouterr().out and not (repo / "profiles").exists()
    assert bup.main(["--only", "owner"], get=full_steam(), **NOSLEEP) == 0      # 평소엔 고정
    assert not (repo / "profiles" / "owner.json").exists()


# --- 시드 수에 따른 min_tag_count (2026-09-11 사람 결정) ---------------------------

def test_min_tag_count_depends_on_seed_count():
    assert bup.min_tag_count_for(29, RULES) == 2 and bup.min_tag_count_for(30, RULES) == 3


def test_build_one_uses_the_stricter_count_for_a_large_seed_set():
    # 시드 5개 — large_seed_games를 5로 낮추면 min 3이 적용돼 게임 2개에만 나온 Survival이 빠진다
    profile, problems = bup.build_one(friend(), KEY, {**RULES, "large_seed_games": 5}, STOP, 5, TAGS,
                                      get=steam(), **NOSLEEP)
    assert problems == [] and profile["rules"]["applied_min_tag_count"] == 3
    assert {t["name"] for t in profile["tags"]} == {"Puzzle", "Cozy", "Crafting"}


def test_excluded_tags_never_enter_a_profile():
    # 2026-09-11 사람 결정 — 성인 콘텐츠 태그는 취향에 안 넣는다. 제외목록처럼 먼저 빼서 다른 태그가 자리를 채운다
    profile, problems = bup.build_one(friend(), KEY, {**RULES, "exclude_tags": ["Cozy"]}, STOP, 5, TAGS,
                                      get=steam(), **NOSLEEP)
    assert problems == [] and "Cozy" not in {t["name"] for t in profile["tags"]}
    assert all("Cozy" not in g["top_tags"] for g in profile["games"])
    assert "Cozy" in profile["rules"]["tag_stoplist"]
