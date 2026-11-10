-- ============================================================================
-- KUROANIME SHARE & SYNC DATABASE SETUP (V3 - DROP & RECREATE)
-- Fixes:
-- 1. Changes user_id from uuid to text (supports Google logins & Otaku profiles)
-- 2. Grants full SELECT / INSERT / UPDATE / DELETE to anon & authenticated roles
-- 3. Enables RLS with permissive policies for sharing codes across any device / friend
-- Paste this ENTIRE block into your Supabase Dashboard -> SQL Editor and click "Run".
-- ============================================================================

-- Step 1: Drop old table and its constraints completely
DROP TABLE IF EXISTS public.user_sync_shares CASCADE;

-- Step 2: Create the table with text user_id
CREATE TABLE public.user_sync_shares (
  share_code text PRIMARY KEY,
  user_id text NOT NULL,
  user_name text NOT NULL DEFAULT 'Kuro Member',
  user_email text DEFAULT '',
  user_avatar text DEFAULT '',
  library_data jsonb NOT NULL DEFAULT '[]'::jsonb,
  history_data jsonb NOT NULL DEFAULT '[]'::jsonb,
  library_count integer NOT NULL DEFAULT 0,
  history_count integer NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz DEFAULT (now() + interval '30 days')
);

-- Step 3: Fast lookup indexes
CREATE INDEX IF NOT EXISTS idx_sync_shares_user_id ON public.user_sync_shares (user_id);
CREATE INDEX IF NOT EXISTS idx_sync_shares_expires_at ON public.user_sync_shares (expires_at);

-- Step 4: Enable Row Level Security (RLS)
ALTER TABLE public.user_sync_shares ENABLE ROW LEVEL SECURITY;

-- Step 5: Grant API permissions to anon and authenticated roles
GRANT ALL ON TABLE public.user_sync_shares TO anon, authenticated, service_role;

-- Step 6: Create permissive RLS policies so friends can generate and import codes
DROP POLICY IF EXISTS "Public sync share select" ON public.user_sync_shares;
CREATE POLICY "Public sync share select"
  ON public.user_sync_shares
  FOR SELECT
  TO anon, authenticated
  USING (true);

DROP POLICY IF EXISTS "Public sync share insert" ON public.user_sync_shares;
CREATE POLICY "Public sync share insert"
  ON public.user_sync_shares
  FOR INSERT
  TO anon, authenticated
  WITH CHECK (true);

DROP POLICY IF EXISTS "Public sync share update" ON public.user_sync_shares;
CREATE POLICY "Public sync share update"
  ON public.user_sync_shares
  FOR UPDATE
  TO anon, authenticated
  USING (true)
  WITH CHECK (true);

DROP POLICY IF EXISTS "Public sync share delete" ON public.user_sync_shares;
CREATE POLICY "Public sync share delete"
  ON public.user_sync_shares
  FOR DELETE
  TO anon, authenticated
  USING (true);
