"""
test_collect.py — collect.py의 로직만 가짜 응답으로 확인한다. 네트워크는 부르지 않는다.

실행:  python -m pytest test_collect.py -q
"""

import datetime
import json

import pytest
import requests

import collect

KST = collect.KST
NOW = datetime.datetime(2026, 9, 10, 8, 0, tzinfo=KST)          # 수집 시각: 한국 아침 8시
WIN_START = int(datetime.datetime(2026, 9, 9, tzinfo=KST).timestamp())
IN = WIN_START + 3600                                            # 어제 01:00 KST
BEFORE = WIN_START - 3600                                        # 그저께 23:00 KST
NO_KEY, NULL = "no-key", "null"                                  # 출시일 결측 흉내
DAY = 86400


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    """실제 raw/ 폴더를 읽지 않게 — 추적 기능은 이전 수집분을 읽는다."""
    monkeypatch.chdir(tmp_path)


class FakeResp:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status}")

    def json(self):
        return self.payload


class FakeSteam:
    """출시일 최신순 카탈로그 하나를 들고, Query는 그 구간을 잘라 준다(appid = 카탈로그 위치).

    malformed: 경로별로 HTTP 200에 모양만 다른 본문을 준다.
    fail_items_from / flaky_items_from: 첫 appid로 배치를 지정 — 계속 실패 / 한 번만 실패.
    drop_items / not_success: GetItems가 빼먹는 appid / success=0으로 주는 appid.
    """

    def __init__(self, catalog, fail_paths=(), malformed=None, fail_items_from=(), flaky_items_from=(),
                 drop_items=(), not_success=()):
        self.catalog = catalog
        self.fail_paths = set(fail_paths)
        self.malformed = malformed or {}
        self.fail_items_from = set(fail_items_from)
        self.flaky_items_from = set(flaky_items_from)
        self.flaked = set()
        self.drop_items = set(drop_items)
        self.not_success = set(not_success)
        self.calls = []

    @staticmethod
    def item(appid, ts):
        if ts == NO_KEY:
            return {"appid": appid}
        if ts == NULL:
            return {"appid": appid, "release": None}
        return {"appid": appid, "release": {"steam_release_date": ts}}

    def __call__(self, url, params=None, timeout=None):
        path = url.replace(f"{collect.API}/", "")
        self.calls.append(path)
        if path in self.fail_paths:
            return FakeResp(status=503)
        if path in self.malformed:
            return FakeResp(self.malformed[path])
        payload = json.loads(params["input_json"]) if "input_json" in params else {}

        if path == collect.QUERY_PATH:
            s, c = payload["query"]["start"], payload["query"]["count"]
            items = [self.item(i, ts) for i, ts in enumerate(self.catalog)][s:s + c]
            return FakeResp({"response": {"store_items": items}})

        if path == collect.ITEMS_PATH:
            # 실패는 호출 순번이 아니라 배치 내용(첫 appid)으로 정한다. 순번으로 정하면 재시도가
            # 다음 순번이 되어 성공해 버린다 — 2026-09-10 첫 실행에서 테스트가 그렇게 틀렸다.
            first = payload["ids"][0]["appid"]
            if first in self.fail_items_from:
                return FakeResp(status=500)
            if first in self.flaky_items_from and first not in self.flaked:
                self.flaked.add(first)
                return FakeResp(status=500)
            items = [{"appid": i["appid"], "success": 0 if i["appid"] in self.not_success else 1}
                     for i in payload["ids"] if i["appid"] not in self.drop_items]
            return FakeResp({"response": {"store_items": items}})

        if path == collect.TAGLIST_PATH:
            return FakeResp({"response": {"tags": [{"tagid": 9, "name": "Strategy"}]}})

        raise AssertionError(f"예상 못 한 경로 {path}")


def run(fake, **kw):
    return collect.collect(NOW, get=fake, sleep=lambda s: None, **kw)


