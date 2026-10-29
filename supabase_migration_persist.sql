-- Run once in Supabase: SQL Editor > New query
alter table public.synced_meetings add column if not exists owner_email text;

create table if not exists public.app_admins (email text primary key);
alter table public.app_admins enable row level security;

-- Replace with your admin sign-in email (same as ADMIN_EMAIL in app secrets):
-- insert into public.app_admins (email) values ('you@example.com');

create policy "Admins read all synced meetings" on public.synced_meetings
  for select using (
    exists (
      select 1 from public.app_admins a
      where lower(a.email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy "Admins read admin list" on public.app_admins
  for select using (lower(email) = lower(auth.jwt() ->> 'email'));
