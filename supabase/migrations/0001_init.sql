-- AI 친구 에이전트 서비스의 첫 테이블 구조.
-- Supabase 대시보드의 SQL Editor에 붙여 넣어 실행하거나, supabase CLI로 적용한다.
-- staging 프로젝트와 production 프로젝트에 각각 한 번씩 실행한다.
--
-- 모든 테이블에 RLS를 켜고 정책은 만들지 않는다.
-- 그래서 브라우저가 publishable key로 테이블을 직접 읽거나 쓸 수 없고, 서버(secret key)만 접근한다.
-- 익명 로그인 사용자도 authenticated 역할로 들어오므로, 정책을 만들 때는 그 점을 먼저 확인한다.

create table if not exists public.characters (
  user_id uuid primary key references auth.users (id) on delete cascade,
  data jsonb not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.user_settings (
  user_id uuid primary key references auth.users (id) on delete cascade,
  location text not null default '서울',
  timezone text not null default 'Asia/Seoul',
  push_enabled boolean not null default false
);

create table if not exists public.messages (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null,
  meta jsonb not null default '{}'::jsonb,   -- 고른 도구, 확신도, 작동한 안전장치
  created_at timestamptz not null default now()
);
create index if not exists messages_user_id_idx on public.messages (user_id, id);

create table if not exists public.summaries (
  user_id uuid primary key references auth.users (id) on delete cascade,
  text text not null,
  covers_until bigint not null                -- 이 message id 까지 요약에 들어 있다
);

create table if not exists public.memories (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null check (kind in ('fact', 'preference', 'event')),
  text text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists memories_user_id_idx on public.memories (user_id);

create table if not exists public.reminders (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  text text not null,
  due_at timestamptz not null,               -- 항상 시간대가 들어 있는 시각으로 저장한다
  status text not null default 'pending' check (status in ('pending', 'delivered', 'cancelled')),
  delivered_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists reminders_due_idx on public.reminders (user_id, status, due_at);

alter table public.characters enable row level security;
alter table public.user_settings enable row level security;
alter table public.messages enable row level security;
alter table public.summaries enable row level security;
alter table public.memories enable row level security;
alter table public.reminders enable row level security;