def requested(doc):
    return [a for b in doc["items_batches"] for a in b["appids"]]


def write_previous(tmp_path, name, window_start, in_window_ids, out_of_window_ids=(), not_ok=()):
    """이전 수집분 흉내. window 안 · 밖 출시작을 목록에 넣고, 상세(items_batches)도 채운다.
    not_ok: 그날 상세 조회가 실패(success 0)한 appid."""
    items = [{"appid": a, "release": {"steam_release_date": window_start + 3600}} for a in in_window_ids]
    items += [{"appid": a, "release": {"steam_release_date": window_start - 3600}} for a in out_of_window_ids]
    details = [{"appid": a, "success": 0 if a in not_ok else 1, "type": 0} for a in [*in_window_ids, *out_of_window_ids]]
    doc = {"meta": {"window_unix": [window_start, window_start + DAY]},
           "query_pages": [{"start": 0, "body": {"response": {"store_items": items}}}],
           "items_batches": [{"appids": [d["appid"] for d in details], "body": {"response": {"store_items": details}}}]}
    (tmp_path / "raw").mkdir(exist_ok=True)
    (tmp_path / "raw" / name).write_text(json.dumps(doc), encoding="utf-8")


# --- 날짜 경계 -----------------------------------------------------------------

def test_window_is_previous_kst_day():
    start, end = collect.target_window(NOW)
    assert start == datetime.datetime(2026, 9, 9, tzinfo=KST)
    assert end == datetime.datetime(2026, 9, 10, tzinfo=KST)


def test_window_uses_kst_not_utc_just_after_midnight():
    # KST 00:30 = UTC 전날 15:30. UTC로 잡으면 그저께가 대상이 된다.
    just_after = datetime.datetime(2026, 9, 10, 0, 30, tzinfo=KST)
    start, _ = collect.target_window(just_after.astimezone(datetime.timezone.utc))
    assert start.date() == datetime.date(2026, 9, 9)


# --- 목록 넘기기 ---------------------------------------------------------------

def test_stops_only_when_whole_page_is_before_window():
    doc = run(FakeSteam([IN] * 100 + [BEFORE] * 200))
    # start 0, 40, 80(80~99가 어제라 계속), 120(전부 그저께) → 4페이지
    assert [p["start"] for p in doc["query_pages"]] == [0, 40, 80, 120]
    assert doc["meta"]["query_stop_reason"] == collect.STOP_REACHED
    assert doc["meta"]["window_covered"] is True
    # 경계 페이지의 그저께 출시작도 버리지 않는다 — 필터는 QA 몫
    assert set(range(100)) <= set(requested(doc))


def test_single_out_of_order_item_does_not_stop_paging():
    # 첫 페이지에 그저께 출시작 하나가 섞여 있다. min 기준으로 멈추면 10~99의 어제 출시작을 못 받는다.
    catalog = [IN] * 10 + [BEFORE] + [IN] * 89 + [BEFORE] * 200
    doc = run(FakeSteam(catalog))
    in_window = {i for i, ts in enumerate(catalog) if ts == IN}
    assert in_window <= set(requested(doc))
    assert doc["meta"]["window_covered"] is True


def test_pages_overlap_so_boundary_items_are_fetched_twice():
    doc = run(FakeSteam([IN] * 100 + [BEFORE] * 200))
    first, second = (collect.page_items(p["body"]) for p in doc["query_pages"][:2])
    overlap = collect.PAGE_SIZE - collect.PAGE_STRIDE
    assert overlap == 10
    assert [i["appid"] for i in first[-overlap:]] == [i["appid"] for i in second[:overlap]]
    # 요청은 한 번씩만 — 원본 페이지에는 겹친 그대로 남는다
    assert len(requested(doc)) == len(set(requested(doc)))


