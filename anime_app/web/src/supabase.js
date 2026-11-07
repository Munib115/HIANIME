import { createClient } from '@supabase/supabase-js';

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL || 'https://ammwkqeqjvukfuolnkuz.supabase.co';
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY || 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFtbXdrcWVxanZ1a2Z1b2xua3V6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3OTM4NzEzMDYsImV4cCI6MjEwOTQ0NzMwNn0.qxBcvQkUO-0h15dEho_GFk3WEV6X7xBeKY3DbzFJKRQ';

export const supabase = (supabaseUrl && supabaseAnonKey)
  ? createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
    },
  })
  : null;

/**
 * Returns the currently authenticated Supabase user or null
 */
export async function getCurrentUser() {
  if (!supabase) return null;
  try {
    const { data: { session } } = await supabase.auth.getSession();
    return session?.user || null;
  } catch (e) {
    return null;
  }
}

/**
 * Fetch continuous watch history from Supabase for the signed-in user
 */
export async function fetchWatchHistoryFromCloud() {
  const user = await getCurrentUser();
  if (!user) return [];
  try {
    const { data, error } = await supabase
      .from('watch_history')
      .select('*')
      .order('updated_at', { ascending: false })
      .limit(50);
    if (error) {
      console.warn('[Supabase history fetch error]', error.message);
      return [];
    }
    return data || [];
  } catch (e) {
    console.warn('[Supabase history fetch]', e);
    return [];
  }
}

/**
 * Upsert a watch history entry to Supabase
 */
export async function saveWatchHistoryToCloud(entry) {
  const user = await getCurrentUser();
  if (!user) return;
  try {
    const payload = {
      user_id: user.id,
      anime_id: Number(entry.anime_id),
      anime_title: entry.title || entry.anime_title || 'Anime',
      anime_poster: entry.poster || entry.anime_poster || '',
      episode_id: String(entry.episode_id || ''),
      episode_number: Math.max(1, Math.floor(Number(entry.episode_number) || 1)),
      episode_title: entry.episode_title || '',
      progress_seconds: Math.max(0, Math.floor(Number(entry.progress_seconds) || 0)),
      duration_seconds: Math.max(0, Math.floor(Number(entry.duration_seconds) || 0)),
      completed: Boolean(entry.completed),
      updated_at: new Date(entry.timestamp || Date.now()).toISOString()
    };
    const { error } = await supabase
      .from('watch_history')
      .upsert(payload, { onConflict: 'user_id,anime_id' });
    if (error) console.warn('[Supabase history upsert error]', error.message);
  } catch (e) {
    console.warn('[Supabase history upsert]', e);
  }
}

/**
 * Clear all watch history from Supabase for the signed-in user
 */
export async function deleteWatchHistoryFromCloud() {
  const user = await getCurrentUser();
  if (!user) return;
  try {
    await supabase.from('watch_history').delete().eq('user_id', user.id);
  } catch (e) {
    console.warn('[Supabase history clear error]', e);
  }
}

/**
 * Fetch the user's saved library / watchlist from Supabase
 */
export async function fetchLibraryFromCloud() {
  const user = await getCurrentUser();
  if (!user) return [];
  try {
    const { data, error } = await supabase
      .from('user_library')
      .select('*')
      .order('updated_at', { ascending: false });
    if (error) {
      console.warn('[Supabase library fetch error]', error.message);
      return [];
    }
    return data || [];
  } catch (e) {
    console.warn('[Supabase library fetch]', e);
    return [];
  }
}

/**
 * Upsert or remove an anime from the user's library in Supabase
 */
