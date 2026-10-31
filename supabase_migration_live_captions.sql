-- Run once in Supabase: SQL Editor > New query
-- Holds the rolling live captions the bot worker writes while a meeting is being recorded.
alter table public.bot_jobs add column if not exists live_transcript text;
