-- 크레딧 원장(ledger). 잔액을 한 칸에 덮어쓰지 않고, 늘고 준 기록을 한 줄씩 쌓는다.
-- 잔액은 그 사용자의 delta 합계다. 기록을 고치거나 지우지 않고, 되돌릴 때도 반대 부호의 줄을 더한다.
--
-- ref 는 기록마다 하나뿐인 참조다(예: signup:<사용자ID>, msg:<메시지ID>, refund:<메시지ID>).
-- 같은 요청이 두 번 들어와도 같은 ref 는 한 번만 들어가므로 두 번 차감하거나 두 번 지급하지 않는다.
--
-- 0001_init.sql 과 같이 RLS 를 켜고 정책은 만들지 않는다. 서버(secret key)만 읽고 쓴다.
-- staging 프로젝트와 production 프로젝트에 각각 한 번씩 실행한다.

create table if not exists public.credit_ledger (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  delta integer not null check (delta <> 0),  -- 늘면 양수, 줄면 음수
  reason text not null,                        -- signup, message, refund
  ref text not null unique,
  created_at timestamptz not null default now()
);
create index if not exists credit_ledger_user_id_idx on public.credit_ledger (user_id, id);

alter table public.credit_ledger enable row level security;
