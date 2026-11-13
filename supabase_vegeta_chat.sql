-- ====================================================================
-- KuroAnime • Prince Vegeta Chat Storage for Google / Supabase Auth Users
-- Paste this script into your Supabase Dashboard -> SQL Editor -> Run
-- ====================================================================

-- 1. Create table for persistent Vegeta AI chats
CREATE TABLE IF NOT EXISTS public.vegeta_chat_messages (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'model', 'bot')),
    content TEXT NOT NULL,
    image_url TEXT DEFAULT NULL,
    actions JSONB DEFAULT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc'::text, now())
);

-- 2. Create index for fast chronological conversation retrieval by user
CREATE INDEX IF NOT EXISTS idx_vegeta_chat_user_created 
    ON public.vegeta_chat_messages (user_id, created_at ASC);

-- 3. Enable Row Level Security (RLS)
ALTER TABLE public.vegeta_chat_messages ENABLE ROW LEVEL SECURITY;

-- 4. Set RLS Policies (Users can strictly only access and manage their own chats)
DROP POLICY IF EXISTS "Users can read own vegeta chats" ON public.vegeta_chat_messages;
CREATE POLICY "Users can read own vegeta chats"
    ON public.vegeta_chat_messages
    FOR SELECT
    TO authenticated
    USING (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can insert own vegeta chats" ON public.vegeta_chat_messages;
CREATE POLICY "Users can insert own vegeta chats"
    ON public.vegeta_chat_messages
    FOR INSERT
    TO authenticated
    WITH CHECK (auth.uid() = user_id);

DROP POLICY IF EXISTS "Users can delete own vegeta chats" ON public.vegeta_chat_messages;
CREATE POLICY "Users can delete own vegeta chats"
    ON public.vegeta_chat_messages
    FOR DELETE
    TO authenticated
    USING (auth.uid() = user_id);

-- 5. Enable Supabase Realtime synchronization across devices & tabs
ALTER PUBLICATION supabase_realtime ADD TABLE public.vegeta_chat_messages;
