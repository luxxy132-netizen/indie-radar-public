"""
report.py — 다음 작업 ⑨: 지난 7일 신작을 취향 프로파일과 맞춰 주간 추천을 만들고 슬랙으로 보낸다.

규칙은 seed_library.yaml의 match_rules (2026-09-10 사람과 함께 정함, 처음 2주는 잠정)
  1) 신작 태그는 가중치순 상위 tags_per_new_game개만 본다 — profile_rules.tag_stoplist를 먼저 뺀 뒤
  2) 점수 = 겹친 취향 태그마다 log(그 주 신작 수 ÷ 그 태그가 붙은 신작 수)의 합. 흔한 태그일수록 0에 가깝다
  3) 그 주 신작의 rare_max_ratio 이하에만 붙는 취향 태그가 min_rare_tags개 이상 겹치면 기준 통과
  4) 통과한 것 중 점수순 최대 weekly_max개. 약한 주엔 억지로 채우지 않는다

재료   data/{대상일}.json 7일치(qa가 통과시켜 적재한 것) + 리뷰는 발송일 원본 raw/{발송일}.json의 최신값
받는 사람  subscribers.yaml — 사람마다 자기 취향(profile_path 또는 profiles/{id}.json)으로 골라 슬랙 개인 DM
       (2026-09-11 사람과 정함: 채널 발송은 끔). 취향 파일은 build_user_profiles.py가 바로 앞 단계에서 갱신한다
산출   report/{시작일}_{종료일}_{id}.json · .md 사람마다 + report/{시작일}_{종료일}-summary.md 전체 요약
       (덮어쓰지 않는다 — 다시 돌리면 -2, -3 …)
발송   환경변수 SLACK_BOT_TOKEN (GitHub Secrets, chat:write). 없으면 파일만 저장하고 경고한다

실행:  python report.py [--date YYYY-MM-DD] [--force]
       --date   발송일로 칠 날짜(KST). 기본은 오늘. 대상은 그 전날까지 7일
       --force  이미 보낸 주도 다시 보낸다
종료 코드: 0 정상(토큰 없음 · 이미 보냄 포함) / 1 누구라도 전송 실패 · 취향 파일 없음 · 명단 비었음 · 7일 전부 적재 없음
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter

import requests
import yaml

import collect
from build_user_profiles import LAST_RUN_PATH, ST_STALE, load_subscribers, profile_file
from peek import SEED_LIBRARY_PATH
from qa import free_stem, release_fields

DATA_DIR = "data"
REPORT_DIR = "report"
PROFILE_PATH = "profile.json"
WINDOW_DAYS = 7
MAX_RETRIES = 2
SLEEP_SEC = 2
TIMEOUT_SEC = 30
SLACK_POST_URL = "https://slack.com/api/chat.postMessage"
PAUSE_SEC = 1            # 사람 사이 쉬는 시간 — chat.postMessage는 채널(DM)마다 초당 1건 안팎
MAX_RETRY_AFTER = 30     # 속도 제한 때 Retry-After를 이만큼까지만 기다린다
WEEKDAYS = "월화수목금토일"
# 슬랙 한 메시지 블록 한도 50. 제목 · 목록 · 구분선 3 + 게임마다 이미지 · 카드 2 + 두 게임마다 구분선 + 꼬리 1
# → 18개면 48블록. 그보다 많이 고르게 바꾸면 슬랙이 메시지 전체를 거절하므로 설정을 읽을 때 막는다
MAX_WEEKLY_PICKS = 18


# --- 입력 ----------------------------------------------------------------------

def load_rules(path=SEED_LIBRARY_PATH):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    rules = cfg["match_rules"]
    if not 1 <= rules["weekly_max"] <= MAX_WEEKLY_PICKS:
        raise ValueError(f"match_rules.weekly_max는 1~{MAX_WEEKLY_PICKS}여야 한다(슬랙 블록 50개 한도): {rules['weekly_max']}")
    return rules, cfg["profile_rules"]["tag_stoplist"]


def load_exclude_tags(path=SEED_LIBRARY_PATH):
    """누구의 취향 태그에도 넣지 않는 태그(user_profile_rules.exclude_tags). 운영자 profile.json은 시드 지문 때문에
    다시 만들지 않고 여기서 읽을 때 뺀다."""
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return list((cfg.get("user_profile_rules") or {}).get("exclude_tags") or [])


def target_days(run_day):
    """발송일 전날까지 7일, 오래된 순. 월요일 발송이면 지난주 월~일."""
    return [run_day - datetime.timedelta(days=i) for i in range(WINDOW_DAYS, 0, -1)]


def load_week(days, data_dir=DATA_DIR):
    """7일치 적재 파일. (게임 목록, 읽은 날, 없는 날, 읽지 못한 파일, 적재됐지만 0개인 날). 같은 appid는 처음 본 것만."""
    games, loaded, missing, unreadable, empty = {}, [], [], [], []
    for d in days:
        path = os.path.join(data_dir, f"{d:%Y-%m-%d}.json")
        if not os.path.exists(path):
            missing.append(d)
            continue
        try:
            with open(path, encoding="utf-8") as f:
                day_doc = json.load(f)
            records = day_doc["games"]
            if not isinstance(records, list):
                raise TypeError("games가 목록이 아니다")
        except (OSError, ValueError, KeyError, TypeError) as e:
            unreadable.append({"day": f"{d:%Y-%m-%d}", "error": f"{type(e).__name__}: {e}"})
            missing.append(d)
            continue
        loaded.append(d)
        if not records:
            empty.append(d)                 # 적재는 됐는데 0개 — 조용히 "읽은 날"로만 세지 않는다
        for r in records:
            if isinstance(r, dict) and r.get("appid") is not None:
                # 어느 원본에서 적재됐는지 붙여 둔다 — 옛 적재분의 빠진 필드를 원본에서 읽을 때 쓴다(파일은 안 고친다)
                games.setdefault(r["appid"], {**r, "_source_raw": day_doc.get("source_raw")})
    return list(games.values()), loaded, missing, unreadable, empty


def latest_reviews(run_day, raw_dir=collect.RAW_DIR):
    """발송일 원본의 신작 · 추적 상세에서 appid별 (리뷰 수, 긍정 %). 원본이 없으면 빈 dict."""
    path = os.path.join(raw_dir, f"{run_day:%Y-%m-%d}.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    out = {}
    for key in ("items_batches", "tracked_batches"):
        for b in doc.get(key) or []:
            for it in collect.page_items(b.get("body")):
                rv = (it.get("reviews") or {}).get("summary_filtered") or {}
                if isinstance(rv.get("review_count"), int):
                    out[it.get("appid")] = (rv["review_count"], rv.get("percent_positive"))
    return out


def raw_release_objects(path):
    """원본 하나의 신작 상세에서 appid별 release 객체. 못 읽으면 빈 dict."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {}
    return {it.get("appid"): it.get("release")
            for b in doc.get("items_batches") or [] for it in collect.page_items(b.get("body"))}


