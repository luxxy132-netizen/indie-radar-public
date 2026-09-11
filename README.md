# 🎮 인디게임 레이더

매일 스팀 신작을 모으고, 매주 월요일 아침 **내 취향에 맞는 인디게임만 슬랙 개인 DM으로** 받는 도구입니다.
서버 없이 GitHub Actions만으로 돌아갑니다.

**소개 페이지** → https://luxxy132-netizen.github.io/indie-radar-public/

> 이 저장소는 코드를 공개하기 위한 곳입니다. 실제 운영은 비공개 저장소에서 돌아가고, 받는 사람 명단 · 취향 · 수집 데이터는 여기에 없습니다.
> 여기서는 매일 수집이 꺼져 있습니다(아래 "직접 써 보기"의 `RADAR_ENABLED`).

## 무엇을 하나

| 언제 | 무엇을 |
|---|---|
| 매일 08:00 (KST) | 전날 출시된 스팀 게임을 모으고(`collect.py`), 품질을 점검한 뒤(`qa.py`) 통과한 것만 쌓는다(`data/`) |
| 매주 월요일 08:00 | 받는 사람마다 취향을 새로 만들고(`build_user_profiles.py`), 지난 7일 신작에서 골라 슬랙 DM으로 보낸다(`report.py`) |

DM에는 이번 주 추천 게임 목록(상점 링크)과, 게임마다 대표 이미지 · 가격 · 출시일 · 리뷰 · 추천 이유 · 한 줄 소개가 담긴 카드가 들어갑니다.

## 추천은 어떻게 고르나

1. **취향**: 좋아하는 게임들의 스팀 공식 태그 중, 여러 게임에 공통으로 나오는 태그를 취향으로 친다
   - 스팀 라이브러리의 플레이 시간 상위 게임(소프트웨어 제외) + 좋아하는 게임 링크 → `profiles/{id}.json`
   - 또는 직접 고른 게임 목록(`seed_library.yaml` → `peek.py` → `build_profile.py` → `profile.json`)
2. **드문 태그**: 그 주 신작 중 10% 이하에만 붙은 취향 태그를 "드문 태그"로 본다 — Indie처럼 흔한 태그는 점수가 거의 0
3. **기준**: 드문 취향 태그가 2개 이상 겹치는 신작만 통과, 점수순 최대 10개. 약한 주에는 억지로 채우지 않는다

규칙 값은 모두 `seed_library.yaml`의 `profile_rules` · `match_rules` · `user_profile_rules`에 있습니다.
성인 콘텐츠 태그(Sexual Content 등)는 누구의 취향 태그에도 넣지 않습니다.

## 직접 써 보기

아래에서 **Settings**는 전부 "이 저장소만의 설정" 화면입니다. GitHub 계정 설정이 아니라, 저장소를 열어 둔 상태에서 위쪽에 있는 탭 중 하나예요.

### 1. 내 계정으로 저장소 복사하기 (Fork)

