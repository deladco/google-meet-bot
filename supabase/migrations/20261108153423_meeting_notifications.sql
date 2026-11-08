create table if not exists public.meeting_notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  event_key text not null,
  kind text not null check (kind in ('completed', 'failed', 'worker_unavailable')),
  title text not null,
  body text not null,
  job_table text check (job_table in ('bot_jobs', 'audio_jobs')),
  job_id uuid,
  meeting_id text,
  created_at timestamptz not null default now(),
  read_at timestamptz,
  unique (user_id, event_key)
);

alter table public.meeting_notifications enable row level security;
revoke all on table public.meeting_notifications from public, anon, authenticated;
grant select, insert on table public.meeting_notifications to authenticated;
-- Readers may mark their own notifications read, but cannot rewrite messages/ownership.
grant update (read_at) on table public.meeting_notifications to authenticated;
grant select, insert, update, delete on table public.meeting_notifications to service_role;

create policy "Users read own notifications" on public.meeting_notifications
  for select to authenticated using ((select auth.uid()) = user_id);
create policy "Users create own notifications" on public.meeting_notifications
  for insert to authenticated with check ((select auth.uid()) = user_id);
create policy "Users mark own notifications read" on public.meeting_notifications
  for update to authenticated using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create index if not exists meeting_notifications_inbox_idx
  on public.meeting_notifications (user_id, created_at desc);
create index if not exists meeting_notifications_unread_idx
  on public.meeting_notifications (user_id) where read_at is null;

notify pgrst, 'reload schema';
