# starter: 실습 단계마다 꺼내 쓰는 파일

처음 배포할 때는 쓰지 않는 파일들이다. 해당 실습을 할 때 저장소 안의 제자리로 복사해 쓴다. 여기 있는 동안에는 GitHub Actions가 실행하지 않는다.

| 파일 | 복사할 위치 | 하는 일 |
| --- | --- | --- |
| `workflows/deploy.yml` | `.github/workflows/deploy.yml` | main 에 머지되면 Cloud Run에 소스 배포 |
| `workflows/ci.yml` | `.github/workflows/ci.yml` | PR마다 테스트(가짜 응답, 비용 없음). 배포 전에도 불러 씀 |
| `workflows/deploy-envs.yml` | `.github/workflows/deploy.yml` (기존 파일 대체) | 테스트 통과 후 staging 브랜치는 staging 에, main 은 승인 후 production 에 배포 |
| `workflows/deploy-image.yml` | `.github/workflows/deploy.yml` (기존 파일 대체) | Actions에서 Dockerfile로 이미지를 만들어 커밋 번호 태그로 올리고 그 이미지로 배포 |
| `Dockerfile` | `Dockerfile` | 실행 환경 예시(Python 3.13, tesseract 와 한국어 데이터, 관리자 아닌 사용자) |
| `dockerignore` | `.dockerignore` | 이미지에 넣지 않을 파일(.env 포함) |
| `incidents/rename-key.patch` | `git apply` | 키 환경변수 이름을 바꿔 서버에서 앱이 시작하지 않는 변경 |
| `incidents/reminder-timezone.patch` | `git apply` | 약속 시각에서 시간대를 빼는 변경. 노트북(한국 시간)에서는 멀쩡하고 서버(UTC)에서는 9시간 늦게 저장됨 |
| `incidents/port-hardcode.patch` | `git apply` | 시작 명령의 포트를 8000 으로 고정. 노트북에서는 실행되지만 Cloud Run이 정한 포트에서 기다리지 않아 배포가 실패함 |
| `incidents/missing-package.patch` | `git apply` | requirements.txt 에서 `python-multipart` 를 뺌. 앱이 시작하면서 오류를 내고 꺼짐 |
| `incidents/safety-exact-match.patch` | `git apply` | 위기 표현을 목록과 정확히 같은 문장일 때만 잡게 바꿈. 테스트 `test_crisis_message_gets_fixed_safety_reply` 가 실패함 |
| `incidents/weather-keyword.patch` | `git apply` | 날씨 키워드를 "날씨 알려"로 좁힘. 테스트 `test_weather_question_picks_weather_tool` 이 실패함 |
| `incidents/dockerfiles/no-tesseract/` | 루트 `Dockerfile` 로 복사 | tesseract 설치 줄이 빠진 Dockerfile. 사진 글자 읽기가 서버에서 실패함 |
| `incidents/dockerfiles/env-included/` | `dockerignore` 를 루트 `.dockerignore` 로 복사 | `.env` 가 빠진 .dockerignore. 노트북에서 빌드한 이미지 안에 키 파일이 들어감 |
| `incidents/dockerfiles/root-user/` | 루트 `Dockerfile` 로 복사 | 관리자(root)로 실행되는 Dockerfile |
| `incidents/dockerfiles/port-fixed/` | 루트 `Dockerfile` 로 복사 | 포트를 8000 으로 고정한 Dockerfile. Cloud Run에서 시작 확인에 실패함 |

어느 실습부터 시작하든 필요한 상태와 만드는 방법은 `docs/start-states.md` 에 있다.

워크플로가 읽는 값(`GCP_PROJECT_ID`, `WIF_PROVIDER`, `DEPLOY_SA`, `RUNTIME_SA` 등)은 `docs/wif-setup.md` 절차로 만든다. 액션 버전은 바꾸지 않는다.