def release_history(games):
    """appid별 (첫 출시일, 얼리 액세스 졸업 여부). 적재 파일에 없으면(2026-09-10 이전 적재분)
    그 파일을 만든 원본(source_raw)에서 읽는다 — 적재 파일은 고치지 않는다."""
    out, cache = {}, {}
    for g in games:
        if "ea_graduated" in g:
            out[g.get("appid")] = (g.get("original_release_kst"), bool(g.get("ea_graduated")))
            continue
        src = g.get("_source_raw")
        if src not in cache:
            cache[src] = raw_release_objects(src)
        release = cache[src].get(g.get("appid"))
        out[g.get("appid")] = release_fields(release) if release is not None else (None, False)
    return out


# --- 점수 ----------------------------------------------------------------------

def score_week(games, profile_tags, stoplist, rules):
    """(기준 통과 목록 — 점수순, 그 주의 드문 취향 태그). 드문 정도는 그 주 신작 전체를 분모로 센다."""
    stop, prof = set(stoplist), set(profile_tags)
    n = rules["tags_per_new_game"]
    # 제외목록을 먼저 빼고 상위 n개 — 거꾸로 하면 제외 태그가 자리만 차지한다(④ 실측)
    tags = {g["appid"]: [t for t in g.get("tags") or [] if t not in stop][:n] for g in games}
    total = len(games)
    df = Counter(t for ts in tags.values() for t in set(ts))
    rare = {t for t in prof if df[t] and df[t] / total <= rules["rare_max_ratio"]}
    scored = []
    for g in games:
        hit = [t for t in tags[g["appid"]] if t in prof]
        rare_hit = [t for t in hit if t in rare]
        scored.append({
            "game": g,
            "score": round(sum(math.log(total / df[t]) for t in hit), 3),
            "matched": hit,
            "rare": rare_hit,
            "passed": len(rare_hit) >= rules["min_rare_tags"],
        })
    passed = sorted((s for s in scored if s["passed"]), key=lambda s: (-s["score"], s["game"].get("name") or ""))
    return passed, sorted(rare)


