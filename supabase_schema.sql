-- Run once in Supabase: SQL Editor > New query
create table if not exists public.meetings (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  meeting_id text not null,
  recorded_at timestamptz,
  status text,
  transcript text,
  notes text,
  minutes jsonb,
  created_at timestamptz not null default now(),
  unique (user_id, meeting_id)
);

alter table public.meetings enable row level security;

create policy "Users read own meetings" on public.meetings
  for select using (auth.uid() = user_id);
create policy "Users insert own meetings" on public.meetings
  for insert with check (auth.uid() = user_id);
create policy "Users update own meetings" on public.meetings
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "Users delete own meetings" on public.meetings
  for delete using (auth.uid() = user_id);
