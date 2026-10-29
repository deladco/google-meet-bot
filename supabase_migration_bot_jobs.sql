-- Run once in Supabase: SQL Editor > New query
create table if not exists public.bot_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  user_email text,
  meeting_url text not null,
  bot_name text not null default 'AI Assistant',
  status text not null default 'queued',
  message text,
  meeting_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.bot_jobs enable row level security;

create policy "Users read own bot jobs" on public.bot_jobs
  for select using (auth.uid() = user_id);
create policy "Users create own bot jobs" on public.bot_jobs
  for insert with check (auth.uid() = user_id);
create policy "Users update own bot jobs" on public.bot_jobs
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
