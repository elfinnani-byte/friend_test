# AI 친구 에이전트 서비스

사용자가 정한 캐릭터가 이전 대화와 사용자에 대해 기억한 것을 바탕으로 대화하는 에이전트 서비스입니다.

- 필요하면 도구를 골라 쓴 뒤 답합니다. 날씨, 미세먼지, 공휴일을 찾고 약속을 저장합니다.
- "목요일 아침에 우산 챙기라고 알려 줘"처럼 부탁한 약속은 때가 되면 친구가 먼저 말을 걸어 전합니다.
- 화면과 API를 FastAPI 하나가 함께 제공하는 단일 서비스입니다. 배포와 운영 실습의 예시 코드로 씁니다.

## 저장소 만들기

이 저장소는 GitHub 템플릿 저장소입니다. 오른쪽 위의 **Use this template > Create a new repository** 를 눌러 내 계정에 **공개(Public)** 저장소로 만든 뒤, 내 노트북으로 내려받습니다.

```text
git clone https://github.com/<내 계정>/<저장소 이름>.git
cd <저장소 이름>
```

## 한 턴의 처리 흐름

```text
사용자 메시지
 ① 안전장치   위기 표현이면 캐릭터 설정과 상관없이 정해진 안전 응답 (코드 규칙)
 ② 도구 선택  모델이 도구 0~2개와 인자, 확신도, 이유를 JSON으로 고름
              확신도 0.6 미만이면 도구 없이 되묻기. 모델이 없거나 실패하면 키워드 규칙
 ③ 도구 실행  날씨, 미세먼지, 공휴일(키 없는 공개 API), 약속 저장, 보기, 취소
 ④ 답 생성    캐릭터 지시문 + 기억 메모 + 대화 요약 + 최근 10턴 + 도구 결과 → 스트리밍
 ⑤ 기억 정리  답을 다 보낸 뒤 같은 요청 안에서 기억 메모와 대화 요약을 갱신
```

**⑤를 응답 뒤 백그라운드로 돌리지 않는 이유:** Cloud Run 기본 설정(요청 기반 과금)에서는 응답을 보낸 뒤 CPU가 거의 주어지지 않습니다. 그래서 응답을 닫기 전에 처리합니다.

**호출 횟수와 비용:** 한 턴에 모델을 세 번 부릅니다(도구 선택, 답, 기억 정리). 메시지 한 건의 원가를 계산할 때 세 번을 모두 셉니다.

## 구성

| 경로 | 내용 |
| --- | --- |
| `app/main.py` | 화면(`/`)과 API. 목록은 파일 맨 위 설명 참고 |
| `app/agent.py` | 한 턴의 처리 흐름(①~⑤)과 앱을 열 때 먼저 말 걸기 |
| `app/router.py` | 도구 선택기. 모델 선택, 키워드 규칙, 안전장치(확신도 기준, 연도 고치기) |
| `app/tools.py` | 도구 6개와 지원 도시 목록 |
| `app/memory.py` | 기억 3층(최근 대화, 대화 요약, 기억 메모) |
| `app/persona.py` | 캐릭터 설정 선택지와, 설정을 지시문으로 바꾸는 규칙. 고정 안전 규칙 |
| `app/safety.py` | 위기 표현과 민감 정보 규칙 |
| `app/when.py` | 날짜, 시각 표현 해석과 달력 |
| `app/auth.py` | 요청한 사용자 확인(로그인 없음 또는 Supabase Auth) |
| `app/store/` | 저장소. `memory_store.py`(서버 메모리), `supabase_store.py`(Supabase) |
| `app/llm.py` | LLM 호출(OpenAI, Gemini, 가짜 응답) |
| `app/config.py` | 설정 읽기. 꼭 필요한 값이 없으면 시작하지 않는다 |
| `static/index.html` | 화면: 대화, 캐릭터 설정, 기억과 약속, 개발용 "이번 턴" 패널 |
| `supabase/migrations/` | Supabase 테이블 생성 SQL |
| `data/eval/` | 평가 데이터: 도구 선택 41개, 기억 정리 15개, 안전 15개 |
| `scripts/eval_tools.py` | 도구 선택 평가 |
| `scripts/watch_requests.py` | 배포 중 끊김 측정. `/version` 에 0.2초마다 요청하고 리비전별 응답 수와 실패 수를 요약(최대 5분, AI 호출 없음) |
| `tests/` | 가짜 응답으로 돌리는 테스트 |
| `hello/` | 배포 연습용 최소 앱(AI를 부르지 않음) |
| `Procfile` | Cloud Run이 앱을 시작할 때 쓰는 명령 |
| `AGENTS.md`, `CLAUDE.md` | 코딩 에이전트와 사람이 지키는 작업 규칙 |
| `.claude/settings.json` | 코딩 에이전트(Claude Code)가 실행하지 못하는 명령과 매번 묻는 명령 |
| `docs/` | 동작 예시(`examples.md`), 실습별 시작 상태(`start-states.md`), 배포 기록 양식, Supabase 연결 절차, GitHub Actions 배포 권한 절차(`wif-setup.md`), 운영 절차(`runbook.md`)와 카나리 판단 기준(`canary-criteria.md`) 양식 |
| `starter/` | 실습 단계마다 꺼내 쓰는 워크플로, Dockerfile 예시, 일부러 넣어 보는 변경(`starter/README.md`) |
| `data/samples/` | 사진 글자 읽기 실습용 일정표 사진 |

