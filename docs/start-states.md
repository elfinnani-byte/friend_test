# 실습별 시작 상태

실습은 어느 것부터 시작해도 된다. 각 실습을 시작하기 전에 아래 표의 "필요한 상태"가 갖춰져 있는지 확인하고, 없으면 "만드는 방법"대로 먼저 만든다. 앞 실습을 이미 했다면 확인만 하고 넘어간다.

표는 위에서 아래로 쌓인다. 예를 들어 "Dockerfile" 실습을 하려면 그 위 칸들의 상태가 모두 필요하다.

| 실습 | 필요한 상태 (이 칸에서 새로 더해지는 것) | 만드는 방법 | 걸리는 시간 |
| --- | --- | --- | --- |
| Cloud Run 첫 배포 ① | 없음. Google Cloud 가입, 프로젝트, gcloud 설치를 이 실습 안에서 한다 | | |
| Cloud Run 첫 배포 ② | Google Cloud 프로젝트, gcloud 로그인과 프로젝트·리전 설정, API 4개 사용 설정, 템플릿으로 만든 내 저장소 | 아래 A | 20~40분 |
| | `friend-agent` 서비스가 비밀값 `llm-api-key` 를 참조해 배포되어 있음 | 아래 B | 15분 |
| 자동 배포 | (위와 같음) | | |
| 자동 검사와 환경 분리 | 배포용·실행용 서비스 계정과 Workload Identity 설정, 저장소 변수 4개 | `docs/wif-setup.md` 0~4단계와 7단계 | 30분 |
| | `.github/workflows/deploy.yml` 이 main 머지 때 배포함 | 아래 C | 10분 |
| Dockerfile | `.github/workflows/ci.yml`, staging·production 으로 나눠 배포하는 `deploy.yml` | 아래 D | 15분 |
| | GitHub 환경 `staging`·`production`(변수 SERVICE, ENV. production 은 배포 승인자 지정), `staging` 브랜치 | 아래 D | 15분 |
| | `friend-agent-staging` 서비스 (공개, 비밀값 연결) | 아래 E | 10분 |
| 이미지 배포 | 저장소 루트의 `Dockerfile`, `.dockerignore` | 아래 F | 5분 |
| 무중단 배포 | Actions에서 이미지를 만들어 배포하는 `deploy.yml`, Artifact Registry 저장소 `friend-agent` 와 쓰기 권한, 저장소 변수 `AR_REPO` | 아래 G | 15분 |
| 카나리와 메인 퀘스트 | `deploy.yml` 의 배포 단계에 `/health` 시작 확인 설정 | 아래 H | 5분 |

Supabase(데이터베이스와 로그인)는 "공개 준비" 실습에서 구글 로그인과 함께 붙인다. 그 전 실습은 모두 Supabase 없이 진행한다(서버 메모리 저장). 붙이는 절차는 `docs/supabase.md`.

## A. Google Cloud와 저장소

1. Google Cloud 무료 체험 가입, 프로젝트 만들기, gcloud 설치와 `gcloud init` 은 "Cloud Run 첫 배포 ①" 교안의 Google Cloud 시작하기 섹션을 따른다.
2. 기본 리전과 API:

```text
gcloud config set run/region us-central1
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com secretmanager.googleapis.com
```

3. 템플릿 저장소에서 Use this template > Create a new repository 로 공개 저장소를 만들고 내려받는다.

## B. 에이전트 서비스 배포 (비밀값 참조)

1. 콘솔 > Secret Manager > 보안 비밀 만들기: 이름 `llm-api-key`, 값은 AI 회사 API 키. 키 값은 채팅이나 명령에 붙여 넣지 않는다.
2. 아래 명령을 저장소 루트에서 실행한다.

```text
PROJECT_ID=$(gcloud config get-value project)
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')

# 기본 실행 계정이 비밀값 하나를 읽을 수 있게 한다 (실행용 전용 계정은 "자동 배포" 실습에서 만든다)
gcloud secrets add-iam-policy-binding llm-api-key \
  --member "serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor

gcloud run deploy friend-agent --source . --region us-central1 --allow-unauthenticated \
  --update-secrets LLM_API_KEY=llm-api-key:latest --min-instances 0 --max-instances 2
```

