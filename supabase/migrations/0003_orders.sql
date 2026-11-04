-- 크레딧 구매 주문. 토스페이먼츠 결제위젯 v2(주문서형)로 결제한다.
--
-- 흐름: 주문 만들기(pending) → 화면이 결제 요청 → successUrl 로 돌아옴
--       → 서버가 amount 를 이 표의 amount 와 비교 → 결제 승인 API → paid, 크레딧 지급(credit_ledger ref order:<id>)
-- amount 와 credits 는 주문을 만들 때 서버의 상품표(app/config.py PRODUCTS)에서 복사한다. 화면이 보낸 값은 쓰지 않는다.
-- id 는 토스페이먼츠의 orderId 로 그대로 쓴다(영문, 숫자, -, _, = 로 6~64자).
--
-- 0001_init.sql 과 같이 RLS 를 켜고 정책은 만들지 않는다. 서버(secret key)만 읽고 쓴다.
-- staging 프로젝트와 production 프로젝트에 각각 한 번씩 실행한다.

create table if not exists public.orders (
  id text primary key check (id ~ '^[A-Za-z0-9_=-]{6,64}$'),
  user_id uuid not null references auth.users (id) on delete cascade,
  product_id text not null,
  amount integer not null check (amount > 0),     -- 원
  credits integer not null check (credits > 0),
  status text not null default 'pending' check (status in ('pending', 'paid', 'failed')),
  payment_key text,                               -- 승인한 결제의 paymentKey. 조회와 취소에 필요하다
  created_at timestamptz not null default now(),
  paid_at timestamptz
);
create index if not exists orders_user_id_idx on public.orders (user_id, created_at);

alter table public.orders enable row level security;