# --- 메시지 --------------------------------------------------------------------

def esc(text):
    """슬랙 mrkdwn에서 특수문자 — 게임 이름에 < > & 가 들어가면 링크가 깨진다."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def month_day(d):
    return f"{d.month}/{d.day}"


def month_day_weekday(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def price_text(g):
    if g.get("is_free"):
        return "무료"
    price = g.get("price")
    if not price:
        return "가격 없음"
    pct = g.get("discount_pct") or 0
    if pct and g.get("price_original"):
        return f"{g['price_original']} → {price} (-{pct}%)"
    if pct:
        return f"{price} (-{pct}%)"        # 할인 전 가격이 없는 적재분(2026-09-10 이전)
    return price


def review_text(g, reviews):
    latest = reviews.get(g.get("appid"))
    if latest:
        count, pct, suffix = latest[0], latest[1], ""
    else:
        count, pct, suffix = g.get("review_count"), g.get("percent_positive"), " (출시일 기준)"
    if not count:
        return "아직 리뷰 없음" + suffix
    return f"리뷰 {count:,}개 (긍정 {pct}%)" + suffix


def release_text(g):
    try:
        d = datetime.date.fromisoformat((g.get("release_kst") or "")[:10])
    except ValueError:
        return "출시일 모름"
    return f"{month_day(d)} 출시"


def history_text(g, history):
    """얼리 액세스 졸업작 · 재출시작 표시. 사람이 보고 판단하도록 남기되 새 게임이 아님을 밝힌다(2026-09-10 결정)."""
    original, graduated = history.get(g.get("appid"), (None, False))
    when = None
    if original:
        first = datetime.date.fromisoformat(original)
        same_year = (g.get("release_kst") or "")[:4] == str(first.year)
        when = f"첫 출시 {first.month}/{first.day}" if same_year else f"첫 출시 {first.year}년"
    if graduated:
        return "얼리 액세스 졸업" + (f" ({when})" if when else "")
    return when


def min_pool(rules):
    """드문 태그가 나올 수 있는 최소 신작 수 — 한 게임에만 붙어도 비율이 rare_max_ratio를 넘으면 불가능하다."""
    return math.ceil(1 / rules["rare_max_ratio"] - 1e-9)


def status_line(loaded, missing, empty, pool_n, passed_n, picks_n):
    """운영 상태 한 줄 — 슬랙엔 안 보내고 리포트 .md · 실행 요약에만 남긴다."""
    days = lambda ds: ", ".join(month_day(d) for d in ds) or "없음"
    return (f"기록: 신작 {pool_n}개 · 기준 통과 {passed_n}개 · 상위 {picks_n}개 · 7일 중 {len(loaded)}일치 적재 · "
            f"적재 없는 날 {days(missing)} · 적재됐지만 0개인 날 {days(empty)}")


def game_block(i, p, reviews, history):
    """추천 게임 하나 — 이름 한 줄 + 항목마다 이모지로 시작하는 줄.
    2026-09-11 사람 요청: 한 줄에 몰아 쓰면 안 읽힌다. 이모지는 항목마다 다르게(보기만 해도 무슨 줄인지 안다)."""
    g = p["game"]
    name = esc(g.get("name") or g.get("appid"))
    if g.get("store_url"):
        name = f"<{g['store_url']}|{name}>"
    _, graduated = history.get(g.get("appid"), (None, False))
    past = history_text(g, history)
    # 졸업작은 🌱 줄로 따로, 졸업이 아닌 재출시작은 출시일 줄 옆에 첫 출시일만 붙인다
    released = release_text(g) + (f" ({past})" if past and not graduated else "")
    lines = [f"{i}. *{name}*", f"💰 {price_text(g)}", f"📅 {released}"]
    if graduated:
        lines.append(f"🌱 {past}")
    if g.get("is_early_access"):
        lines.append("🚧 얼리 액세스")
    lines.append(f"⭐ {review_text(g, reviews)}")
    lines.append(f"🎯 추천 이유: {' · '.join(esc(t) for t in p['rare'])}")
    return lines


def footer_lines(rules):
    return [f"추천 기준: 내 취향 태그 중, 이번 주 신작에는 드물게({rules['rare_max_ratio']:.0%} 이하) 붙는 태그가 "
            f"{rules['min_rare_tags']}개 이상 겹친 게임",
            "해보고 싶은 건 직접 골라서 찜하기. 최종 판단은 사람 몫"]


def build_message(days, loaded, missing, pool_n, passed_n, picks, reviews, rules, history=None, empty=()):
    """슬랙 메시지. 줄표(—)는 쓰지 않는다(2026-09-11 사람 요청).
    적재 없는 날 · 0개인 날 · "신작 N개 중 기준 통과 M개" 같은 운영 상태는 메시지에 넣지 않는다(2026-09-11 사람 요청 —
    받는 사람에겐 잡음이다). 그 정보는 status_line()으로 리포트 .md · 실행 요약에 남기고, .json에도 그대로 있다.
    missing · empty는 그래서 여기서 안 쓴다(부르는 쪽 모양은 그대로 둔다)."""
    history = history or {}
    lines = ["🎮 인디게임 레이더", f"{month_day_weekday(days[0])} ~ {month_day_weekday(days[-1])} 출시작"]
    if not loaded:
        lines.append("이번 주 데이터 없음: 7일 모두 적재된 날이 없다. 수집 · 점검 로그를 볼 것")
        return "\n".join(lines)
    if not picks and pool_n < min_pool(rules):
        lines.append(f"신작이 {pool_n}개뿐이라 드문 태그를 가를 수 없다(최소 {min_pool(rules)}개 필요). 이번 주는 추천 없음")
    elif not picks:
        lines.append(f"신작 {pool_n}개 중 기준을 넘은 신작이 없다. 이번 주는 추천 없음")
    else:
        for i, p in enumerate(picks, 1):
            lines += [""] + game_block(i, p, reviews, history)
    lines += [""] + footer_lines(rules)
    return "\n".join(lines)


# --- 상점 대표 이미지 · 소개 (A안, 2026-09-11) ------------------------------------

# 한국어로 요청하면 한국어 소개 · 한국어판 대표 이미지(Honeycomb 등)를 주고, 없으면 스팀이 영어로 준다 — 실측
EXTRAS_CONTEXT = {"language": "koreana", "country_code": "KR"}
ASSET_BASE = "https://shared.akamai.steamstatic.com/store_item_assets/"
DESC_LIMIT = 100


def asset_url(assets, keys=("header_2x", "header")):
    """GetItems assets → 대표 이미지 주소. 파일 이름에 해시가 붙어 있어 규칙으로 못 만든다 — API가 준 형식으로 조립한다.
    고화질(2배, 920×430)이 있으면 그걸, 없으면 일반(460×215)을."""
    fmt = assets.get("asset_url_format")
    if not isinstance(fmt, str) or "${FILENAME}" not in fmt:
        return None                       # 형식이 바뀌면 파일 이름 없는 주소가 나간다 — 슬랙이 메시지 전체를 거절한다
    for key in keys:
        if isinstance(assets.get(key), str) and assets[key]:
            return ASSET_BASE + fmt.replace("${FILENAME}", assets[key])
    return None


def short_desc(text, limit=DESC_LIMIT):
    """소개 한 줄. 줄바꿈을 펴고, 슬랙에서 그대로 보이는 **굵게** 기호를 지우고, 길면 자른다."""
    text = " ".join((text or "").replace("**", "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def fetch_store_extras(appids, get=None, sleep=time.sleep):
    """(appid별 {"image", "desc"}, 오류 목록). 추천된 게임만 GetItems 한 번. 매일 수집 코드는 건드리지 않는다.
    실패해도 발송은 한다 — 이미지 · 소개 없이 글로만 나가고, 그 사실은 리포트 파일에 남는다."""
    if not appids:
        return {}, []
    budget, errors = collect.CallBudget(1 + collect.MAX_RETRIES), []
    payload = {"ids": [{"appid": a} for a in appids], "context": EXTRAS_CONTEXT,
               "data_request": {"include_assets": True, "include_basic_info": True}}
    body = collect.call_api(collect.ITEMS_PATH, {"input_json": json.dumps(payload)}, "store_items", budget, errors,
                            get=get or requests.get, sleep=sleep)
    extras = {}
    as_dict = lambda v: v if isinstance(v, dict) else {}
    for it in collect.page_items(body):
        extras[it.get("appid")] = {"image": asset_url(as_dict(it.get("assets"))),
                                   "desc": short_desc(as_dict(it.get("basic_info")).get("short_description"))}
    return extras, errors


def extras_summary(picks, extras, errors, fell_back):
    """리포트 파일에 남길 한 줄 — 이미지 · 소개가 몇 개 붙었고, 어느 게임이 빠졌는지."""
    if not picks:
        return "추천 없음 — 글 메시지로 보냈다"
    n = len(picks)
    has = lambda p, key: bool((extras.get(p["game"].get("appid")) or {}).get(key))
    note = f"대표 이미지 {sum(has(p, 'image') for p in picks)}/{n}개 · 소개 {sum(has(p, 'desc') for p in picks)}/{n}개"
    no_image = [str(p["game"].get("name") or p["game"].get("appid")) for p in picks if not has(p, "image")]
    if no_image and len(no_image) < n:
        note += f" (이미지 없음: {', '.join(no_image)})"
    if errors:
        note += " · 상점 정보 조회 실패"
    if fell_back:
        note += " · 슬랙이 받지 않아 이미지를 빼고 다시 보냈다"
    return note


def section(text):
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}   # 슬랙 section 글자 한도


def store_link(g):
    name = esc(g.get("name") or g.get("appid"))
    return f"<{g['store_url']}|{name}>" if g.get("store_url") else name


def notification_text(days, picks):
    """알림(잠금화면) 한 줄 — 블록을 못 그리는 곳에서도 이게 보인다."""
    span = f"{month_day(days[0])}~{month_day(days[-1])}"
    return f"🎮 인디게임 레이더 · {span} 추천 {len(picks)}개" if picks else f"🎮 인디게임 레이더 · {span} 이번 주는 추천 없음"


def build_blocks(days, loaded, missing, pool_n, passed_n, picks, reviews, rules, history, empty, extras):
    """슬랙 블록 — 맨 위 번호 목록(이름 = 상점 링크) → 게임마다 대표 이미지 + 정보 → 두 게임마다 구분선.
    2026-09-11 사람과 정한 A안. 이미지 · 소개를 못 받은 게임은 그 게임만 글로 나간다.
    추천이 없거나 적재된 날이 없으면 글 메시지 한 덩어리로 보낸다."""
    if not picks:
        return [section(build_message(days, loaded, missing, pool_n, passed_n, picks, reviews, rules, history, empty))]
    blocks = [section(f"*🎮 인디게임 레이더*\n{month_day_weekday(days[0])} ~ {month_day_weekday(days[-1])} 출시작"),
              section("\n".join(f"{i}. {store_link(p['game'])}" for i, p in enumerate(picks, 1))),
              {"type": "divider"}]
    for i, p in enumerate(picks, 1):
        g = p["game"]
        extra = extras.get(g.get("appid")) or {}
        if extra.get("image"):
            blocks.append({"type": "image", "image_url": extra["image"],
                           "alt_text": str(g.get("name") or g.get("appid"))[:2000]})
        card = game_block(i, p, reviews, history or {})
        if extra.get("desc"):
            card.append(f"📝 {esc(extra['desc'])}")
        blocks.append(section("\n".join(card)))
        if i % 2 == 0 and i < len(picks):
            blocks.append({"type": "divider"})          # "한 장에 두 게임" 아이디어를 구분선으로 흉내 낸다
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "\n".join(footer_lines(rules))}]})
    return blocks


def without_images(blocks):
    return [b for b in blocks if b.get("type") != "image"]


# --- 발송 ----------------------------------------------------------------------

def retry_after(r):
    try:
        return min(max(int((getattr(r, "headers", None) or {}).get("Retry-After", SLEEP_SEC)), 1), MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        return SLEEP_SEC


def slack_code(value):
    """슬랙 오류 코드 모양일 때만 그대로. 그 밖의 본문(중간 서버의 오류 페이지 등)은 남기지 않는다."""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,40}", value) else "(본문 생략)"


def post_dm(token, user, text, blocks=None, post=None, sleep=time.sleep):
    """슬랙 개인 DM 하나(chat.postMessage, channel = 멤버 ID). (성공 여부, 오류 목록).
    성공은 HTTP 200 + JSON ok=true. 오류에는 예외 종류 · HTTP 코드 · 슬랙 오류 코드만 남긴다 — 토큰은 헤더에만 있고
    예외 메시지 · 응답 본문에 섞여 들어올 수 있어서다.
    다시 보내는 건 네트워크 오류 · 5xx · 속도 제한(Retry-After만큼 기다림)뿐. invalid_blocks · channel_not_found ·
    invalid_auth 같은 건 요청 자체가 거절된 것이라 같은 걸 다시 보내도 같다."""
    post = post or requests.post
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    payload = {"channel": user, "text": text, "unfurl_links": False, "unfurl_media": False}
    if blocks:
        payload["blocks"] = blocks
    errors, wait = [], SLEEP_SEC
    for attempt in range(1 + MAX_RETRIES):
        if attempt:
            sleep(wait)
        wait = SLEEP_SEC
        try:
            r = post(SLACK_POST_URL, json=payload, headers=headers, timeout=TIMEOUT_SEC)
        except requests.RequestException as e:
            errors.append(f"시도{attempt}: {type(e).__name__}")
            continue
        try:
            body = r.json()
        except ValueError:
            body = None
        body = body if isinstance(body, dict) else None
        if r.status_code == 200 and body and body.get("ok") is True:
            return True, errors
        code = body.get("error") if body else None
        errors.append(f"시도{attempt}: HTTP {r.status_code} {slack_code(code)}")
        if r.status_code == 429 or code == "ratelimited":
            wait = retry_after(r)
            continue
        if r.status_code >= 500 or (r.status_code == 200 and body is None):
            continue                               # 서버 쪽 문제 — 다시 보낸다
        break
    return False, errors


def send_with_fallback(send, blocks):
    """(받아들여진 블록 또는 None, 오류, 이미지를 뺐는지). send(blocks) → (성공 여부, 오류 목록).
    슬랙은 이미지 주소 하나만 못 가져와도 메시지 전체를 invalid_blocks로 거절한다 — 그 주 추천이 통째로 안 나가지
    않게, 그 오류일 때만 이미지를 빼고 한 번 더 보낸다."""
    sent, errors = send(blocks)
    if sent:
        return blocks, errors, False
    plain = without_images(blocks)
    if plain == blocks or not any("invalid_blocks" in e for e in errors):
        return None, errors, False
    sent, more = send(plain)
    return (plain if sent else None), errors + [f"이미지 뺀 재발송 {e}" for e in more], True


def already_sent(label, sid, report_dir=REPORT_DIR):
    """그 사람에게 그 주를 보낸 기록이 있나. 이름을 정확히 맞춘다 — 앞부분만 보면 id 'a'가 'ab'의 기록에 걸린다."""
    if not os.path.isdir(report_dir):
        return False
    pattern = re.compile(rf"^{re.escape(label)}_{re.escape(sid)}(-\d+)?\.json$")
    for name in os.listdir(report_dir):
        if pattern.match(name):
            try:
                with open(os.path.join(report_dir, name), encoding="utf-8") as f:
                    if json.load(f).get("sent"):
                        return True
            except (OSError, ValueError):
                continue
    return False


# --- 실행 ----------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(description="인디게임 레이더 주간 추천 리포트")
    ap.add_argument("--date", help="발송일(KST, YYYY-MM-DD). 기본은 오늘. 대상은 그 전날까지 7일")
    ap.add_argument("--force", action="store_true", help="이미 보낸 주도 다시 보낸다")
    return ap.parse_args(argv)


def github_outputs(path, text):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write(f"report={path}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("## 주간 리포트\n\n" + text + "\n")


def read_profile(path):
    """(취향 태그 목록, 만든 시각, None) 또는 (None, None, 문제)."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        tags = [t["name"] for t in doc["tags"]]
    except (OSError, ValueError, KeyError, TypeError) as e:
        return None, None, f"취향 파일 {path}을 읽지 못했다({type(e).__name__})"
    if not tags:
        return None, None, f"취향 파일 {path}에 태그가 없다"
    return tags, doc.get("generated_at"), None


