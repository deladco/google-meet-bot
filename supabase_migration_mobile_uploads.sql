-- Run once in Supabase: SQL Editor > New query
-- Lets the mobile app upload a phone recording; the worker transcribes it.
create table if not exists public.audio_jobs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  user_email text,
  storage_path text not null,
  filename text not null default 'recording.m4a',
  language text,
  status text not null default 'queued',
  message text,
  meeting_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.audio_jobs enable row level security;

create policy "Users read own audio jobs" on public.audio_jobs
  for select using (auth.uid() = user_id);
create policy "Users create own audio jobs" on public.audio_jobs
  for insert with check (auth.uid() = user_id and storage_path like auth.uid()::text || '/%');

-- Private bucket; each user can only touch files under a folder named after their user id.
insert into storage.buckets (id, name, public)
values ('recordings', 'recordings', false)
on conflict (id) do nothing;

create policy "Users upload own recordings" on storage.objects
  for insert to authenticated
  with check (bucket_id = 'recordings' and (storage.foldername(name))[1] = auth.uid()::text);
create policy "Users read own recordings" on storage.objects
  for select to authenticated
  using (bucket_id = 'recordings' and (storage.foldername(name))[1] = auth.uid()::text);

notify pgrst, 'reload schema';

-- App Store requirement: users must be able to delete their account from inside the app.
create or replace function public.delete_my_account()
returns void
language plpgsql
security definer
set search_path = public, auth
as $$
begin
  if auth.uid() is null then
    raise exception 'Not signed in';
  end if;
  delete from auth.users where id = auth.uid();
end;
$$;

revoke all on function public.delete_my_account() from public, anon;
grant execute on function public.delete_my_account() to authenticated;
