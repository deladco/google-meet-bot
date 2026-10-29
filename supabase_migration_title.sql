-- Run once in Supabase SQL Editor (after supabase_schema.sql)
alter table public.synced_meetings add column if not exists title text;