## 환경변수

| 이름 | 값 | 설명 |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai`, `gemini`, `claude`, `fake` | 기본값 `openai`. `fake` 는 키 없이 가짜 응답을 돌려준다. `claude` 의 기본 모델은 `claude-opus-5-5` 이고, `LLM_MODEL=claude-haiku-5-5` 처럼 바꿀 수 있다 |
| `LLM_API_KEY` | AI 회사 API 키 | `fake` 가 아니면 필수. 없으면 앱이 시작하지 않는다 |
| `LLM_MODEL` | 모델 이름 | 비우면 제공자별 기본값(`gpt-4o-mini` 등)을 쓴다 |
| `ENV` | `local`, `staging`, `production` | `staging` 이면 화면 위에 테스트 환경 표시. `production` 이면 개발용 패널이 기본으로 꺼짐 |
| `SHOW_TURN_PANEL` | `true`, `false` | 개발용 "이번 턴" 패널. 기본값은 production 에서만 꺼짐 |
| `STORAGE` | `memory`, `supabase` | 기본값 `memory`(서버 메모리. 재배포하면 사라짐) |
| `AUTH` | `none`, `supabase` | 기본값 `none`(로그인 없음, 모든 요청이 개발용 사용자 한 명) |
| `SUPABASE_URL` | `https://<프로젝트>.supabase.co` | `AUTH=supabase` 일 때 필수 |
| `SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_...` | 브라우저에 공개해도 되는 키. 로그인에만 쓴다 |
| `SUPABASE_SECRET_KEY` | `sb_secret_...` | `STORAGE=supabase` 일 때 필수. RLS를 건너뛰는 키라서 Secret Manager에 둔다 |
| `GOOGLE_LOGIN` | `true`, `false` | 구글 계정 연결 버튼 표시 |
| `OCR_ENABLED` | `true`, `false` | 사진 글자 읽기(📷 버튼). 운영체제에 tesseract 프로그램과 한국어 데이터가 있어야 한다 |
| `COMMIT_SHA` | 커밋 해시 | `/version` 에 표시된다. 자동 배포에서 넣는다 |
| `BANNER` | 짧은 문구 | 화면 위에 띠로 보이고 `/version` 에도 나온다. 이미지를 새로 만들지 않고 새 리비전을 만들어 볼 때 쓴다 |
| `SLOW_START_SECONDS` | 초 | 배포 실습용 시작 지연. 포트는 바로 열지만 이 시간 동안 모든 요청(`/health` 포함)에 503을 돌려준다. 기본값 0 |

`STORAGE=supabase` 는 `AUTH=supabase` 와 함께 써야 합니다. 로그인 없이 DB에 저장하면 모든 사용자의 데이터가 한 사람 것으로 섞이므로, 앱이 시작하지 않습니다.

## 노트북에서 실행하기

```text
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # .env 를 열어 값을 채운다. 처음에는 LLM_PROVIDER=fake 로 두어도 된다
uvicorn app.main:app --port 8080
```

