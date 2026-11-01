-- Run once in Supabase: SQL Editor > New query (after supabase_migration_calendar.sql)
alter table public.calendar_feeds alter column ics_url drop not null;
alter table public.calendar_feeds add column if not exists google_refresh_token text;
