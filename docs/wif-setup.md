# GitHub Actions가 Google Cloud에 배포할 수 있게 하기 (Workload Identity Federation)

GitHub Actions가 서비스 계정 키 파일 없이 Google Cloud에 배포하게 하는 절차다. 사람과 코딩 에이전트가 모두 이 순서대로 진행한다.

- **키 파일(JSON)을 만들지 않는다.** `gcloud iam service-accounts keys create` 는 쓰지 않는다.
- **Owner, Editor 같은 넓은 역할을 주지 않는다.** 아래 표의 역할만 준다.
- 명령마다 무엇을 만들고 누구에게 어떤 권한을 주는지 확인하고 실행한다.
- 풀, 공급자, 권한 연결은 반영되는 데 최대 5분쯤 걸린다. 바로 실패하면 몇 분 뒤 다시 시도한다.

## 만드는 것과 주는 권한

| 대상 | 이름 | 받는 권한 | 범위 |
| --- | --- | --- | --- |
| 실행용 서비스 계정 | `friend-agent-runtime` | 비밀값 읽기 `roles/secretmanager.secretAccessor` | 비밀값 `llm-api-key` 하나 |
| 배포용 서비스 계정 | `github-deployer` | 소스 배포 `roles/run.sourceDeveloper` | 프로젝트 |
| | | API 사용 `roles/serviceusage.serviceUsageConsumer` | 프로젝트 |
| | | 서비스 계정 사용 `roles/iam.serviceAccountUser` | 실행용 서비스 계정 하나 |
| | | (이미지 배포 때 추가) 이미지 올리기 `roles/artifactregistry.writer` | Artifact Registry 저장소 `friend-agent` 하나 |
| Workload Identity 풀 | `github` | | |
| 풀의 공급자 | `friend-agent-repo` | 내 저장소에서 온 요청만 받는 조건 | |
| 내 저장소의 워크플로 | | 배포용 서비스 계정으로 행동 `roles/iam.workloadIdentityUser` | 배포용 서비스 계정 하나 |
| Cloud Build 기본 서비스 계정 | `<프로젝트 번호>-compute@developer.gserviceaccount.com` | 빌드 후 배포 `roles/run.builder` (없을 때만) | 프로젝트 |

## 0. 값 정하기

```text
PROJECT_ID=<프로젝트 ID>
PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
REPO=<GitHub 계정>/<저장소 이름>          # 예: jisu/friend-agent
```

## 1. 실행용 서비스 계정

에이전트 서비스가 실행될 때 쓰는 계정이다. 다른 서비스와 같이 쓰는 기본 서비스 계정 대신 이 서비스 전용 계정을 둔다.

```text
gcloud iam service-accounts create friend-agent-runtime --project $PROJECT_ID \
  --display-name "friend-agent 실행용"

gcloud secrets add-iam-policy-binding llm-api-key --project $PROJECT_ID \
  --member "serviceAccount:friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com" \
  --role roles/secretmanager.secretAccessor
```

## 2. 배포용 서비스 계정과 권한

GitHub Actions가 배포할 때 쓰는 계정이다.

```text
gcloud iam service-accounts create github-deployer --project $PROJECT_ID \
  --display-name "GitHub Actions 배포용"

DEPLOYER=github-deployer@$PROJECT_ID.iam.gserviceaccount.com

gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/run.sourceDeveloper
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/serviceusage.serviceUsageConsumer

gcloud iam service-accounts add-iam-policy-binding \
  friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com --project $PROJECT_ID \
  --member "serviceAccount:$DEPLOYER" --role roles/iam.serviceAccountUser
```

`roles/iam.serviceAccountUser` 는 프로젝트 전체가 아니라 실행용 서비스 계정 하나에만 준다. 프로젝트 전체에 주면 배포용 계정이 프로젝트의 모든 서비스 계정으로 행동할 수 있다.

## 3. Workload Identity 풀과 공급자

```text
gcloud iam workload-identity-pools create github --project $PROJECT_ID \
  --location global --display-name "GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc friend-agent-repo --project $PROJECT_ID \
  --location global --workload-identity-pool github \
  --display-name "friend-agent 저장소" \
  --issuer-uri "https://token.actions.githubusercontent.com" \
  --attribute-mapping "google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition "assertion.repository=='$REPO'"
```

`--attribute-condition` 이 "내 저장소에서 온 요청만 받는다"는 조건이다. 이 조건이 없으면 GitHub의 다른 저장소도 이 공급자로 토큰을 받을 수 있다.

## 4. 내 저장소가 배포용 계정으로 행동하게 하기

```text
gcloud iam service-accounts add-iam-policy-binding $DEPLOYER --project $PROJECT_ID \
  --role roles/iam.workloadIdentityUser \
  --member "principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"
```

## 5. (이미지 배포를 시작할 때) Artifact Registry 저장소와 쓰기 권한

```text
gcloud artifacts repositories create friend-agent --project $PROJECT_ID \
  --repository-format docker --location us-central1

gcloud artifacts repositories add-iam-policy-binding friend-agent --project $PROJECT_ID \
  --location us-central1 --member "serviceAccount:$DEPLOYER" --role roles/artifactregistry.writer
```

## 6. Cloud Build 기본 서비스 계정 확인

2024년 이후 만든 프로젝트는 Cloud Build가 기본 서비스 계정으로 빌드한 뒤 배포할 권한이 없어 소스 배포가 실패할 수 있다. 실패 메시지에 권한 오류가 보일 때만 아래를 실행한다.

```text
gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member "serviceAccount:$PROJECT_NUMBER-compute@developer.gserviceaccount.com" --role roles/run.builder
```

## 7. GitHub 저장소에 값 넣기

워크플로가 읽는 값은 비밀값이 아니므로 Variables 에 넣는다.

```text
WIF_PROVIDER=$(gcloud iam workload-identity-pools providers describe friend-agent-repo --project $PROJECT_ID \
  --location global --workload-identity-pool github --format 'value(name)')

gh variable set GCP_PROJECT_ID --body "$PROJECT_ID"
gh variable set WIF_PROVIDER --body "$WIF_PROVIDER"
gh variable set DEPLOY_SA --body "$DEPLOYER"
gh variable set RUNTIME_SA --body "friend-agent-runtime@$PROJECT_ID.iam.gserviceaccount.com"
```

화면으로 넣을 때: GitHub 저장소 > Settings > Secrets and variables > Actions > Variables 탭 > New repository variable.

## 8. 확인

- Google Cloud 콘솔 > IAM 및 관리자 > 워크로드 아이덴티티 제휴: 풀 `github` 과 공급자 `friend-agent-repo` 가 보이고, 공급자의 속성 조건에 내 저장소 이름이 있다.
- IAM 및 관리자 > 서비스 계정: `github-deployer`, `friend-agent-runtime` 이 있고 키 탭에 키가 하나도 없다.
- IAM 및 관리자 > IAM: `github-deployer` 의 역할이 위 표와 같다. Owner, Editor 가 없다.
