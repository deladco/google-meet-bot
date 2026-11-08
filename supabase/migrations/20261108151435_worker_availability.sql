-- Shared infrastructure status only. No host names, job IDs, user IDs,
-- meeting URLs, transcripts, or credentials belong in this table.
create table if not exists public.worker_heartbeats (
  worker_id uuid primary key,
  last_seen_at timestamptz not null default now(),
  activity text not null check (activity in (
    'idle', 'joining', 'recording', 'processing', 'transcribing',
    'summarizing', 'recovering', 'offline'
  ))
);

alter table public.worker_heartbeats enable row level security;
revoke all on table public.worker_heartbeats from public, anon, authenticated;
grant select on table public.worker_heartbeats to authenticated;
grant select, insert, update, delete on table public.worker_heartbeats to service_role;

-- Every signed-in user may see these deliberately shared infrastructure fields.
-- All writes require the worker's service role; users cannot forge an online state.
create policy "Signed-in users read shared worker availability"
  on public.worker_heartbeats for select to authenticated using (true);

create index if not exists worker_heartbeats_last_seen_idx
  on public.worker_heartbeats (last_seen_at desc);

notify pgrst, 'reload schema';
