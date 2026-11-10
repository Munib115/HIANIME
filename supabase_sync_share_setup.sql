-- ============================================================================
-- KUROANIME SHARE & SYNC WATCH HISTORY & LIBRARY — SUPABASE SQL SETUP
-- Allows authorized Google users to generate a sync code and share their
-- watch history and saved library with friends.
-- Paste this entire script into your Supabase SQL Editor and click "Run".
-- ============================================================================

-- 1. Create table for user sync share codes
create table if not exists public.user_sync_shares (
  share_code text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  user_name text not null default 'Friend',
  user_avatar text default '',
  library_data jsonb not null default '[]'::jsonb,
  history_data jsonb not null default '[]'::jsonb,
  library_count integer not null default 0,
  history_count integer not null default 0,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null default (now() + interval '30 days')
);

-- 2. Indexes for fast lookup by code and user
create index if not exists idx_sync_shares_user_id on public.user_sync_shares (user_id);
create index if not exists idx_sync_shares_expires_at on public.user_sync_shares (expires_at);

-- 3. Grants to Supabase API roles
grant select on table public.user_sync_shares to anon, authenticated;
grant insert, update, delete on table public.user_sync_shares to authenticated;

-- 4. Enable Row Level Security (RLS)
alter table public.user_sync_shares enable row level security;

-- Policy A: Anyone with a valid unexpired share code can read it (to preview & import)
drop policy if exists "Anyone can read valid sync shares" on public.user_sync_shares;
create policy "Anyone can read valid sync shares"
  on public.user_sync_shares
  for select
  to anon, authenticated
  using (expires_at > now());

-- Policy B: Only authenticated users can create their own share codes
drop policy if exists "Authenticated users can create sync shares" on public.user_sync_shares;
create policy "Authenticated users can create sync shares"
  on public.user_sync_shares
  for insert
  to authenticated
  with check (
    (select auth.uid()) = user_id
  );

-- Policy C: Users can update their own share codes
drop policy if exists "Users can update own sync shares" on public.user_sync_shares;
create policy "Users can update own sync shares"
  on public.user_sync_shares
  for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

-- Policy D: Users can delete their own share codes
drop policy if exists "Users can delete own sync shares" on public.user_sync_shares;
create policy "Users can delete own sync shares"
  on public.user_sync_shares
  for delete
  to authenticated
  using ((select auth.uid()) = user_id);