## C. main 머지 때 배포하는 워크플로

```text
mkdir -p .github/workflows
cp starter/workflows/deploy.yml .github/workflows/deploy.yml
```

PR로 올려 main 에 머지하고, Actions 탭에서 배포가 성공하는지 본다.

## D. 검사와 환경별 배포

```text
cp starter/workflows/ci.yml .github/workflows/ci.yml
cp starter/workflows/deploy-envs.yml .github/workflows/deploy.yml
git switch -c staging && git push -u origin staging && git switch main
```

GitHub 저장소 > Settings > Environments 에서 `staging`, `production` 을 만들고, 각 환경의 Variables 에 `SERVICE`(friend-agent-staging / friend-agent), `ENV`(staging / production) 를 넣는다. `production` 에는 Required reviewers 에 나를 넣는다.

## E. staging 서비스

```text
gcloud run deploy friend-agent-staging --source . --region us-central1 --allow-unauthenticated \
  --service-account friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com \
  --update-secrets LLM_API_KEY=llm-api-key:latest --update-env-vars ENV=staging \
  --min-instances 0 --max-instances 1
```

수업에서는 staging 용 키를 따로 만들어 사용 한도를 낮게 걸기도 한다. 이때는 비밀값 이름을 바꾸고, 실행용 계정에 그 비밀값 읽기 권한을 준다.

## F. Dockerfile

```text
cp starter/Dockerfile Dockerfile
cp starter/dockerignore .dockerignore
```

PR로 올려 staging, production 에 배포한다. 소스 배포는 저장소에 Dockerfile 이 있으면 그 파일로 이미지를 만든다.

## G. 이미지 배포

`docs/wif-setup.md` 5단계(Artifact Registry 저장소와 쓰기 권한)를 실행한 뒤:

```text
gh variable set AR_REPO --body "us-central1-docker.pkg.dev/$PROJECT_ID/friend-agent"
cp starter/workflows/deploy-image.yml .github/workflows/deploy.yml
```

PR로 올려 staging, production 에 배포한다.

## H. 시작 확인 설정

`.github/workflows/deploy.yml` 의 배포 단계 `flags` 줄을 아래처럼 바꾼다. 앱이 `/health` 에 정상 응답해야 요청을 받기 시작한다.

```text
flags: --service-account=${{ vars.RUNTIME_SA }} --startup-probe=httpGet.path=/health,periodSeconds=5,failureThreshold=12,timeoutSeconds=2
```

PR로 올려 배포한 뒤, 콘솔의 리비전 상세에서 시작 확인이 `/health` 로 설정되었는지 본다.

## 템플릿이 바뀌었을 때

템플릿으로 만든 저장소는 만든 순간의 파일만 복사한 새 저장소라서, 템플릿이 바뀌어도 내 저장소에는 반영되지 않습니다. 강사가 템플릿의 특정 파일을 고쳤다고 안내하면, 그 파일만 템플릿에서 가져와 덮어씁니다. 아래 명령의 파일 이름은 안내받은 파일로 바꿉니다.

```text
# 내 저장소 폴더에서. remote 추가는 처음 한 번만 한다
git remote add template https://github.com/88chacha/friend-agent-template.git

# 템플릿의 최신 내용을 받고, 안내받은 파일만 가져온다
git fetch template
git checkout template/main -- <파일 경로> <파일 경로>

git status          # 가져온 파일만 바뀌었는지 확인한다
git commit -m "템플릿 수정 반영"
git push
```

코딩 에이전트에게 맡길 때:

```text
88chacha/friend-agent-template 저장소를 template 이라는 이름의 remote로 추가하고(이미 있으면 건너뛰고) fetch 해 줘.
template/main 에서 <파일 경로> 파일만 가져와서 커밋해 줘. 다른 파일은 건드리지 마.
```

가져온 파일을 내가 이미 고쳐 두었다면 내 변경은 템플릿의 내용으로 덮어써집니다. 실행하기 전에 `git diff template/main -- <파일 경로>` 로 무엇이 바뀌는지 먼저 봅니다. 템플릿 전체를 합치는 `git merge template/main --allow-unrelated-histories` 는 내가 고친 파일마다 충돌이 나기 쉬워 쓰지 않습니다.