def test_page_cap_means_window_not_covered():
    doc = run(FakeSteam([IN] * 1000), budget_limit=100)
    assert doc["meta"]["query_pages"] == collect.MAX_QUERY_PAGES
    assert doc["meta"]["window_covered"] is False
    assert collect.exit_code(doc["meta"]) == 1


def test_missing_release_dates_do_not_stop_paging():
    # 첫 페이지 50개가 전부 출시일 결측이어도 멈추면 안 된다. 키 없음 / null / 0 세 가지.
    catalog = [NO_KEY] * 20 + [NULL] * 20 + [0] * 10 + [IN] * 30 + [BEFORE] * 200
    doc = run(FakeSteam(catalog))
    assert doc["meta"]["window_covered"] is True
    assert set(range(50, 80)) <= set(requested(doc))


def test_empty_page_means_window_not_covered():
    doc = run(FakeSteam([IN] * 60))
    assert doc["meta"]["query_stop_reason"] == "빈 페이지"
    assert doc["meta"]["window_covered"] is False
    assert collect.exit_code(doc["meta"]) == 1


# --- HTTP 200인데 모양이 다른 응답 ---------------------------------------------

def test_malformed_200_query_is_a_failure_not_an_empty_page():
    # Steam은 잘못된 input_json에 {"response": {}}를 준다. 빈 페이지로 넘기면 0건 수집에 exit 0이 된다.
    doc = run(FakeSteam([IN] * 100, malformed={collect.QUERY_PATH: {"response": {}}}))
    assert doc["meta"]["failed_calls"] >= 1
    assert doc["meta"]["window_covered"] is False
    assert any("응답 형식 이상" in e["error"] for e in doc["meta"]["errors"])
    assert collect.exit_code(doc["meta"]) == 1


@pytest.mark.parametrize("body", [None, [], "oops", {"response": None}, {"response": {"tags": "x"}}])
def test_malformed_taglist_bodies_are_recorded(body):
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50, malformed={collect.TAGLIST_PATH: body}))
    assert doc["tag_list"] is None
    assert doc["meta"]["failed_calls"] == 1
    assert len(doc["meta"]["errors"]) == 1 + collect.MAX_RETRIES


# --- 상세 일괄 호출 ------------------------------------------------------------

def test_items_are_requested_in_batches_of_50():
    doc = run(FakeSteam([IN] * 120 + [BEFORE] * 100))
    # 페이지 0·40·80·120(120~169 전부 그저께) → 고유 appid 0~169 = 170개
    assert [len(b["appids"]) for b in doc["items_batches"]] == [50, 50, 50, 20]


def test_failed_batch_is_kept_with_its_appids():
    doc = run(FakeSteam([IN] * 50 + [BEFORE] * 100, fail_items_from={0}), budget_limit=100)
    first = doc["items_batches"][0]
    assert first["body"] is None and len(first["appids"]) == 50       # 무엇이 비었는지 남는다
    assert doc["items_batches"][1]["body"] is not None                  # 다음 배치는 계속 받는다
    assert doc["meta"]["failed_calls"] == 1
    assert doc["meta"]["items_missing"] == 50
    assert collect.exit_code(doc["meta"]) == 1


def test_transient_failure_recovers_on_retry_and_leaves_a_trace():
    doc = run(FakeSteam([IN] * 50 + [BEFORE] * 100, flaky_items_from={0}), budget_limit=100)
    assert doc["items_batches"][0]["body"] is not None                 # 재시도로 복구됐다
    assert [e["attempt"] for e in doc["meta"]["errors"]] == [0]         # 흔적은 남는다
    assert collect.exit_code(doc["meta"]) == 0                          # 그래서 종료 코드는 0


def test_items_missing_and_not_success_are_counted():
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50, drop_items={3}, not_success={4, 5}))
    assert doc["meta"]["items_missing"] == 1
    assert doc["meta"]["items_not_success"] == 2
    assert collect.exit_code(doc["meta"]) == 0      # 데이터 품질 판정은 QA 몫 — 수집기는 세기만 한다


