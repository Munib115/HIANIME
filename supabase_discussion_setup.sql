-- ============================================================================
-- KUROANIME DISCUSSION CHAT — SUPABASE SQL EDITOR SETUP
-- Supports: Text Messages, Images, Videos & Voice Notes (Google Auth Only)
-- Paste this entire script into the Supabase SQL Editor and click "Run".
-- ============================================================================

-- 1. Create the anime_comments table
create table if not exists public.anime_comments (
  comment_id bigint generated always as identity primary key,
  anime_id integer not null,
  episode_number integer not null default 1,
  user_id uuid not null references auth.users(id) on delete cascade,
  user_name text not null,
  user_email text not null,
  user_avatar text default '',
  auth_provider text not null default 'google',
  content text default '',
  media_url text default '',
  media_type text default '' check (media_type in ('', 'image', 'video', 'voice')),
  media_duration integer default 0,
  is_spoiler boolean not null default false,
  likes integer not null default 0,
  created_at timestamptz not null default now()
);

-- 2. Add composite B-tree index for fast episode & anime chat queries
create index if not exists idx_anime_comments_episode_feed
  on public.anime_comments (anime_id, episode_number, created_at desc);

create index if not exists idx_anime_comments_user_id
  on public.anime_comments (user_id);

-- 3. Grant table permissions to Supabase API roles
grant select on table public.anime_comments to anon, authenticated;
grant insert, update, delete on table public.anime_comments to authenticated;

-- 4. Enable Row Level Security (RLS)
alter table public.anime_comments enable row level security;

-- Policy A: Anyone (guests & signed-in users) can read discussion messages
drop policy if exists "Anyone can read anime comments" on public.anime_comments;
create policy "Anyone can read anime comments"
  on public.anime_comments
  for select
  to anon, authenticated
  using (true);

-- Policy B: Only Google-authenticated users can post messages as themselves
drop policy if exists "Google authenticated users can insert comments" on public.anime_comments;
create policy "Google authenticated users can insert comments"
  on public.anime_comments
  for insert
  to authenticated
  with check (
    (select auth.uid()) = user_id
    and coalesce(
      (select auth.jwt() -> 'app_metadata' ->> 'provider'),
      auth_provider
    ) = 'google'
    and (
      char_length(trim(coalesce(content, ''))) > 0
      or char_length(trim(coalesce(media_url, ''))) > 0
    )
  );

-- Policy C: Users can only update their own comments
drop policy if exists "Users can update own comments" on public.anime_comments;
create policy "Users can update own comments"
  on public.anime_comments
  for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

-- Policy D: Users can only delete their own comments
drop policy if exists "Users can delete own comments" on public.anime_comments;
create policy "Users can delete own comments"
  on public.anime_comments
  for delete
  to authenticated
  using ((select auth.uid()) = user_id);

-- 5. Create Storage Bucket for Discussion Images, Videos & Voice Messages
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'discussion-media',
  'discussion-media',
  true,
  26214400, -- 25 MB limit per image / video / voice note
  array[
    'image/jpeg', 'image/png', 'image/webp', 'image/gif',
    'video/mp4', 'video/webm', 'video/quicktime',
    'audio/webm', 'audio/mp4', 'audio/mpeg', 'audio/ogg', 'audio/wav'
  ]
)
on conflict (id) do update set
  public = true,
  file_size_limit = 26214400;

-- 6. Storage RLS Policies for `discussion-media` bucket
drop policy if exists "Public can view discussion media" on storage.objects;
create policy "Public can view discussion media"
  on storage.objects
  for select
  to anon, authenticated
  using (bucket_id = 'discussion-media');

drop policy if exists "Google users can upload discussion media" on storage.objects;
create policy "Google users can upload discussion media"
  on storage.objects
  for insert
  to authenticated
  with check (
    bucket_id = 'discussion-media'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

drop policy if exists "Google users can delete own discussion media" on storage.objects;
create policy "Google users can delete own discussion media"
  on storage.objects
  for delete
  to authenticated
  using (
    bucket_id = 'discussion-media'
    and (storage.foldername(name))[1] = (select auth.uid())::text
  );

-- 7. Enable Realtime broadcast on anime_comments (safe idempotent block)
do $$
begin
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'supabase_realtime'
      and schemaname = 'public'
      and tablename = 'anime_comments'
  ) then
    alter publication supabase_realtime add table public.anime_comments;
  end if;
end $$;