1. [이 저장소 페이지](https://github.com/luxxy132-netizen/indie-radar-public)를 엽니다
2. 화면 **오른쪽 위**에 있는 `Fork` 버튼을 누릅니다
3. 넘어간 화면에서 초록색 `Create fork` 버튼을 누릅니다
4. 잠시 기다리면 `내계정이름/indie-radar-public`이라는 내 저장소가 생깁니다. **지금부터는 이 새로 생긴 내 저장소**에서 작업합니다

> 💡 받는 사람 명단에 슬랙 ID · 스팀 주소가 들어가니, 저장소를 비공개로 바꾸는 걸 권합니다: 내 저장소 페이지 → 위쪽 `Settings` 탭 → 페이지 맨 아래로 스크롤 → **Danger Zone** → **Change repository visibility** → **Change to private**

### 2. 비밀 값 두 개 넣기 (Secrets)

1. 내 저장소 페이지 위쪽 탭에서 `Settings`를 누릅니다
2. 왼쪽 메뉴에서 `Secrets and variables`를 누르고, 펼쳐지면 그 아래 `Actions`를 누릅니다
3. **Secrets** 탭이 선택된 상태에서 초록색 `New repository secret` 버튼을 누릅니다
4. 아래 표대로 Name과 Secret을 채우고 `Add secret`을 누릅니다. 이 값들은 코드 · 채팅 · 로그 어디에도 남지 않습니다

   | Name(이름) | Secret(값)을 어디서 받나 |
   |---|---|
   | `SLACK_BOT_TOKEN` | 슬랙 앱 → `OAuth & Permissions` → `Bot Token Scopes`에 `chat:write` 추가 → 앱 설치 → 생성된 `xoxb-`로 시작하는 토큰 복사. App Home에서 Messages Tab도 켜 둘 것 |
   | `STEAM_API_KEY` | steamcommunity.com/dev/apikey 접속 → 발급받은 키 복사 |

5. 4번을 두 번 반복해서 `SLACK_BOT_TOKEN`, `STEAM_API_KEY` 둘 다 넣습니다

### 3. 매일 수집 켜기 (Variables)

1. 2번과 같은 화면에서, **Secrets** 옆에 있는 `Variables` 탭을 누릅니다
2. `New repository variable` 버튼을 누릅니다
3. Name 칸에 `RADAR_ENABLED`, Value 칸에 `true`를 입력하고 `Add variable`을 누릅니다. 이게 있어야 매일 수집이 돕니다

### 4. 받는 사람 추가하기

1. 내 저장소의 파일 목록에서 `subscribers.yaml` 파일을 누릅니다
2. 오른쪽 위 연필 모양 아이콘(`Edit this file`)을 누릅니다
3. 아래 예시처럼 받는 사람을 한 명씩 적습니다
4. 오른쪽 위 초록색 `Commit changes...` 버튼을 누르고, 다시 뜨는 창에서 한 번 더 `Commit changes`를 누르면 저장됩니다

```yaml
subscribers:
  - id: me                       # 영어 소문자 · 숫자만
    slack_user: U0123ABCD        # 봇이 있는 워크스페이스에서: 프로필 → ⋮ → 멤버 ID 복사
    steam_profile: https://steamcommunity.com/id/이름
    favorites:                   # 선택. 스팀 프로필 없이 이것만 쓰려면 8개 이상
      - https://store.steampowered.com/app/413150/Stardew_Valley/
```

- 받는 사람은 스팀 개인정보 설정에서 **게임 세부 정보 · 총 플레이 시간**을 공개로 둬야 합니다
- 슬랙 게스트 · 외부(슬랙 커넥트) 계정에는 봇이 DM을 보낼 수 없습니다
- 첫 DM 전에 위쪽 `Actions` 탭 → 왼쪽 `measure` → 오른쪽 `Run workflow`에서 subscriber에 id를 넣고 실행하면 취향을 미리 볼 수 있습니다(파일을 쓰지 않음)
- 첫 DM은 다음 월요일 아침 8시에 옵니다. 기다리지 않고 바로 확인하려면 위쪽 `Actions` 탭 → 왼쪽 `collect` → 오른쪽 `Run workflow`에서 `report`를 체크하고 실행하세요
- 월요일 발송은 지난 7일치가 쌓여 있어야 제대로 나옵니다 — 매일 수집을 켠 뒤 일주일쯤 기다리세요

## 수동 실행

**Actions → collect → Run workflow**

| 옵션 | 뜻 |
|---|---|
| `report` | 월요일이 아니어도 이번 주 추천을 만들어 보낸다 |
| `force_report` | 이미 보낸 주도 다시 보낸다 |

## 실패하면

조용히 넘어가지 않습니다. 수집 · 점검 · 취향 갱신 · 발송 중 하나라도 실패하면 **GitHub 이슈**가 열리고, 무엇이 왜 실패했는지 적힙니다.

- 한 사람의 실패가 다른 사람의 발송을 막지 않는다
- 누군가의 스팀 프로필이 비공개로 바뀌면 지난 취향으로 보내고 이슈로 알린다
- 날짜가 붙은 기록 파일은 덮어쓰지 않는다(다시 돌리면 `-2`, `-3` …)
- 슬랙이 이미지 하나를 못 가져와 메시지 전체를 거절하면, 이미지만 빼고 한 번 더 보낸다

## 파일

```
collect.py              매일 수집 — 스팀 공식 API(키 없음)로 전날 출시작 · 태그 · 리뷰 → raw/
qa.py                   품질 점검 15항목 → qa/, 통과한 날만 data/
build_user_profiles.py  받는 사람 취향(스팀 라이브러리 + 좋아하는 게임 → profiles/)
build_profile.py        직접 고른 게임 목록으로 취향 만들기(seed_library.yaml → profile.json)
peek.py                 직접 고른 게임 이름 → 스팀 appid 해석(build_profile.py의 입력)
report.py               주간 추천 · 슬랙 DM → report/
user_profile_probe.py   취향 기준값 재기(measure.yml에서)
profile_probe.py        태그 기준 측정(초기 작업)

seed_library.yaml       직접 고른 게임(예시) · 모든 규칙 값
subscribers.yaml        받는 사람 명단(비어 있음)
.github/workflows/      collect.yml(매일 · 월요일) · test.yml(코드 바뀔 때 테스트) · measure.yml(수동)
docs/index.html         소개 페이지(GitHub Pages)
```

코드 주석의 "실패 N번", "HANDOFF", "코드 리뷰 날짜"는 비공개 작업 기록을 가리킵니다 — 왜 그렇게 만들었는지의 흔적으로 남겨 두었습니다.

## 로컬에서

```bash
pip install -r requirements.txt
```

```bash
python -m pytest -q test_collect.py test_qa.py test_build_profile.py test_report.py test_build_user_profiles.py
```

테스트는 네트워크를 부르지 않습니다. Python 3.12.

## 보안 · 개인정보

- 비밀값(스팀 키 · 슬랙 토큰)은 GitHub Secrets에만 두고, 오류 기록 · 리포트 · 로그에 남지 않도록 테스트로 확인합니다. 키가 들어가는 호출은 오류에 예외 종류와 HTTP 코드만 남깁니다
- 받는 사람의 스팀 보유 게임 전체 목록과 스팀 고유번호는 저장하지 않습니다(취향에 쓴 게임만)
