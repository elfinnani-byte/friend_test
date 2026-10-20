# Supabase 연결 절차

재배포해도 캐릭터와 대화가 남고, 사용자마다 데이터가 나뉘게 하는 절차다. staging 과 production 에 Supabase 프로젝트를 하나씩 쓴다.

## 1. 프로젝트 준비 (환경마다 한 번)

1. Supabase 대시보드에서 프로젝트를 만든다. 무료 플랜은 활성 프로젝트 2개까지다.
2. **SQL Editor** 에 `supabase/migrations/0001_init.sql` 내용을 붙여 넣고 실행한다.
3. **Authentication > Sign In / Providers** 에서 **Allow anonymous sign-ins** 를 켠다.
4. **Authentication > Rate Limits** 에서 익명 로그인 한도를 확인한다. 기본값은 IP 하나당 시간당 30건이다. 같은 와이파이를 쓰는 교실에서는 이 한도에 금방 걸리므로 수업 전에 올린다.
5. **Project Settings > API Keys** 에서 publishable key 와 secret key 를 확인한다. 키 값은 채팅이나 코드에 붙여 넣지 않는다.

## 2. 서비스에 연결

| 변수 | 넣는 곳 |
| --- | --- |
| `STORAGE=supabase`, `AUTH=supabase` | 일반 환경변수 |
| `SUPABASE_URL` | 일반 환경변수 |
| `SUPABASE_PUBLISHABLE_KEY` | 일반 환경변수(브라우저에 공개되는 키) |
| `SUPABASE_SECRET_KEY` | Secret Manager. 서비스에는 비밀값 이름으로 연결 |

secret key 를 Secret Manager에 넣었다면 실행용 서비스 계정(`friend-agent-runtime`)에 그 비밀값 하나에 대한 읽기 권한(`roles/secretmanager.secretAccessor`)을 준다. staging과 production의 secret key 는 서로 다른 비밀값으로 둔다(예: `supabase-secret-staging`, `supabase-secret-production`).

노트북에서는 `.env` 에 같은 이름으로 넣는다. 노트북은 staging 프로젝트에 연결한다.

## 3. 확인

- 화면 왼쪽 아래에 "게스트로 사용 중"이 보인다.
- 캐릭터를 저장하고 메시지를 보낸 뒤, Supabase **Table Editor** 의 `messages` 테이블에 행이 생겼는지 본다.
- 새 리비전을 배포한 뒤 다시 열어도 대화가 남아 있다.
- staging 에서 보낸 메시지가 production 프로젝트의 테이블에는 없다.

## 4. 구글 계정 연결 (공개 전)

1. Google Cloud 콘솔에서 OAuth 클라이언트를 만들고, 승인된 리디렉션 URI에 Supabase가 안내하는 콜백 주소를 넣는다.
2. Supabase **Authentication > Sign In / Providers > Google** 에 클라이언트 ID와 비밀값을 넣고 켠다.
3. 같은 화면에서 **manual linking(수동 연결)** 을 켠다. 게스트로 쓰던 데이터를 구글 계정에 이어 붙이는 데 필요하다.
4. **Authentication > URL Configuration** 에 서비스 주소를 Site URL과 Redirect URL로 넣는다.
5. 서비스에 `GOOGLE_LOGIN=true` 를 넣는다.

리허설에서 확인할 것: 게스트로 만든 캐릭터와 대화가 구글 계정 연결 뒤에도 그대로 보이는지(user_id가 유지되는지).
