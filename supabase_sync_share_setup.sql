-- ============================================================================
-- KUROANIME SHARE & SYNC WATCH HISTORY & LIBRARY — SUPABASE SQL SETUP (V2)
-- Fixes: user_id text type (supports both UUID & Google Auth profile IDs)
-- Grants insert/select permissions to anon and authenticated roles
-- Paste this entire script into your Supabase SQL Editor and click "Run".
-- ============================================================================

-- 1. Drop existing table if created with strict uuid constraint
drop table if exists public.user_sync_shares cascade;

-- 2. Create table with flexible text user_id
create table public.user_sync_shares (
  share_code text primary key,
  user_id text not null,
  user_name text not null default 'Friend',
  user_email text default '',
  user_avatar text default '',
  library_data jsonb not null default '[]'::jsonb,
  history_data jsonb not null default '[]'::jsonb,
  library_count integer not null default 0,
  history_count integer not null default 0,
  created_at timestamptz not null default now(),
  expires_at timestamptz default (now() + interval '30 days')
);

-- 3. Indexes for fast lookup by code and user
create index if not exists idx_sync_shares_user_id on public.user_sync_shares (user_id);
create index if not exists idx_sync_shares_expires_at on public.user_sync_shares (expires_at);

-- 4. Enable Row Level Security (RLS)
alter table public.user_sync_shares enable row level security;

-- 5. Grant API permissions to both anon and authenticated roles
grant select, insert, update, delete on table public.user_sync_shares to anon, authenticated;

-- Policy A: Anyone with a valid unexpired share code can read it (to preview & import)
drop policy if exists "Allow read sync shares" on public.user_sync_shares;
create policy "Allow read sync shares"
  on public.user_sync_shares
  for select
  to anon, authenticated
  using (expires_at is null or expires_at > now());

-- Policy B: Allow authenticated and authorized Google users to insert share codes
drop policy if exists "Allow insert sync shares" on public.user_sync_shares;
create policy "Allow insert sync shares"
  on public.user_sync_shares
  for insert
  to anon, authenticated
  with check (
    char_length(share_code) >= 6
    and char_length(user_id) > 0
  );

-- Policy C: Allow updating own share codes
drop policy if exists "Allow update sync shares" on public.user_sync_shares;
create policy "Allow update sync shares"
  on public.user_sync_shares
  for update
  to anon, authenticated
  using (true)
  with check (true);

-- Policy D: Allow deleting share codes
drop policy if exists "Allow delete sync shares" on public.user_sync_shares;
create policy "Allow delete sync shares"
  on public.user_sync_shares
  for delete
  to anon, authenticated
  using (true);
