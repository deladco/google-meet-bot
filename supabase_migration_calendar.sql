-- Run once in Supabase: SQL Editor > New query
create table if not exists public.calendar_feeds (
  user_id uuid primary key default auth.uid() references auth.users(id) on delete cascade,
  user_email text,
  ics_url text not null,
  bot_name text not null default 'AI Assistant',
  enabled boolean not null default true,
  updated_at timestamptz not null default now()
);

alter table public.calendar_feeds enable row level security;

create policy "Users read own calendar feed" on public.calendar_feeds
  for select using (auth.uid() = user_id);
create policy "Users create own calendar feed" on public.calendar_feeds
  for insert with check (auth.uid() = user_id);
create policy "Users update own calendar feed" on public.calendar_feeds
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy "Users delete own calendar feed" on public.calendar_feeds
  for delete using (auth.uid() = user_id);

-- Prevents the worker from queueing the same calendar event twice
alter table public.bot_jobs add column if not exists event_key text;
create unique index if not exists bot_jobs_user_event_key
  on public.bot_jobs (user_id, event_key) where event_key is not null;