# --- 추적 (지난 7일 수집분 다시 받기) -------------------------------------------

def test_tracks_previous_collections_target_releases(tmp_path):
    write_previous(tmp_path, "2026-09-09.json", WIN_START - DAY, [9000, 9001, 9002], out_of_window_ids=[9100])
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50))
    tracked = [a for b in doc["tracked_batches"] for a in b["appids"]]
    assert tracked == [9000, 9001, 9002]                    # 그 수집분의 대상일 출시작만 — 9100은 아님
    assert doc["meta"]["tracked_from"] == ["2026-09-09.json"]
    assert doc["meta"]["tracked_missing"] == 0


def test_tracking_only_reads_dated_raw_files_within_window(tmp_path):
    # appid는 9000번대 — 가짜 카탈로그의 오늘 목록(0~59)과 겹치면 "오늘 목록에 있으면 추적 안 함" 규칙에
    # 걸려 빠진다. 2026-09-10 첫 작성 때 5를 써서 테스트가 틀렸다(코드는 설계대로 5를 뺐다).
    write_previous(tmp_path, "2026-09-02.json", WIN_START - 8 * DAY, [9001])   # 8일 전 — 범위 밖
    write_previous(tmp_path, "2026-09-10.json", WIN_START, [9002])             # 오늘 — 이전 수집분 아님
    write_previous(tmp_path, "2026-09-09.conflict-081500.json", WIN_START - DAY, [9003])
    write_previous(tmp_path, "seed_items_2026-09-09.json", WIN_START - DAY, [9004])
    write_previous(tmp_path, "2026-09-03.json", WIN_START - 7 * DAY, [9005])   # 7일 전 — 범위 안
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50))
    assert [a for b in doc["tracked_batches"] for a in b["appids"]] == [9005]
    assert doc["meta"]["tracked_from"] == ["2026-09-03.json"]


def test_tracking_skips_appids_already_in_todays_list(tmp_path):
    write_previous(tmp_path, "2026-09-09.json", WIN_START - DAY, [0, 1, 9000])  # 0·1은 오늘 목록에도 있다
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50))
    assert [a for b in doc["tracked_batches"] for a in b["appids"]] == [9000]


def test_unreadable_previous_raw_is_recorded_not_raised(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "2026-09-08.json").write_text("{깨진 json", encoding="utf-8")
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50))
    assert [u["path"] for u in doc["meta"]["track_unreadable"]] == ["2026-09-08.json"]
    assert doc["meta"]["window_covered"] is True             # 신작 수집은 영향 없음


def test_failed_tracked_batch_counts_as_failed_call(tmp_path):
    write_previous(tmp_path, "2026-09-09.json", WIN_START - DAY, [9000, 9001])
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50, fail_items_from={9000}), budget_limit=100)
    assert doc["meta"]["tracked_missing"] == 2
    assert doc["meta"]["failed_calls"] == 1
    assert doc["meta"]["failed_calls_tracked"] == 1          # qa C0는 이걸 빼고 오늘 적재 여부를 본다
    assert collect.exit_code(doc["meta"]) == 1               # 수집기는 알린다 — 오늘 적재 여부는 qa가 정한다


def test_tracking_skips_appids_that_failed_on_their_own_day(tmp_path):
    # 그날부터 조회가 안 되던 게임을 추적하면 7일 내내 V3-3이 실패한다
    write_previous(tmp_path, "2026-09-09.json", WIN_START - DAY, [9000, 9001], not_ok={9001})
    doc = run(FakeSteam([IN] * 10 + [BEFORE] * 50))
    assert [a for b in doc["tracked_batches"] for a in b["appids"]] == [9000]


