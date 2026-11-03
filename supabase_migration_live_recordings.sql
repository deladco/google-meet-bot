-- Run once in Supabase: SQL Editor > New query (after supabase_migration_mobile_uploads.sql).
-- Browser recordings stream to storage in 5-second chunks while you record, so nothing is lost if the
-- app or the tab crashes, and the worker can show live captions.
alter table public.audio_jobs add column if not exists chunked boolean not null default false;
alter table public.audio_jobs add column if not exists live_transcript text;

-- The browser marks its own recording as finished (status 'recording' -> 'queued').
drop policy if exists "Users update own audio jobs" on public.audio_jobs;
create policy "Users update own audio jobs" on public.audio_jobs
  for update using (auth.uid() = user_id) with check (auth.uid() = user_id);

notify pgrst, 'reload schema';
