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