export async function saveLibraryItemToCloud(item, isAdd = true) {
  const user = await getCurrentUser();
  if (!user) return;
  try {
    const animeId = Number(item.anime_id);
    if (!isAdd) {
      const { error } = await supabase
        .from('user_library')
        .delete()
        .eq('user_id', user.id)
        .eq('anime_id', animeId);
      if (error) console.warn('[Supabase library delete error]', error.message);
      return;
    }

    const payload = {
      user_id: user.id,
      anime_id: animeId,
      anime_title: item.title || item.anime_title || 'Anime',
      anime_poster: item.poster || item.anime_poster || '',
      anime_type: item.type || item.anime_type || 'TV',
      anilist_score: item.anilist_score ? Number(item.anilist_score) : null,
      status: item.status || 'plan_to_watch',
      is_favorite: Boolean(item.is_favorite),
      updated_at: new Date().toISOString()
    };
    const { error } = await supabase
      .from('user_library')
      .upsert(payload, { onConflict: 'user_id,anime_id' });
    if (error) console.warn('[Supabase library upsert error]', error.message);
  } catch (e) {
    console.warn('[Supabase library upsert]', e);
  }
}

/**
 * Upload an image, video, or voice message blob to Supabase Storage (`discussion-media` bucket)
 */
export async function uploadDiscussionMediaToSupabase(fileOrBlob, ext = 'bin', contentType = 'application/octet-stream') {
  if (!supabase || !fileOrBlob) return null;
  const user = await getCurrentUser();
  if (!user) return null;
  try {
    const filePath = `${user.id}/${Date.now()}_${Math.random().toString(36).slice(2, 8)}.${ext}`;
    const { error } = await supabase.storage
      .from('discussion-media')
      .upload(filePath, fileOrBlob, {
        contentType,
        cacheControl: '3600',
        upsert: false,
      });
    if (error) {
      console.warn('[Supabase storage upload warning]', error.message);
      return null;
    }
    const { data: pub } = supabase.storage
      .from('discussion-media')
      .getPublicUrl(filePath);
    return pub?.publicUrl || null;
  } catch (e) {
    console.warn('[Supabase storage upload exception]', e);
    return null;
  }
}

/**
 * Fetch discussion messages from Supabase `anime_comments` table
 */
export async function fetchDiscussionMessagesFromSupabase(animeId, episodeNumber = 1, scope = 'episode') {
  if (!supabase) return [];
  try {
    let query = supabase
      .from('anime_comments')
      .select('*')
      .eq('anime_id', Number(animeId));
    if (scope === 'episode' && episodeNumber > 0) {
      query = query.eq('episode_number', Number(episodeNumber));
    }
    const { data, error } = await query
      .order('created_at', { ascending: false })
      .limit(100);
    if (error || !Array.isArray(data)) return [];
    return data.map(row => ({
      ...row,
      created_at: row.created_at ? new Date(row.created_at).getTime() : Date.now(),
    }));
  } catch (e) {
    return [];
  }
}

/**
 * Insert a new discussion message (text, image, video, or voice) into Supabase `anime_comments`
 */
export async function postDiscussionMessageToSupabase(payload) {
  if (!supabase) return null;
  const user = await getCurrentUser();
  if (!user) return null;
  try {
    const row = {
      anime_id: Number(payload.anime_id),
      episode_number: Math.max(1, Number(payload.episode_number) || 1),
      user_id: user.id,
      user_name: payload.user_name || user.user_metadata?.full_name || user.email?.split('@')[0] || 'Kuro Member',
      user_email: (payload.user_email || user.email || '').toLowerCase(),
      user_avatar: payload.user_avatar || user.user_metadata?.avatar_url || '',
      auth_provider: 'google',
      content: payload.content || '',
      media_url: payload.media_url || '',
      media_type: payload.media_type || '',
      media_duration: Math.max(0, Number(payload.media_duration) || 0),
      is_spoiler: Boolean(payload.is_spoiler),
    };
    const { data, error } = await supabase
      .from('anime_comments')
      .insert(row)
      .select()
      .single();
    if (error || !data) return null;
    return {
      ...data,
      created_at: data.created_at ? new Date(data.created_at).getTime() : Date.now(),
    };
  } catch (e) {
    return null;
  }
}

/**
 * Delete a user's own discussion message from Supabase `anime_comments`
 */
export async function deleteDiscussionMessageFromSupabase(commentId) {
  if (!supabase) return false;
  const user = await getCurrentUser();
  if (!user) return false;
  try {
    const { error } = await supabase
      .from('anime_comments')
      .delete()
      .eq('comment_id', Number(commentId))
      .eq('user_id', user.id);
    return !error;
  } catch (e) {
    return false;
  }
}
