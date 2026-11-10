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

/**
 * Resolve effective user from either Supabase Auth session or authenticated local profile
 */
export async function resolveSyncUser() {
  const sbUser = await getCurrentUser();
  if (sbUser) {
    return {
      id: sbUser.id,
      name: sbUser.user_metadata?.full_name || sbUser.email?.split('@')[0] || 'Kuro Member',
      email: sbUser.email || '',
      avatar: sbUser.user_metadata?.avatar_url || ''
    };
  }
  try {
    const local = JSON.parse(localStorage.getItem('kuro_user_profile') || 'null');
    if (local && (local.email || local.id)) {
      return {
        id: String(local.id || local.email),
        name: String(local.name || (local.email ? local.email.split('@')[0] : 'Kuro Member')),
        email: String(local.email || ''),
        avatar: String(local.avatar || '')
      };
    }
  } catch (e) {}
  return null;
}

/**
 * Normalize and sanitize any user-typed or pasted sync share code
 * Handles spaces (e.g. 'KURO - VD8NHB'), lowercase, missing prefix, or extra dashes.
 */
export function normalizeShareCode(rawCode) {
  if (!rawCode) return '';
  // 1. Strip all whitespace anywhere (spaces, tabs, newlines)
  let clean = String(rawCode).replace(/\s+/g, '').toUpperCase();
  // 2. Collapse consecutive dashes
  clean = clean.replace(/-+/g, '-');
  // 3. Ensure standard KURO- prefix format
  if (!clean.startsWith('KURO-')) {
    if (clean.startsWith('KURO')) {
      const remainder = clean.slice(4).replace(/^-+/, '');
      clean = remainder ? ('KURO-' + remainder) : 'KURO-';
    } else {
      clean = 'KURO-' + clean;
    }
  }
  return clean;
}

/**
 * Generate a random 6-character uppercase code (avoiding ambiguous 0, O, 1, I)
 */
function createRandomShareCode() {
  const chars = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  let out = '';
  for (let i = 0; i < 6; i++) {
    out += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return 'KURO-' + out;
}

/**
 * Generate a new sync share code with current library and watch history
 */
export async function generateSyncShareCode(libraryList = [], historyList = []) {
  const user = await resolveSyncUser();
  if (!user) return { ok: false, error: 'auth_required' };

  try {
    const code = createRandomShareCode();
    const cleanLibrary = Array.isArray(libraryList) ? libraryList.slice(0, 300) : [];
    const cleanHistory = Array.isArray(historyList) ? historyList.slice(0, 300) : [];

    const row = {
      share_code: code,
      user_id: String(user.id || user.email || 'kuro_user'),
      user_name: user.name || 'Kuro Member',
      user_email: user.email || '',
      user_avatar: user.avatar || '',
      library_data: cleanLibrary,
      history_data: cleanHistory,
      library_count: cleanLibrary.length,
      history_count: cleanHistory.length,
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 30 * 24 * 60 * 60 * 1000).toISOString()
    };

    let cloudOk = false;
    let cloudError = null;
    if (supabase) {
      try {
        const { error } = await supabase
          .from('user_sync_shares')
          .insert(row);
        if (!error) {
          cloudOk = true;
        } else {
          cloudError = error.message;
          console.warn('[Supabase sync share insert note]', error);
        }
      } catch (err) {
        cloudError = err?.message || 'Network error';
        console.warn('[Supabase sync share network note]', err);
      }
    }

    // Always mirror to local registry for fail-safe resolution and instant fallback
    try {
      const localRegistry = JSON.parse(localStorage.getItem('kuro_local_sync_shares') || '{}');
      localRegistry[code] = row;
      localStorage.setItem('kuro_local_sync_shares', JSON.stringify(localRegistry));
    } catch (e) {}

    return {
      ok: true,
      code,
      libraryCount: cleanLibrary.length,
      historyCount: cleanHistory.length,
      expiresAt: row.expires_at,
      authorName: row.user_name,
      isCloud: cloudOk,
      cloudError
    };
  } catch (e) {
    console.error('[Sync share generation exception]', e);
    return { ok: false, error: e.message || 'Unexpected sync error' };
  }
}

/**
 * Lookup a shared sync record by code
 */
export async function lookupSyncShareCode(rawCode) {
  const clean = normalizeShareCode(rawCode);
  if (!clean || clean.length < 6) {
    return { ok: false, error: 'Please enter a valid sync code (e.g. KURO-VD8NHB)' };
  }

  const rawSuffix = clean.replace(/^KURO-/, '');

  // 1. Try Supabase Cloud lookup
  if (supabase) {
    try {
      const { data, error } = await supabase
        .from('user_sync_shares')
        .select('*')
        .or('share_code.eq.' + clean + ',share_code.eq.' + rawSuffix)
        .maybeSingle();

      if (!error && data) {
        if (data.expires_at && new Date(data.expires_at).getTime() < Date.now()) {
          return { ok: false, error: 'This sync code has expired (valid for 30 days)' };
        }
        return { ok: true, share: data, source: 'cloud' };
      }
      if (error) {
        console.warn('[Supabase sync lookup note]', error.message);
      }
    } catch (e) {
      console.warn('[Supabase sync lookup error]', e);
    }
  }

  // 2. Fallback to local registry
  try {
    const localRegistry = JSON.parse(localStorage.getItem('kuro_local_sync_shares') || '{}');
    const localData = localRegistry[clean] || localRegistry[rawSuffix];
    if (localData) {
      if (localData.expires_at && new Date(localData.expires_at).getTime() < Date.now()) {
        return { ok: false, error: 'This sync code has expired' };
      }
      return { ok: true, share: localData, source: 'local' };
    }
  } catch (e) {}

  return { ok: false, error: 'Code ' + clean + ' not found. Please ensure the code is correct and was generated successfully.' };
}

/**
 * Import a friend's shared library and watch history to the signed-in user's account
 */
export async function importSyncShare(shareData) {
  if (!shareData) return { ok: false, error: 'Invalid share data' };
  const user = await resolveSyncUser();
  if (!user) return { ok: false, error: 'auth_required' };

  const libItems = Array.isArray(shareData.library_data) ? shareData.library_data : [];
  const histItems = Array.isArray(shareData.history_data) ? shareData.history_data : [];

  let importedLib = 0;
  let importedHist = 0;

  const sbUser = await getCurrentUser();
  if (supabase && sbUser) {
    try {
      for (const item of libItems) {
        if (!item?.anime_id) continue;
        await saveLibraryItemToCloud(item, true);
        importedLib++;
      }
      for (const entry of histItems) {
        if (!entry?.anime_id) continue;
        await saveWatchHistoryToCloud(entry);
        importedHist++;
      }
    } catch (e) {}
  } else {
    importedLib = libItems.length;
    importedHist = histItems.length;
  }

  return {
    ok: true,
    libraryCount: importedLib || libItems.length,
    historyCount: importedHist || histItems.length,
    authorName: shareData.user_name || 'Friend'
  };
}