def read_last_run(path=LAST_RUN_PATH):
    """(갱신 시각, 사람별 결과). 없거나 못 읽으면 (None, {}). 모양이 이상한 항목은 뺀다."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None, {}
    if not isinstance(doc, dict):
        return None, {}
    results = doc.get("results") if isinstance(doc.get("results"), dict) else {}
    return doc.get("run_at"), {k: v for k, v in results.items() if isinstance(v, dict)}


def report_one(sub, ctx, token, force):
    """한 사람 몫: 채점 → 메시지 → (DM) → 리포트 파일. 결과 요약 dict. 한 사람의 문제가 다른 사람을 막지 않는다."""
    sid, days, rules, games, loaded = sub["id"], ctx["days"], ctx["rules"], ctx["games"], ctx["loaded"]
    prof_path = sub.get("profile_path") or profile_file(sid)
    profile_tags, generated_at, prof_problem = read_profile(prof_path)
    if profile_tags:
        profile_tags = [t for t in profile_tags if t not in ctx["exclude_tags"]]
    last = ctx["last_run"].get(sid) or {}
    if sub.get("profile_path"):
        stale, reason = False, None             # 운영자처럼 고정된 취향은 매주 갱신 대상이 아니다
    elif not ctx["refreshed"]:
        # 갱신 단계가 중간에 죽으면 _last_run.json은 지난주 것이다 — 그걸 이번 주 결과로 믿지 않는다
        stale, reason = True, "이번 실행일의 취향 갱신 기록이 없다(갱신 단계가 실패했거나 안 돌았다)"
    else:
        stale = last.get("status") == ST_STALE
        reason = " / ".join(last.get("problems") or []) or None
    profile_info = {"path": prof_path, "generated_at": generated_at, "stale": stale, "reason": reason}

    passed, rare, picks, history = [], [], [], {}
    if prof_problem:
        message = f"취향 없음: {prof_problem}"
    else:
        passed, rare = score_week(games, profile_tags, ctx["stoplist"], rules)
        picks = passed[:rules["weekly_max"]]
        history = release_history([p["game"] for p in picks])
        message = build_message(days, loaded, ctx["missing"], len(games), len(passed), picks, ctx["reviews"], rules,
                                history, ctx["empty"])
    status = status_line(loaded, ctx["missing"], ctx["empty"], len(games), len(passed), len(picks))

    sent, failed, blocks, extras_errors = False, False, None, []
    # 상점 이미지 · 소개는 실제로 보낼 때만 받는다 — 안 보내는 실행에 "글로만 보냈다" 같은 기록이 남지 않게
    extras_note = "보내지 않아 이미지 · 소개는 받지 않았다"
    if prof_problem:
        note, failed = "보내지 않았다 — " + prof_problem, True
    elif not loaded:
        # 운영 문제다 — 받는 사람에게 "데이터 없음"을 DM으로 보내지 않고, 종료 코드 1로 이슈를 연다
        note = "7일 모두 적재 없음 — 보내지 않았다"
    elif already_sent(ctx["label"], sid) and not force:
        note = "이미 보낸 주 — 다시 보내지 않았다(--force로 강제)"
    elif not token:
        note = "봇 토큰 없음(SLACK_BOT_TOKEN) — 파일만 저장했다"
    else:
        extras, extras_errors = fetch_store_extras([p["game"].get("appid") for p in picks])
        attempted = build_blocks(days, loaded, ctx["missing"], len(games), len(passed), picks, ctx["reviews"], rules,
                                 history, ctx["empty"], extras)
        text = notification_text(days, picks)
        accepted, errors, fell_back = send_with_fallback(
            lambda b: post_dm(token, sub["slack_user"], text, blocks=b), attempted)
        sent, failed = accepted is not None, accepted is None
        blocks = accepted or (without_images(attempted) if fell_back else attempted)   # 마지막으로 보낸 모양
        extras_note = extras_summary(picks, extras, extras_errors, fell_back)
        note = "슬랙 DM 발송 완료" if sent else "슬랙 DM 발송 실패 — " + " / ".join(errors)
        if sent and errors:
            note += " (" + " / ".join(errors) + ")"
    if stale:
        note += f" · 지난 취향으로 골랐다({profile_info['reason']})"
    status += f" · {extras_note}"

    report = {
        "label": ctx["label"],
        "subscriber": sid,
        "run_day": f"{ctx['run_day']:%Y-%m-%d}",
        "days": [f"{d:%Y-%m-%d}" for d in days],
        "loaded_days": [f"{d:%Y-%m-%d}" for d in loaded],
        "missing_days": [f"{d:%Y-%m-%d}" for d in ctx["missing"]],
        "unreadable": ctx["unreadable"],
        "empty_days": [f"{d:%Y-%m-%d}" for d in ctx["empty"]],
        "profile": profile_info,
        "profile_tags": profile_tags,
        "pool_games": len(games),
        "passed": len(passed),
        "rules": rules,
        "rare_tags": rare,
        "reviews_from": f"raw/{ctx['run_day']:%Y-%m-%d}.json" if ctx["reviews"] else None,
        "picks": [{"appid": p["game"].get("appid"), "name": p["game"].get("name"), "score": p["score"],
                   "rare_tags": p["rare"], "matched_tags": p["matched"], "store_url": p["game"].get("store_url"),
                   "history": history_text(p["game"], history)}
                  for p in picks],
        "message": message,
        "images": sum(1 for b in blocks or [] if b.get("type") == "image"),   # 실제로 보낸 이미지 수
        "extras_note": extras_note,
        "extras_errors": [e.get("error") for e in extras_errors],
        "blocks": blocks,                    # 마지막으로 보낸 페이로드 — 무엇이 나갔는지 나중에 확인할 수 있게
        "sent": sent,
        "send_note": note,
        "generated_at": datetime.datetime.now(collect.KST).isoformat(timespec="seconds"),
    }
    stem = free_stem(REPORT_DIR, f"{ctx['label']}_{sid}")
    json_path, md_path = os.path.join(REPORT_DIR, stem + ".json"), os.path.join(REPORT_DIR, stem + ".md")
    with open(json_path, "x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    with open(md_path, "x", encoding="utf-8") as f:
        f.write(message + "\n\n---\n" + note + "\n" + status + "\n")
    return {"id": sid, "sent": sent, "failed": failed, "note": note, "picks": len(picks), "status": status,
            "message": message, "json": json_path}


def summary_text(label, results, problem=None):
    lines = [f"# 주간 리포트 {label}", ""]
    if problem:
        lines += [f"⚠️ {problem}", ""]
    for r in results:
        lines.append(f"- **{r['id']}** · 추천 {r['picks']}개 · {r['note']}")
        lines.append(f"  - {r['status']}")
    for r in results:
        lines += ["", f"## {r['id']}", "", "```", r["message"], "```"]
    return "\n".join(lines) + "\n"


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    run_day = datetime.date.fromisoformat(args.date) if args.date else datetime.datetime.now(collect.KST).date()
    days = target_days(run_day)
    label = f"{days[0]:%Y-%m-%d}_{days[-1]:%Y-%m-%d}"

    rules, stoplist = load_rules()
    subs = load_subscribers()
    games, loaded, missing, unreadable, empty = load_week(days)
    run_at, last_run = read_last_run()
    ctx = {"label": label, "run_day": run_day, "days": days, "rules": rules, "stoplist": stoplist, "games": games,
           "loaded": loaded, "missing": missing, "unreadable": unreadable, "empty": empty,
           "reviews": latest_reviews(run_day), "last_run": last_run, "exclude_tags": load_exclude_tags(),
           "refreshed": isinstance(run_at, str) and run_at[:10] == f"{run_day:%Y-%m-%d}"}

    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    gha = bool(os.environ.get("GITHUB_ACTIONS"))
    problem = None if subs else "명단이 비었다(subscribers.yaml) — 아무에게도 안 보냈다"
    if not token and gha and subs:
        # 개인 DM 전환 뒤 가장 흔할 실수 — 명단은 있는데 아무도 못 받는다. 경고로 끝내지 않고 이슈를 연다
        problem = "봇 토큰 없음(SLACK_BOT_TOKEN) — 아무에게도 안 보냈다. GitHub Secrets를 확인할 것"
    if problem:
        print(("::error::" if gha else "") + problem)

    os.makedirs(REPORT_DIR, exist_ok=True)
    results = []
    for n, sub in enumerate(subs):
        if n and token:
            time.sleep(PAUSE_SEC)
        try:
            results.append(report_one(sub, ctx, token, args.force))
        except Exception as e:                  # 한 사람 몫이 예상 못 한 이유로 죽어도 다음 사람은 받는다
            traceback.print_exc()
            results.append({"id": sub["id"], "sent": False, "failed": True, "picks": 0, "json": "(없음)",
                            "note": f"리포트 중 예외 {type(e).__name__} — 실행 로그를 볼 것", "status": "", "message": ""})

    summary = summary_text(label, results, problem)
    summary_path = os.path.join(REPORT_DIR, free_stem(REPORT_DIR, f"{label}-summary") + ".md")
    with open(summary_path, "x", encoding="utf-8") as f:
        f.write(summary)
    github_outputs(summary_path, summary)
    print(summary)
    print(f"저장: {summary_path} · " + " · ".join(r["json"] for r in results))
    return 1 if problem or not loaded or any(r["failed"] for r in results) else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