def test_tracking_is_fetched_last_so_it_starves_first(tmp_path):
    write_previous(tmp_path, "2026-09-09.json", WIN_START - DAY, [9000])
    fake = FakeSteam([IN] * 10 + [BEFORE] * 50)
    run(fake)
    assert fake.calls[-1] == collect.ITEMS_PATH and fake.calls[-2] == collect.TAGLIST_PATH


# --- 실패와 예산 ---------------------------------------------------------------

def test_total_outage_is_recorded_not_raised():
    doc = run(FakeSteam([], fail_paths={collect.QUERY_PATH, collect.TAGLIST_PATH}))
    assert doc["query_pages"] == []
    assert doc["meta"]["query_stop_reason"] == "1번째 페이지 호출 실패"
    assert doc["tag_list"] is None
    assert len(doc["meta"]["errors"]) == 2 * (1 + collect.MAX_RETRIES)
    assert doc["meta"]["failed_calls"] == 2


def test_budget_caps_calls_including_retries():
    fake = FakeSteam([], fail_paths={collect.QUERY_PATH, collect.TAGLIST_PATH})
    doc = run(fake, budget_limit=2)
    assert len(fake.calls) == 2
    assert doc["meta"]["calls_used"] == 2
    assert any(e["error"] == "호출 예산 소진" for e in doc["meta"]["errors"])


# --- 종료 코드 · 원본 저장 ------------------------------------------------------

@pytest.mark.parametrize("failed, covered, code", [(0, True, 0), (1, True, 1), (0, False, 1), (2, False, 1)])
def test_exit_code_mapping(failed, covered, code):
    assert collect.exit_code({"failed_calls": failed, "window_covered": covered}) == code


def test_write_raw_refuses_to_overwrite(tmp_path):
    path = str(tmp_path / "raw" / "2026-09-10.json")
    collect.write_raw({"a": 1}, path)
    with pytest.raises(FileExistsError):
        collect.write_raw({"a": 2}, path)
    assert json.load(open(path, encoding="utf-8")) == {"a": 1}


def today():
    return f"{datetime.datetime.now(KST).date():%Y-%m-%d}"


def test_main_writes_raw_and_returns_0_on_clean_run(tmp_path, monkeypatch, capsys):
    good = run(FakeSteam([IN] * 30 + [BEFORE] * 50))
    monkeypatch.setattr(collect, "collect", lambda now: good)
    assert collect.main() == 0
    assert json.load(open(tmp_path / "raw" / f"{today()}.json", encoding="utf-8"))["meta"]["window_covered"]


def test_main_returns_1_when_window_not_covered(tmp_path, monkeypatch, capsys):
    partial = run(FakeSteam([IN] * 60))                     # 빈 페이지로 끝남
    monkeypatch.setattr(collect, "collect", lambda now: partial)
    assert collect.main() == 1
    assert (tmp_path / "raw" / f"{today()}.json").exists()   # 그래도 원본은 남는다


def test_main_stops_before_calling_when_todays_raw_exists(tmp_path, monkeypatch):
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / f"{today()}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(collect, "collect", lambda *a, **k: pytest.fail("호출하면 안 된다"))
    assert collect.main() == 2


def test_main_keeps_doc_when_another_run_wins_the_race(tmp_path, monkeypatch, capsys):
    good = run(FakeSteam([IN] * 30 + [BEFORE] * 50))

    def collect_while_another_run_writes(now):
        (tmp_path / "raw").mkdir(exist_ok=True)
        (tmp_path / "raw" / f"{today()}.json").write_text('{"winner": true}', encoding="utf-8")
        return good

    monkeypatch.setattr(collect, "collect", collect_while_another_run_writes)
    assert collect.main() == 2
    assert json.load(open(tmp_path / "raw" / f"{today()}.json", encoding="utf-8")) == {"winner": True}
    conflicts = list((tmp_path / "raw").glob(f"{today()}.conflict-*.json"))
    assert len(conflicts) == 1                                # 받은 문서는 버리지 않았다
