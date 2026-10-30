-- Run once in Supabase: SQL Editor > New query
-- Private, per-user search index over meeting transcripts and notes.
create table if not exists public.meeting_chunks (
  id bigint generated always as identity primary key,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  meeting_id text not null,
  kind text not null check (kind in ('transcript', 'notes')),
  chunk_index int not null,
  content text not null,
  recorded_at timestamptz,
  fts tsvector generated always as (to_tsvector('english', content)) stored,
  unique (user_id, meeting_id, kind, chunk_index)
);

create index if not exists meeting_chunks_fts_idx on public.meeting_chunks using gin (fts);
create index if not exists meeting_chunks_user_recent_idx
  on public.meeting_chunks (user_id, recorded_at desc);

alter table public.meeting_chunks enable row level security;

create policy "Users read own chunks" on public.meeting_chunks
  for select using (auth.uid() = user_id);
create policy "Users insert own chunks" on public.meeting_chunks
  for insert with check (auth.uid() = user_id);
create policy "Users delete own chunks" on public.meeting_chunks
  for delete using (auth.uid() = user_id);

-- security invoker: row-level security applies, and the user filter is explicit as well.
create or replace function public.search_meeting_chunks(
  search_query text,
  match_count int default 8,
  since timestamptz default null
)
returns table (
  meeting_id text,
  kind text,
  chunk_index int,
  content text,
  recorded_at timestamptz,
  rank real
)
language sql
stable
security invoker
set search_path = public
as $$
  select c.meeting_id, c.kind, c.chunk_index, c.content, c.recorded_at,
         ts_rank_cd(c.fts, q.query) as rank
  from public.meeting_chunks c,
       (select to_tsquery('english', search_query) as query) q
  where c.user_id = auth.uid()
    and c.fts @@ q.query
    and (since is null or c.recorded_at >= since)
  order by rank desc, c.recorded_at desc
  limit least(greatest(match_count, 1), 30);
$$;

grant execute on function public.search_meeting_chunks(text, int, timestamptz) to authenticated;

notify pgrst, 'reload schema';