1. 브라우저에서 `http://localhost:8080` 을 열면 캐릭터 만들기 화면이 나옵니다.
2. 저장하면 캐릭터가 먼저 말을 겁니다.
3. 오른쪽 "이번 턴" 패널에서 메시지마다 아래 내용을 볼 수 있습니다.
   - 고른 도구, 확신도, 이유, 작동한 안전장치
   - 도구 결과
   - 답에 넣은 기억
   - 새로 기억한 것
   - 걸린 시간

`LLM_PROVIDER=fake` 이면 모델 대신 키워드 규칙이 도구를 고르고, 답은 "(가짜 응답)"으로 시작하는 짧은 문장입니다. `.env` 는 노트북에서 실행할 때만 읽고, 저장소와 배포에는 올라가지 않습니다(`.gitignore`, `.gcloudignore`).

## 테스트와 평가

```text
pip install -r requirements-dev.txt
pytest                                  # 가짜 응답으로 실행. 비용 없음
python scripts/eval_tools.py --rule     # 키워드 규칙 도구 선택 평가. 비용 없음
python scripts/eval_tools.py            # .env 의 모델로 도구 선택 평가. 호출 41번
```

템플릿을 만들 때 잰 결과입니다(2026-10-12).

| 선택기 | 도구 정답 | 도구와 인자 모두 정답 |
| --- | --- | --- |
| 모델 `gpt-4o-mini` (3회 실행) | 37~39 / 41 | 35~36 / 41 |
| 키워드 규칙 | 37 / 41 | 36 / 41 |

**모델이 자주 틀린 것**
- "아침"을 08:00이 아니라 09:00으로 잡음
- "금요일 시험 전날"을 수요일로 계산함
- 지난 연도(2023)로 날짜를 씀. 이 경우는 코드 안전장치가 올해로 고칩니다.
- 묻지 않은 도구를 덧붙임

**키워드 규칙이 틀린 것**
- "우산 잃어버렸어", "지난 공휴일에 캠핑 갔었어"처럼 단어만 같고 뜻이 다른 문장
- "그럼 모레는?" 같은 이어지는 질문

같은 데이터로 돌려도 모델 결과는 실행마다 조금씩 다릅니다.

## Cloud Run에 배포하기

```text
# hello 앱 (hello/ 폴더 안에서)
gcloud run deploy hello --source . --region us-central1 --allow-unauthenticated

# 에이전트 서비스 (저장소 루트에서)
gcloud run deploy friend-agent --source . --region us-central1 --allow-unauthenticated
```

에이전트 서비스는 `LLM_API_KEY` 가 없으면 시작하지 않으므로, 키를 넣기 전 첫 배포는 실패합니다. 키를 넣는 방법과 Secret Manager 연결은 교안을 따라 진행합니다.

## 단계별로 켜는 기능

처음 배포할 때는 서버 메모리에 저장하고 로그인이 없습니다. 아래 기능은 해당 주제를 배우는 날 켭니다.

| 기능 | 켜는 방법 | 필요한 것 |
| --- | --- | --- |
| 재배포해도 남는 저장, 사용자별 데이터 | `STORAGE=supabase`, `AUTH=supabase` | Supabase 프로젝트, 익명 로그인 켜기. 절차는 `docs/supabase.md`. 동료에게 공개하기 전에 구글 로그인과 함께 붙인다 |
| 구글 계정 연결 | `GOOGLE_LOGIN=true` | Supabase 구글 로그인 설정, 수동 연결(manual linking) 켜기 |
| 사진 글자 읽기 | `OCR_ENABLED=true` | tesseract 프로그램. 노트북: `brew install tesseract tesseract-lang`(macOS). 서버: Dockerfile에 설치(`starter/Dockerfile`) |
| 앱을 닫아 둬도 오는 약속 알림 | 아직 없음 | Cloud Scheduler, 웹 푸시, PWA |

## 외부 API

| 도구 | API | 조건 |
| --- | --- | --- |
| 날씨, 미세먼지 | [Open-Meteo](https://open-meteo.com/) | 키 없음. 비상업적 사용 무료, 하루 1만 건 이하 권장. 상용 서비스는 유료 플랜 필요 |
| 공휴일 | [Nager.Date](https://date.nager.at/) | 키 없음 |

공개 지오코딩 API는 한국어 지명을 잘못 찾는 경우가 있어서, 지원 도시 26곳의 좌표를 `app/tools.py` 에 직접 적어 두었습니다.
