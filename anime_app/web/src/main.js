import './style.css';
import { inject } from '@vercel/analytics';
import { NetflixPlayer } from './netflix_player.js';
import { 
  supabase, 
  fetchWatchHistoryFromCloud, 
  saveWatchHistoryToCloud, 
  deleteWatchHistoryFromCloud, 
  fetchLibraryFromCloud, 
  saveLibraryItemToCloud,
  uploadDiscussionMediaToSupabase,
  fetchDiscussionMessagesFromSupabase,
  postDiscussionMessageToSupabase,
  deleteDiscussionMessageFromSupabase,
  generateSyncShareCode,
  lookupSyncShareCode,
  importSyncShare
} from './supabase.js';

// Initialize Vercel Web Analytics
inject();

let activePlayerInstance = null;
import { 
  createIcons, 
  Home, 
  Compass, 
  Search, 
  Bookmark, 
  Play, 
  Star, 
  ArrowLeft, 
  Layers, 
  Volume2, 
  Shield, 
  ShieldCheck,
  ShieldAlert,
  Clock, 
  Check, 
  X, 
  ChevronRight, 
  ChevronLeft,
  RotateCcw,
  Sparkles,
  Film,
  Bell,
  ExternalLink,
  Download,
  Plus,
  User,
  Settings,
  LogOut,
  Trash2,
  Share2,
  Copy,
  ChevronDown,
  Info
} from 'lucide';

// ─── Global State & Storage ─────────────────────────────────────────
const state = {
  currentTab: 'home',
  activeAnimeId: null,
  activeAnimeData: null,
  activeEpisodes: [],
  activeEpisodeIndex: 0,
  activeAudioType: localStorage.getItem('kuro_preferred_audio') === 'dub' ? 'dub' : 'sub', // 'sub' | 'dub'
  activeServerIndex: 0,
  currentStreams: { sub: [], dub: [] },
  browsePage: 1,
  browseGenre: '',
  browseType: '',
  browseStatus: '',
  browseSort: 'popular',
  searchDebounceTimer: null,
  watchlist: JSON.parse(localStorage.getItem('kuro_watchlist') || '[]'),
  history: JSON.parse(localStorage.getItem('kuro_history') || '[]'),
};

function resolveApiBase() {
  if (typeof window !== 'undefined' && (window.location.hostname === 'localhost' || window.location.hostname === '127.0.0.1')) {
    return '/api';
  }
  const raw =
    (typeof import.meta !== 'undefined' && import.meta.env && (import.meta.env.VITE_API_BASE || import.meta.env.VITE_API_URL)) ||
    (typeof window !== 'undefined' && window.__API_BASE__) ||
    'https://kuro-anime.onrender.com/api';
  const cleaned = String(raw).trim().replace(/\/+$/, '');
  if (!cleaned) return '/api';
  if (cleaned.startsWith('http') && !cleaned.endsWith('/api')) {
    return `${cleaned}/api`;
  }
  return cleaned;
}

const API_BASE = resolveApiBase();


// ─── Lucide Icons Initializer ───────────────────────────────────────
function refreshIcons() {
  createIcons({
    icons: {
      Home, Compass, Search, Bookmark, Play, Star, ArrowLeft,
      Layers, Volume2, Shield, ShieldCheck, ShieldAlert, Clock, Check, X, ChevronRight,
      ChevronLeft, RotateCcw, Sparkles, Film, Bell, ExternalLink, Download, Plus,
      User, Settings, LogOut, Trash2, Share2, Copy, ChevronDown, Info
    }
  });
}

// ─── Toast Notifications ────────────────────────────────────────────
function showToast(msg) {
  const toast = document.getElementById('toast-notification');
  if (!toast) return;
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2500);
}

// ─── Video Playback Controller ──────────────────────────────────────
function stopVideoPlayback() {
  if (activePlayerInstance) {
    try { activePlayerInstance.destroy(); } catch (e) {}
    activePlayerInstance = null;
  }
  const frameContainer = document.getElementById('video-frame-container');
  if (frameContainer) {
    frameContainer.innerHTML = '';
  }
  // Thoroughly pause, reset and unload all video and audio elements
  document.querySelectorAll('video, audio').forEach(media => {
    try {
      media.pause();
      media.currentTime = 0;
      media.removeAttribute('src');
      media.load();
    } catch (e) {}
  });
  // Unload any iframe stream instances
  document.querySelectorAll('#video-frame-container iframe, .video-iframe').forEach(iframe => {
    try {
      iframe.src = 'about:blank';
      iframe.remove();
    } catch (e) {}
  });
  // Exit fullscreen & unlock orientation if active
  const isFs = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
  if (isFs) {
    try {
      if (document.exitFullscreen) document.exitFullscreen();
      else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    } catch (e) {}
  }
  try {
    if (screen.orientation && screen.orientation.unlock) {
      screen.orientation.unlock();
    }
  } catch (e) {}
}

// ─── View Routing & Browser History ─────────────────────────────────
let isHandlingPopState = false;

function getActiveViewId() {
  return document.querySelector('.view-screen.active')?.id || 'view-home';
}

function showView(viewId, { pushHistory = true } = {}) {
  const prevViewId = getActiveViewId();

  // If moving away from player, flush current episode history & kill video playback
  if (prevViewId === 'view-watch' && viewId !== 'view-watch') {
    if (state.activeAnimeData && state.activeEpisodes[state.activeEpisodeIndex]) {
      const curEp = state.activeEpisodes[state.activeEpisodeIndex];
      const curTime = activePlayerInstance?.video?.currentTime || 0;
      const curDur = activePlayerInstance?.video?.duration || 0;
      saveHistory(state.activeAnimeData, curEp, curTime, curDur, curDur > 0 && curTime >= curDur - 25);
    }
    stopVideoPlayback();
  }

  // Ensure body scroll lock is cleared unless an overlay modal is open
  const anyModalOpen = document.querySelector('.inbox-modal-backdrop.active, .kuro-modal-backdrop.active');
  if (!anyModalOpen) {
    document.body.style.overflow = '';
  }

  // Guard against showing an unrendered/stuck detail screen (prevents "Loading anime details..." black screen)
  if (viewId === 'view-detail' && !state.isFetchingDetail) {
    const detailContainer = document.getElementById('detail-container');
    const hasRenderedDetail = detailContainer && detailContainer.querySelector('.detail-body');
    if (!hasRenderedDetail) {
      viewId = 'view-home';
      state.currentTab = 'home';
    }
  }

  document.querySelectorAll('.view-screen').forEach(el => el.classList.remove('active'));
  const target = document.getElementById(viewId);
  if (target) {
    target.classList.add('active');
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    document.documentElement.scrollLeft = 0;
    document.body.scrollLeft = 0;
  }

  // When returning to Home, ensure Continue Watching & Home rails are rendered and fresh
  if (viewId === 'view-home') {
    state.currentTab = 'home';
    renderContinueWatchingRail();
    const railsContainer = document.getElementById('rails-container');
    if (railsContainer && railsContainer.children.length === 0) {
      handleInitialRoute();
    }
  }

  // Update tab active state (mobile & desktop)
  document.querySelectorAll('.tab-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === state.currentTab);
  });
  document.querySelectorAll('.desktop-nav-menu .nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === state.currentTab);
  });

  // Push semantic crawlable URL to browser history state
  if (pushHistory && !isHandlingPopState) {
    try {
      let targetPath = '/';
      if (viewId === 'view-browse') {
        targetPath = state.browseGenre ? `/browse?genre=${encodeURIComponent(state.browseGenre)}` : '/browse';
      } else if (viewId === 'view-search') {
        const searchInput = document.getElementById('search-input-field');
        const q = searchInput?.value?.trim();
        targetPath = q ? `/search?q=${encodeURIComponent(q)}` : '/search';
      } else if (viewId === 'view-watchlist') {
        targetPath = '/watchlist';
      } else if (viewId === 'view-detail' && state.activeAnimeId) {
        targetPath = `/anime/${state.activeAnimeId}`;
      } else if (viewId === 'view-watch' && state.activeAnimeId) {
        const ep = state.activeEpisodes[state.activeEpisodeIndex];
        const epNum = ep ? (ep.episode_number || state.activeEpisodeIndex + 1) : 1;
        targetPath = `/watch/${state.activeAnimeId}?ep=${epNum}`;
      }

      const currentState = window.history.state;
      if (!currentState || currentState.viewId !== viewId || currentState.animeId !== state.activeAnimeId || window.location.pathname !== targetPath) {
        window.history.pushState(
          { viewId, tab: state.currentTab, animeId: state.activeAnimeId },
          '',
          targetPath
        );
      }
    } catch (e) {}
  }
}

window.addEventListener('popstate', (e) => {
  isHandlingPopState = true;
  try {
    const path = window.location.pathname;
    const animeMatch = path.match(/^\/anime\/(\d+)/);
    const watchMatch = path.match(/^\/watch\/(\d+)/);

    if (animeMatch) {
      const id = parseInt(animeMatch[1], 10);
      openAnimeDetail(id, false);
    } else if (watchMatch) {
      const id = parseInt(watchMatch[1], 10);
      const searchParams = new URLSearchParams(window.location.search);
      const epNum = parseInt(searchParams.get('ep') || '1', 10);
      openAnimeDetail(id, true, Math.max(0, epNum - 1), epNum);
    } else if (path === '/browse') {
      switchTab('browse');
    } else if (path === '/search') {
      switchTab('search');
    } else if (path === '/watchlist') {
      switchTab('watchlist');
    } else {
      switchTab('home');
    }
  } finally {
    isHandlingPopState = false;
  }
});

function switchTab(tabName) {
  state.currentTab = tabName || 'home';
  if (state.currentTab === 'home') {
    showView('view-home');
    updatePageSeo({
      title: 'KuroAnime — Stream Anime in Sub & Dub',
      description: 'Stream thousands of anime in Sub and Dub with instant player servers, verified ratings, and clean ad-free interface.',
      path: '/'
    });
  } else if (state.currentTab === 'browse') {
    showView('view-browse');
    loadBrowse();
    updatePageSeo({
      title: 'Explore Anime Catalog in Sub & Dub | KuroAnime',
      description: 'Browse popular anime series and movies filtered by genres, format, and status in Japanese Sub and English Dub.',
      path: '/browse'
    });
  } else if (state.currentTab === 'search') {
    showView('view-search');
    setTimeout(() => document.getElementById('search-input-field')?.focus(), 150);
    updatePageSeo({
      title: 'Search Anime | KuroAnime',
      description: 'Search anime by English, Romaji, or Japanese title to stream episodes in Sub and Dub.',
      path: '/search'
    });
  } else if (state.currentTab === 'watchlist') {
    showView('view-watchlist');
    renderWatchlist();
    updatePageSeo({
      title: 'My Collection & Watchlist | KuroAnime',
      description: 'Your saved anime watchlist and continue watching progress.',
      path: '/watchlist'
    });
  } else {
    state.currentTab = 'home';
    showView('view-home');
  }
}

// ─── API Fetch Helper ───────────────────────────────────────────────
async function fetchApi(endpoint) {
  try {
    const res = await fetch(`${API_BASE}${endpoint}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return await res.json();
  } catch (err) {
    console.error('API Error on', endpoint, err);
    return null;
  }
}

// ─── Watchlist & History Management ─────────────────────────────────
function toggleWatchlist(anime) {
  const animeId = Number(anime.anime_id);
  const idx = state.watchlist.findIndex(a => Number(a.anime_id) === animeId);
  const willBeSaved = idx === -1;

  if (idx > -1) {
    state.watchlist.splice(idx, 1);
    showToast(`Removed from Watchlist`);
    saveLibraryItemToCloud({ anime_id: animeId }, false);
  } else {
    const item = {
      anime_id: animeId,
      title: anime.title,
      poster: anime.poster || anime.poster_large || anime.poster_url,
      type: anime.type,
      anilist_score: anime.anilist_score,
      added_at: Date.now()
    };
    state.watchlist.unshift(item);
    showToast(`Added to Watchlist!`);
    saveLibraryItemToCloud(item, true);
  }
  localStorage.setItem('kuro_watchlist', JSON.stringify(state.watchlist));
  updateWatchlistBadge();

  // Dynamically update card library badges across all visible lists
  document.querySelectorAll(`.anime-card[data-id="${animeId}"] .card-poster-wrapper`).forEach(pw => {
    const existingBadge = pw.querySelector('.card-saved-badge');
    if (willBeSaved && !existingBadge) {
      const badge = document.createElement('span');
      badge.className = 'card-saved-badge';
      badge.title = 'In Your Library';
      badge.innerHTML = '<i data-lucide="bookmark"></i>';
      pw.appendChild(badge);
    } else if (!willBeSaved && existingBadge) {
      existingBadge.remove();
    }
  });

  refreshIcons();
}

function isWatchlisted(animeId) {
  return state.watchlist.some(a => Number(a.anime_id) === Number(animeId));
}

function updateWatchlistBadge() {
  const badge = document.getElementById('watchlist-badge');
  if (badge) {
    badge.style.display = state.watchlist.length > 0 ? 'block' : 'none';
  }
}

function renderContinueWatchingRail() {
  const railsContainer = document.getElementById('rails-container');
  if (!railsContainer) return;

  let continueSection = railsContainer.querySelector('.rail-section-continue');
  if (!state.history || state.history.length === 0) {
    if (continueSection) continueSection.remove();
    return;
  }

  const html = `
    <div class="rail-header">
      <span class="rail-title"><span class="rail-title-line"></span> Continue Watching</span>
    </div>
    <div class="rail-scroll-track">
      ${state.history.slice(0, 10).map(h => {
        const epNum = Number(h.episode_number) || 1;
        const pct = (h.duration_seconds && h.duration_seconds > 0)
          ? Math.min(100, Math.round((h.progress_seconds / h.duration_seconds) * 100))
          : 0;
        return `
        <div class="continue-card" data-anime-id="${h.anime_id}" data-ep-idx="${Math.max(0, epNum - 1)}" data-ep-num="${epNum}">
          <div class="continue-thumb-wrapper">
            <img class="continue-thumb-img" src="${h.poster}" alt="${h.title}" loading="lazy" />
            <div class="continue-play-overlay">
              <div class="continue-play-icon"><i data-lucide="play"></i></div>
            </div>
            <span class="continue-ep-badge">EP ${epNum}</span>
            <div class="continue-progress-bar"><div class="continue-progress-fill" style="width: ${pct > 0 ? pct + '%' : '15%'}"></div></div>
          </div>
          <div class="continue-title">${h.title}</div>
          <div class="continue-subtitle">Resume Ep ${epNum}</div>
        </div>
        `;
      }).join('')}
    </div>
  `;

  if (continueSection) {
    continueSection.innerHTML = html;
  } else {
    continueSection = document.createElement('div');
    continueSection.className = 'rail-section rail-section-continue';
    continueSection.innerHTML = html;
    railsContainer.prepend(continueSection);
  }

  continueSection.querySelectorAll('.continue-card').forEach(cc => {
    cc.addEventListener('click', () => {
      const aId = Number(cc.dataset.animeId);
      const epIdx = Math.max(0, Number(cc.dataset.epIdx) || 0);
      const epNum = Number(cc.dataset.epNum) || (epIdx + 1);
      state.playerEnteredFrom = 'home';
      openAnimeDetail(aId, true, epIdx, epNum);
    });
  });
  refreshIcons();
}

function saveHistory(anime, episode, progressSeconds = 0, durationSeconds = 0, completed = false) {
  if (!anime || !episode) return;
  const animeId = Number(anime.anime_id);
  const epNum = Math.max(1, Math.floor(Number(episode.episode_number) || 1));
  const existingIdx = state.history.findIndex(h => Number(h.anime_id) === animeId);
  const existingEntry = existingIdx > -1 ? state.history[existingIdx] : null;
  const sameEpisode = existingEntry && Number(existingEntry.episode_number) === epNum;
  const thumb = anime.banner_image || anime.poster || anime.poster_large || anime.poster_url || existingEntry?.poster || '';

  let prog = Math.max(0, Math.floor(Number(progressSeconds) || 0));
  let dur = Math.max(0, Math.floor(Number(durationSeconds) || 0));

  // Only preserve previous progress if saving the exact same episode
  if (!prog && sameEpisode && existingEntry.progress_seconds) {
    prog = Math.max(0, Math.floor(Number(existingEntry.progress_seconds) || 0));
  } else if (!prog && episode.episode_id) {
    const savedPos = parseInt(localStorage.getItem(`kuro_pos_${animeId}_${episode.episode_id}`) || '0', 10);
    if (savedPos > 0) prog = savedPos;
  }
  if (!dur && sameEpisode && existingEntry.duration_seconds) {
    dur = Math.max(0, Math.floor(Number(existingEntry.duration_seconds) || 0));
  }

  const entry = {
    anime_id: animeId,
    title: anime.title || existingEntry?.title || 'Anime',
    poster: thumb,
    episode_number: epNum,
    episode_title: episode.title || '',
    episode_id: String(episode.episode_id || ''),
    progress_seconds: prog,
    duration_seconds: dur,
    completed: Boolean(completed),
    timestamp: Date.now()
  };
  if (existingIdx > -1) {
    state.history.splice(existingIdx, 1);
  }
  state.history.unshift(entry);
  if (state.history.length > 50) state.history.pop();
  localStorage.setItem('kuro_history', JSON.stringify(state.history));
  renderContinueWatchingRail();
  saveWatchHistoryToCloud(entry);
}

async function syncCloudUserData() {
  if (!supabase) return;
  try {
    const [cloudHistory, cloudLib] = await Promise.all([
      fetchWatchHistoryFromCloud(),
      fetchLibraryFromCloud()
    ]);

    if (cloudHistory && cloudHistory.length > 0) {
      const mappedHistory = cloudHistory.map(row => ({
        anime_id: Number(row.anime_id),
        title: row.anime_title,
        poster: row.anime_poster,
        episode_number: Math.max(1, Math.floor(Number(row.episode_number) || 1)),
        episode_title: row.episode_title,
        episode_id: row.episode_id,
        progress_seconds: Math.max(0, Math.floor(Number(row.progress_seconds) || 0)),
        duration_seconds: Math.max(0, Math.floor(Number(row.duration_seconds) || 0)),
        completed: Boolean(row.completed),
        timestamp: new Date(row.updated_at).getTime() || 0
      }));

      // Merge local history with cloud history — never let an older cloud entry overwrite a newer local episode!
      for (const localItem of state.history) {
        const cloudIdx = mappedHistory.findIndex(c => Number(c.anime_id) === Number(localItem.anime_id));
        if (cloudIdx === -1) {
          saveWatchHistoryToCloud(localItem);
          mappedHistory.push(localItem);
        } else {
          const cloudItem = mappedHistory[cloudIdx];
          const localTime = Number(localItem.timestamp) || 0;
          const cloudTime = Number(cloudItem.timestamp) || 0;
          if (localTime >= cloudTime || (Number(localItem.episode_number) !== Number(cloudItem.episode_number) && localTime > 0)) {
            mappedHistory[cloudIdx] = localItem;
            saveWatchHistoryToCloud(localItem);
          }
        }
      }

      state.history = mappedHistory.sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
      localStorage.setItem('kuro_history', JSON.stringify(state.history));
      renderContinueWatchingRail();
    } else if (state.history.length > 0) {
      state.history.forEach(item => saveWatchHistoryToCloud(item));
    }

    if (cloudLib && cloudLib.length > 0) {
      const mappedLib = cloudLib.map(row => ({
        anime_id: Number(row.anime_id),
        title: row.anime_title,
        poster: row.anime_poster,
        type: row.anime_type || 'TV',
        anilist_score: row.anilist_score,
        status: row.status,
        is_favorite: row.is_favorite,
        added_at: new Date(row.created_at).getTime()
      }));

      for (const localItem of state.watchlist) {
        if (!mappedLib.some(c => Number(c.anime_id) === Number(localItem.anime_id))) {
          saveLibraryItemToCloud(localItem, true);
          mappedLib.push(localItem);
        }
      }

      state.watchlist = mappedLib;
      localStorage.setItem('kuro_watchlist', JSON.stringify(state.watchlist));
      updateWatchlistBadge();
    } else if (state.watchlist.length > 0) {
      state.watchlist.forEach(item => saveLibraryItemToCloud(item, true));
    }

    if (state.currentTab === 'watchlist') {
      renderWatchlist();
    }
  } catch (err) {
    console.warn('[Supabase sync cloud data error]', err);
  }
}

// ─── YouTube-Style Unslop Skeleton Renderers ────────────────────────
function renderCardSkeletons(count = 12) {
  let out = '';
  for (let i = 0; i < count; i++) {
    const titleW = 72 + ((i * 7) % 22);
    const subW = 44 + ((i * 5) % 18);
    out += `
      <div class="yt-card-skeleton" aria-hidden="true">
        <div class="yt-skel-poster yt-shimmer">
          <span class="yt-skel-pill-top"></span>
          <span class="yt-skel-pill-bot"></span>
        </div>
        <div class="yt-skel-line yt-skel-title yt-shimmer" style="width:${titleW}%"></div>
        <div class="yt-skel-line yt-skel-sub yt-shimmer" style="width:${subW}%"></div>
      </div>
    `;
  }
  return out;
}

function renderHomeSkeletons() {
  const stageEl = document.getElementById('hero-stage');
  const contentEl = document.getElementById('hero-content');
  const thumbsEl = document.getElementById('hero-thumbs');
  const railsContainer = document.getElementById('rails-container');

  if (stageEl && !stageEl.querySelector('.hero-slide')) {
    stageEl.innerHTML = '<div class="hero-skeleton yt-shimmer"></div>';
  }
  if (contentEl && !contentEl.querySelector('.hero-title')) {
    contentEl.innerHTML = `
      <div class="yt-hero-skeleton" aria-hidden="true">
        <div class="yt-hero-eyebrow-row">
          <span class="hero-eyebrow-line"></span>
          <span class="yt-skel-line yt-shimmer" style="width:92px;height:12px;border-radius:4px;"></span>
        </div>
        <div class="yt-skel-line yt-shimmer yt-hero-title-1"></div>
        <div class="yt-skel-line yt-shimmer yt-hero-title-2"></div>
        <div class="yt-hero-meta-row">
          <span class="yt-skel-line yt-shimmer" style="width:48px;height:14px;border-radius:4px;"></span>
          <span class="yt-skel-line yt-shimmer" style="width:42px;height:14px;border-radius:4px;"></span>
          <span class="yt-skel-line yt-shimmer" style="width:74px;height:14px;border-radius:4px;"></span>
          <span class="yt-skel-line yt-shimmer" style="width:96px;height:14px;border-radius:4px;"></span>
        </div>
        <div class="yt-skel-line yt-shimmer yt-hero-desc-1"></div>
        <div class="yt-skel-line yt-shimmer yt-hero-desc-2"></div>
        <div class="yt-hero-actions-row">
          <span class="yt-skel-btn yt-shimmer" style="width:104px;height:40px;"></span>
          <span class="yt-skel-btn yt-shimmer" style="width:112px;height:40px;"></span>
        </div>
      </div>
    `;
  }
  if (thumbsEl && !thumbsEl.querySelector('.hero-thumb')) {
    thumbsEl.innerHTML = Array.from({ length: 6 }, () => '<div class="yt-thumb-skel yt-shimmer" aria-hidden="true"></div>').join('');
  }
  if (railsContainer && !railsContainer.querySelector('.anime-card')) {
    const widths = [136, 112, 148];
    railsContainer.innerHTML = widths.map(w => `
      <div class="rail-section yt-rail-skeleton" aria-hidden="true">
        <div class="rail-header">
          <span class="rail-title"><span class="rail-title-line"></span><span class="yt-skel-line yt-skel-rail-heading yt-shimmer" style="width:${w}px"></span></span>
          <span class="yt-skel-line yt-skel-rail-more yt-shimmer"></span>
        </div>
        <div class="rail-scroll-track">
          ${renderCardSkeletons(8)}
        </div>
      </div>
    `).join('');
  }
}

function renderDetailSkeleton() {
  const epBones = Array.from({ length: 18 }, () => '<div class="yt-skel-ep-tile yt-shimmer"></div>').join('');
  return `
    <div class="detail-banner yt-shimmer" style="background-color: var(--bg-surface);">
      <div class="detail-banner-overlay"></div>
      <button class="detail-back-btn" id="detail-loading-back-btn">
        <i data-lucide="arrow-left"></i> Back
      </button>
    </div>
    <div class="detail-body yt-detail-skeleton" aria-hidden="true">
      <div class="detail-header-card">
        <div class="detail-poster yt-shimmer"></div>
        <div class="detail-info">
          <div class="yt-skel-line yt-shimmer" style="width:min(340px, 78%);height:28px;border-radius:6px;margin-bottom:6px;"></div>
          <div class="yt-skel-line yt-shimmer" style="width:min(210px, 52%);height:14px;border-radius:4px;margin-bottom:10px;"></div>
          <div class="detail-meta-row">
            <span class="yt-skel-line yt-shimmer" style="width:52px;height:22px;border-radius:5px;"></span>
            <span class="yt-skel-line yt-shimmer" style="width:42px;height:22px;border-radius:5px;"></span>
            <span class="yt-skel-line yt-shimmer" style="width:88px;height:22px;border-radius:5px;"></span>
            <span class="yt-skel-line yt-shimmer" style="width:46px;height:22px;border-radius:5px;"></span>
          </div>
        </div>
      </div>
      <div class="detail-actions-bar">
        <div class="yt-skel-btn yt-shimmer" style="flex:2;height:42px;border-radius:8px;"></div>
        <div class="yt-skel-btn yt-shimmer" style="flex:1;height:42px;border-radius:8px;"></div>
      </div>
      <div class="detail-synopsis-box" style="display:flex;flex-direction:column;gap:10px;">
        <div class="yt-skel-line yt-shimmer" style="width:84px;height:13px;border-radius:4px;"></div>
        <div class="yt-skel-line yt-shimmer" style="width:96%;height:12px;border-radius:4px;"></div>
        <div class="yt-skel-line yt-shimmer" style="width:91%;height:12px;border-radius:4px;"></div>
        <div class="yt-skel-line yt-shimmer" style="width:68%;height:12px;border-radius:4px;"></div>
      </div>
      <div class="episodes-section">
        <div class="section-label-row" style="margin-bottom:12px;">
          <div class="yt-skel-line yt-shimmer" style="width:120px;height:15px;border-radius:4px;"></div>
        </div>
        <div class="episodes-list-grid">
          ${epBones}
        </div>
      </div>
    </div>
  `;
}

function renderPlayerFrameSkeleton() {
  return `
    <div class="yt-player-skeleton" aria-hidden="true">
      <div class="yt-player-shimmer-bg yt-shimmer"></div>
      <div class="yt-player-center-ring">
        <span class="yt-player-spinner"></span>
      </div>
      <div class="yt-player-bottom-bar">
        <div class="yt-player-scrubber yt-shimmer"></div>
        <div class="yt-player-controls-row">
          <div class="yt-player-ctrl-left">
            <span class="yt-skel-line yt-shimmer" style="width:28px;height:22px;border-radius:5px;"></span>
            <span class="yt-skel-line yt-shimmer" style="width:28px;height:22px;border-radius:5px;"></span>
            <span class="yt-skel-line yt-shimmer" style="width:76px;height:14px;border-radius:4px;"></span>
          </div>
          <div class="yt-player-ctrl-right">
            <span class="yt-skel-line yt-shimmer" style="width:54px;height:22px;border-radius:5px;"></span>
            <span class="yt-skel-line yt-shimmer" style="width:28px;height:22px;border-radius:5px;"></span>
          </div>
        </div>
      </div>
    </div>
  `;
}
// ─── End YouTube-Style Unslop Skeleton Renderers ────────────────────

// ─── Dynamic SEO Metadata, Canonical & JSON-LD Updater ──────────────
function updatePageSeo(options = {}) {
  const {
    title = 'KuroAnime — Stream Anime in Sub & Dub',
    description = 'Stream thousands of anime in Sub and Dub with instant player servers, verified ratings, and clean ad-free interface.',
    image = 'https://kuroanime-flax.vercel.app/goku-banner.jpg',
    path = window.location.pathname || '/',
    type = 'website',
    schema = null
  } = options;

  document.title = title;

  let descMeta = document.querySelector('meta[name="description"]');
  if (descMeta) {
    descMeta.setAttribute('content', description);
  }

  const canonicalUrl = `https://kuroanime-flax.vercel.app${path}`;
  let canonEl = document.getElementById('kuro-canonical') || document.querySelector('link[rel="canonical"]');
  if (canonEl) {
    canonEl.setAttribute('href', canonicalUrl);
  }

  const setMetaProp = (id, prop, val) => {
    let el = document.getElementById(id) || document.querySelector(`meta[property="${prop}"]`);
    if (el) el.setAttribute('content', val);
  };
  setMetaProp('kuro-og-type', 'og:type', type);
  setMetaProp('kuro-og-url', 'og:url', canonicalUrl);
  setMetaProp('kuro-og-title', 'og:title', title);
  setMetaProp('kuro-og-desc', 'og:description', description);
  setMetaProp('kuro-og-image', 'og:image', image);

  const setMetaNamed = (id, name, val) => {
    let el = document.getElementById(id) || document.querySelector(`meta[name="${name}"]`);
    if (el) el.setAttribute('content', val);
  };
  setMetaNamed('kuro-tw-url', 'twitter:url', canonicalUrl);
  setMetaNamed('kuro-tw-title', 'twitter:title', title);
  setMetaNamed('kuro-tw-desc', 'twitter:description', description);
  setMetaNamed('kuro-tw-image', 'twitter:image', image);

  let schemaScript = document.getElementById('kuro-schema-json');
  if (schemaScript) {
    if (schema) {
      schemaScript.textContent = JSON.stringify(schema, null, 2);
    } else {
      schemaScript.textContent = JSON.stringify({
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "KuroAnime",
        "url": "https://kuroanime-flax.vercel.app/",
        "description": "Stream thousands of anime in Sub and Dub with verified fast servers.",
        "potentialAction": {
          "@type": "SearchAction",
          "target": "https://kuroanime-flax.vercel.app/search?q={search_term_string}",
          "query-input": "required name=search_term_string"
        }
      }, null, 2);
    }
  }
}

// ─── Card Renderer Component ────────────────────────────────────────
function renderAnimeCard(anime) {
  const poster = anime.poster || anime.poster_large || anime.poster_url || '';
  const score = anime.anilist_score ? (anime.anilist_score / 10).toFixed(1) : (anime.mal_score || '');
  const hasSub = anime.sub !== false;
  const hasDub = anime.dub === true;
  const inWatchlist = isWatchlisted(anime.anime_id);

  return `
    <a class="anime-card" href="/anime/${anime.anime_id}" data-id="${anime.anime_id}">
      <div class="card-poster-wrapper">
        <img class="card-poster-img" src="${poster}" alt="${anime.title} anime poster" loading="lazy" decoding="async" onload="this.classList.add('is-loaded')" onerror="this.onerror=null;this.src='/favicon.svg';this.classList.add('is-fallback')" />
        <div class="card-top-badges">
          ${hasSub ? '<span class="pill-sub">SUB</span>' : ''}
          ${hasDub ? '<span class="pill-dub">DUB</span>' : ''}
        </div>
        ${inWatchlist ? `<span class="card-saved-badge" title="In Your Library"><i data-lucide="bookmark"></i></span>` : ''}
        <div class="card-bottom-badges">
          <span class="badge-eps">${anime.episode_count ? `${anime.episode_count} EPS` : (anime.type || 'TV')}</span>
          ${score ? `<span class="badge-score"><i data-lucide="star"></i> ${score}</span>` : ''}
        </div>
      </div>
      <div class="card-title">${anime.title}</div>
      <div class="card-subtitle">${anime.type || 'TV'}${anime.season_year ? ` • ${anime.season_year}` : ''}</div>
    </a>
  `;
}

// ─── Hero Cinema Spotlight Controller ─────────────────────────────
let heroCarouselState = {
  items: [],
  currentIndex: 0,
  timer: null,
  duration: 8000,
  isPaused: false,
  touchStartX: 0
};

function initHeroSpotlight(spotlightList) {
  if (!spotlightList || !spotlightList.length) return;

  const stageEl = document.getElementById('hero-stage');
  const thumbsEl = document.getElementById('hero-thumbs');
  const dotsEl = document.getElementById('hero-dots');
  const contentEl = document.getElementById('hero-content');
  const heroSection = document.getElementById('hero-section');

  if (!stageEl || !contentEl) return;

  heroCarouselState.items = spotlightList.slice(0, 8);
  const items = heroCarouselState.items;
  heroCarouselState.currentIndex = 0;

  // 1. Render slides in stage with responsive 1080p/4K backdrops optimized for mobile & PC
  stageEl.innerHTML = items.map((item, idx) => {
    let rawBanner = item.banner_image || item.poster_large || item.poster || '';
    let pcBanner = rawBanner;
    let ambientBanner = rawBanner;
    let srcsetAttr = '';
    if (rawBanner.includes('image.tmdb.org')) {
      const w780 = rawBanner.replace(/\/t\/p\/[^/]+/, '/t/p/w780');
      const w1280 = rawBanner.replace(/\/t\/p\/[^/]+/, '/t/p/w1280');
      const w1920 = rawBanner.replace(/\/t\/p\/[^/]+/, '/t/p/w1920_and_h1080_multi_faces');
      const orig = rawBanner.replace(/\/t\/p\/[^/]+/, '/t/p/original');
      pcBanner = orig;
      ambientBanner = w780;
      srcsetAttr = `srcset="${w780} 780w, ${w1280} 1280w, ${w1920} 1920w, ${orig} 3840w" sizes="100vw"`;
    }
    return `
      <div class="hero-slide ${idx === 0 ? 'active' : ''}" data-idx="${idx}">
        <div class="hero-slide-ambient" style="background-image: url('${ambientBanner}')" aria-hidden="true"></div>
        <img class="hero-slide-img" src="${pcBanner}" ${srcsetAttr} alt="${item.title}" loading="${idx === 0 ? 'eager' : 'lazy'}" fetchpriority="${idx === 0 ? 'high' : 'auto'}" decoding="async" />
      </div>
    `;
  }).join('');

  // 2. Render desktop thumbnails (up to 6)
  if (thumbsEl) {
    const thumbItems = items.slice(0, 6);
    thumbsEl.innerHTML = thumbItems.map((item, idx) => {
      let thumb = item.banner_image || item.poster || '';
      if (thumb.includes('image.tmdb.org')) {
        thumb = thumb.replace(/\/t\/p\/[^/]+/, '/t/p/w780');
      }
      return `
        <button class="hero-thumb ${idx === 0 ? 'active' : ''}" data-idx="${idx}" aria-label="Slide ${idx + 1}: ${item.title}">
          <img src="${thumb}" alt="" loading="lazy" />
          <span class="hero-thumb-bar"></span>
        </button>
      `;
    }).join('');

    thumbsEl.querySelectorAll('.hero-thumb').forEach(btn => {
      btn.addEventListener('click', () => {
        goToHeroSlide(parseInt(btn.dataset.idx, 10));
      });
    });
  }

  // 3. Render mobile dots
  if (dotsEl) {
    dotsEl.innerHTML = items.map((_, idx) => `
      <button class="${idx === 0 ? 'active' : ''}" data-idx="${idx}" aria-label="Slide ${idx + 1}"></button>
    `).join('');

    dotsEl.querySelectorAll('button').forEach(btn => {
      btn.addEventListener('click', () => {
        goToHeroSlide(parseInt(btn.dataset.idx, 10));
      });
    });
  }

  // 4. Render initial slide content
  renderHeroContent(0);

  // 5. Setup interaction listeners (once)
  if (heroSection && !heroSection.dataset.listenersAttached) {
    heroSection.dataset.listenersAttached = 'true';

    // Hover pause
    heroSection.addEventListener('mouseenter', () => {
      heroCarouselState.isPaused = true;
      heroSection.classList.add('paused');
      clearTimeout(heroCarouselState.timer);
    });

    heroSection.addEventListener('mouseleave', () => {
      heroCarouselState.isPaused = false;
      heroSection.classList.remove('paused');
      scheduleHeroNext(heroCarouselState.duration);
    });

    // Mobile touch swipe (>40px)
    heroSection.addEventListener('touchstart', e => {
      heroCarouselState.touchStartX = e.changedTouches[0].screenX;
    }, { passive: true });

    heroSection.addEventListener('touchend', e => {
      const touchEndX = e.changedTouches[0].screenX;
      const diff = touchEndX - heroCarouselState.touchStartX;
      if (Math.abs(diff) > 42) {
        if (diff < 0) {
          goToHeroSlide((heroCarouselState.currentIndex + 1) % heroCarouselState.items.length);
        } else {
          goToHeroSlide((heroCarouselState.currentIndex - 1 + heroCarouselState.items.length) % heroCarouselState.items.length);
        }
      }
    }, { passive: true });
  }

  // Schedule autoplay
  scheduleHeroNext(heroCarouselState.duration);
}

function scheduleHeroNext(delay) {
  clearTimeout(heroCarouselState.timer);
  if (heroCarouselState.isPaused) return;
  heroCarouselState.timer = setTimeout(() => {
    const nextIdx = (heroCarouselState.currentIndex + 1) % heroCarouselState.items.length;
    goToHeroSlide(nextIdx);
  }, delay);
}

function goToHeroSlide(index) {
  if (index < 0 || index >= heroCarouselState.items.length) return;
  heroCarouselState.currentIndex = index;

  // 1. Update slides
  const slides = document.querySelectorAll('#hero-stage .hero-slide');
  slides.forEach((sl, idx) => {
    sl.classList.toggle('active', idx === index);
  });

  // 2. Update thumbnails (re-trigger CSS progress bar animation)
  const thumbs = document.querySelectorAll('#hero-thumbs .hero-thumb');
  thumbs.forEach((th, idx) => {
    th.classList.toggle('active', idx === index);
    if (idx === index) {
      const bar = th.querySelector('.hero-thumb-bar');
      if (bar) {
        bar.style.animation = 'none';
        void bar.offsetWidth;
        bar.style.animation = '';
      }
    }
  });

  // 3. Update dots
  const dots = document.querySelectorAll('#hero-dots button');
  dots.forEach((dot, idx) => {
    dot.classList.toggle('active', idx === index);
  });

  // 4. Render text content with staggered reveal
  renderHeroContent(index);

  // 5. Schedule next
  scheduleHeroNext(heroCarouselState.duration);
}

function renderHeroContent(index) {
  const contentEl = document.getElementById('hero-content');
  if (!contentEl) return;

  const item = heroCarouselState.items[index];
  if (!item) return;

  const title = item.title_english || item.title || 'Untitled Anime';
  const score = item.anilist_score ? (item.anilist_score / 10).toFixed(1) : (item.mal_score || '');
  const year = item.season_year || (item.aired ? item.aired.match(/\d{4}/)?.[0] : '');
  const type = item.type || 'TV';
  const eps = item.episode_count ? `${item.episode_count} Episodes` : '';

  // Clean genres (supports both comma-separated and space-separated formats)
  const rawGenres = item.genres || '';
  const genreTokens = (rawGenres.includes(',') ? rawGenres.split(',') : rawGenres.split(/\s+/)).map(g => g.trim()).filter(Boolean);
  const genresStr = genreTokens.slice(0, 2).join(' / ');

  // Clean synopsis (strip HTML tags)
  const synopsis = (item.synopsis || '').replace(/<[^>]*>?/gm, '').trim();

  // Watchlist state
  const inList = state.watchlist.some(w => w.anime_id === item.anime_id);

  // Build metadata tokens
  const metaParts = [];
  if (score) metaParts.push(`<span class="score">★ ${score}</span>`);
  if (year) metaParts.push(`<span>${year}</span>`);
  if (type) metaParts.push(`<span>${type}</span>`);
  if (eps) metaParts.push(`<span>${eps}</span>`);
  if (genresStr) metaParts.push(`<span>${genresStr}</span>`);

  const metaHtml = metaParts.join('<span class="dot"></span>');

  contentEl.innerHTML = `
    <div class="hero-eyebrow">
      <span class="hero-eyebrow-line"></span>
      #${index + 1} Spotlight
    </div>
    <h1 class="hero-title">${title}</h1>
    <div class="hero-meta">
      ${metaHtml}
    </div>
    <div class="hero-tags-row">
      ${item.sub !== false ? '<span class="hero-tag">SUB</span>' : ''}
      ${item.dub === true ? '<span class="hero-tag">DUB</span>' : ''}
    </div>
    ${synopsis ? `<p class="hero-synopsis">${synopsis}</p>` : ''}
    <div class="hero-actions">
      <button class="btn-hero-play" id="hero-btn-play" data-id="${item.anime_id}">
        <svg viewBox="0 0 24 24"><polygon points="6 3 20 12 6 21 6 3"/></svg>
        Play
      </button>
      <button class="btn-hero-ghost ${inList ? 'active' : ''}" id="hero-btn-list" data-id="${item.anime_id}">
        <i data-lucide="${inList ? 'check' : 'plus'}"></i>
        <span id="hero-list-label">${inList ? 'In List' : 'My List'}</span>
      </button>
    </div>
  `;

  // Staggered reveal animation
  contentEl.classList.remove('swap');
  void contentEl.offsetWidth; // force reflow
  contentEl.classList.add('swap');

  // Convert lucide tags
  refreshIcons();

  // Button actions
  document.getElementById('hero-btn-play')?.addEventListener('click', () => {
    state.playerEnteredFrom = 'home';
    const saved = state.history.find(h => Number(h.anime_id) === Number(item.anime_id));
    const savedEpNum = saved ? Number(saved.episode_number) || 1 : 1;
    openAnimeDetail(item.anime_id, true, Math.max(0, savedEpNum - 1), savedEpNum);
  });

  document.getElementById('hero-btn-list')?.addEventListener('click', () => {
    toggleWatchlist(item);
    const nowInList = state.watchlist.some(w => w.anime_id === item.anime_id);
    const btn = document.getElementById('hero-btn-list');
    if (btn) {
      btn.classList.toggle('active', nowInList);
      btn.innerHTML = `<i data-lucide="${nowInList ? 'check' : 'plus'}"></i><span id="hero-list-label">${nowInList ? 'In List' : 'My List'}</span>`;
      refreshIcons();
    }
  });
}

// ─── Home View ──────────────────────────────────────────────────────
async function initHome() {
  renderHomeSkeletons();
  // 1. Catalog Stats (Populates footer quietly)
  fetchApi('/stats').then(stats => {
    if (stats) {
      const footerStat = document.getElementById('footer-catalog-stats');
      if (footerStat) {
        footerStat.textContent = `${stats.anime.toLocaleString()} titles · ${stats.streams.toLocaleString()} streams`;
      }
    }
  });

  // 2. Home Rails & Spotlight
  const homeData = await fetchApi('/home');
  if (!homeData) {
    const stageEl = document.getElementById('hero-stage');
    if (stageEl) {
      stageEl.innerHTML = `
        <div class="empty-state" style="padding: 120px 20px; text-align: center; color: var(--text-secondary);">
          <i data-lucide="wifi-off" style="width: 44px; height: 44px; stroke: #ff5a3c; margin-bottom: 12px;"></i>
          <h2 style="font-size: 1.4rem; color: #fff; margin-bottom: 8px;">Catalog Connection Offline</h2>
          <p style="font-size: 0.9rem; max-width: 440px; margin: 0 auto 20px;">Could not load spotlight from <code>${API_BASE}/home</code>. Please verify backend service.</p>
          <button class="btn-hero-play" onclick="location.reload()" style="display:inline-flex; margin: 0 auto;">Retry</button>
        </div>
      `;
      refreshIcons();
    }
    return;
  }

  // Render Professional Cinema Hero Spotlight
  initHeroSpotlight(homeData.spotlight || []);

  // Render Rails
  const railsContainer = document.getElementById('rails-container');
  if (railsContainer && homeData.rails) {
    const railConfigs = [
      { key: 'trending', title: 'Trending Now' },
      { key: 'top_rated', title: 'Top Rated' },
      { key: 'airing', title: 'Currently Airing' },
      { key: 'movies', title: 'Movies & Feature Films' },
      { key: 'recent', title: 'Recently Added' },
    ];

    const catalogRailsHtml = railConfigs.map(cfg => {
      const items = homeData.rails[cfg.key] || [];
      if (!items.length) return '';
      return `
        <div class="rail-section">
          <div class="rail-header">
            <span class="rail-title"><span class="rail-title-line"></span> ${cfg.title}</span>
            <button class="rail-see-all" data-rail="${cfg.key}">View All</button>
          </div>
          <div class="rail-scroll-track">
            ${items.map(renderAnimeCard).join('')}
          </div>
        </div>
      `;
    }).join('');

    railsContainer.innerHTML = catalogRailsHtml;
    renderContinueWatchingRail();

    // Attach card clicks
    railsContainer.querySelectorAll('.anime-card').forEach(card => {
      card.addEventListener('click', (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        openAnimeDetail(Number(card.dataset.id));
      });
    });

    railsContainer.querySelectorAll('.rail-see-all').forEach(btn => {
      btn.addEventListener('click', () => {
        const rail = btn.dataset.rail;
        const statusSelect = document.getElementById('browse-status-select');
        const typeSelect = document.getElementById('browse-type-select');
        const sortSelect = document.getElementById('browse-sort-select');

        if (rail === 'airing') {
          state.browseStatus = 'airing';
          state.browseSort = 'popular';
          state.browseType = '';
          if (statusSelect) statusSelect.value = 'airing';
          if (typeSelect) typeSelect.value = '';
          if (sortSelect) sortSelect.value = 'popular';
        } else if (rail === 'top_rated') {
          state.browseStatus = '';
          state.browseSort = 'score';
          if (statusSelect) statusSelect.value = '';
          if (sortSelect) sortSelect.value = 'score';
        } else if (rail === 'movies') {
          state.browseStatus = '';
          state.browseType = 'Movie';
          state.browseSort = 'popular';
          if (statusSelect) statusSelect.value = '';
          if (typeSelect) typeSelect.value = 'Movie';
          if (sortSelect) sortSelect.value = 'popular';
        } else {
          state.browseStatus = '';
          state.browseSort = 'popular';
          if (statusSelect) statusSelect.value = '';
          if (sortSelect) sortSelect.value = 'popular';
        }
        syncCustomSelectDisplay('browse-status-select');
        syncCustomSelectDisplay('browse-type-select');
        syncCustomSelectDisplay('browse-sort-select');
        state.browsePage = 1;
        switchTab('browse');
      });
    });
  }

  refreshIcons();
}

// ─── Custom Select Synchronization ──────────────────────────────────
function syncCustomSelectDisplay(selectId) {
  const select = document.getElementById(selectId);
  if (!select) return;
  const wrap = select.closest('.custom-select-wrapper');
  if (!wrap) return;
  const val = select.value;
  const opt = wrap.querySelector(`.custom-select-option[data-value="${val}"]`);
  if (opt) {
    wrap.querySelectorAll('.custom-select-option').forEach(o => o.classList.remove('active'));
    opt.classList.add('active');
    const label = wrap.querySelector('.custom-select-label');
    if (label) label.textContent = opt.textContent.replace('✓', '').trim();
  }
}

function initCustomSelects() {
  document.querySelectorAll('.custom-select-wrapper').forEach(wrap => {
    const trigger = wrap.querySelector('.custom-select-trigger');
    const label = wrap.querySelector('.custom-select-label');
    const nativeSelect = wrap.querySelector('.filter-select');
    const options = wrap.querySelectorAll('.custom-select-option');

    trigger?.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = wrap.classList.contains('open');
      // Close other open custom selects
      document.querySelectorAll('.custom-select-wrapper.open').forEach(w => {
        if (w !== wrap) {
          w.classList.remove('open');
          w.querySelector('.custom-select-trigger')?.setAttribute('aria-expanded', 'false');
        }
      });
      wrap.classList.toggle('open', !wasOpen);
      trigger.setAttribute('aria-expanded', String(!wasOpen));
    });

    options.forEach(opt => {
      opt.addEventListener('click', (e) => {
        e.stopPropagation();
        const val = opt.dataset.value;
        const text = opt.textContent.replace('✓', '').trim();
        if (label) label.textContent = text;
        
        options.forEach(o => o.classList.remove('active'));
        opt.classList.add('active');

        wrap.classList.remove('open');
        trigger?.setAttribute('aria-expanded', 'false');

        if (nativeSelect && nativeSelect.value !== val) {
          nativeSelect.value = val;
          nativeSelect.dispatchEvent(new Event('change', { bubbles: true }));
        }
      });
    });
  });

  // Global click outside to close dropdowns
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.custom-select-wrapper')) {
      document.querySelectorAll('.custom-select-wrapper.open').forEach(w => {
        w.classList.remove('open');
        w.querySelector('.custom-select-trigger')?.setAttribute('aria-expanded', 'false');
      });
    }
  });

  // Close on Escape key
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      document.querySelectorAll('.custom-select-wrapper.open').forEach(w => {
        w.classList.remove('open');
        w.querySelector('.custom-select-trigger')?.setAttribute('aria-expanded', 'false');
      });
    }
  });
}

// ─── Browse View ────────────────────────────────────────────────────
async function loadBrowse() {
  syncCustomSelectDisplay('browse-status-select');
  syncCustomSelectDisplay('browse-type-select');
  syncCustomSelectDisplay('browse-sort-select');

  // Load genres once
  const genreContainer = document.getElementById('browse-genres-list');
  if (genreContainer && genreContainer.children.length <= 1) {
    const data = await fetchApi('/genres');
    if (data?.genres) {
      genreContainer.innerHTML = `
        <button class="chip-btn ${state.browseGenre === '' ? 'active' : ''}" data-genre="">All Genres</button>
        ${data.genres.map(g => `<button class="chip-btn ${state.browseGenre === g ? 'active' : ''}" data-genre="${g}">${g}</button>`).join('')}
      `;
      genreContainer.querySelectorAll('.chip-btn').forEach(btn => {
        btn.addEventListener('click', () => {
          genreContainer.querySelectorAll('.chip-btn').forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          state.browseGenre = btn.dataset.genre;
          state.browsePage = 1;
          fetchBrowseResults();
        });
      });
    }
  }

  fetchBrowseResults();
}

async function fetchBrowseResults() {
  const grid = document.getElementById('browse-grid');
  if (!grid) return;
  const paginationEl = document.getElementById('browse-pagination');
  if (paginationEl) paginationEl.style.display = 'none';
  grid.innerHTML = renderCardSkeletons(18);

  const params = new URLSearchParams({
    page: state.browsePage,
    sort: state.browseSort,
    per_page: 24,
  });
  if (state.browseGenre) params.append('genre', state.browseGenre);
  if (state.browseType) params.append('type', state.browseType);
  if (state.browseStatus) params.append('status', state.browseStatus);

  // Dynamic Browse / Genre SEO updates
  if (state.browseGenre) {
    updatePageSeo({
      title: `${state.browseGenre} Anime — Stream in Sub & Dub | KuroAnime`,
      description: `Watch the best ${state.browseGenre} anime series and movies in Japanese Sub and English Dub with fast servers on KuroAnime.`,
      path: `/browse?genre=${encodeURIComponent(state.browseGenre)}`
    });
    try {
      if (window.location.pathname === '/browse') {
        window.history.replaceState(
          { viewId: 'view-browse', tab: 'browse', genre: state.browseGenre },
          '',
          `/browse?genre=${encodeURIComponent(state.browseGenre)}`
        );
      }
    } catch (e) {}
  } else {
    updatePageSeo({
      title: 'Explore Anime Catalog in Sub & Dub | KuroAnime',
      description: 'Browse popular anime series and movies filtered by genres, format, and status in Japanese Sub and English Dub.',
      path: '/browse'
    });
  }

  const res = await fetchApi(`/browse?${params.toString()}`);
  if (!res || !res.results?.length) {
    grid.innerHTML = '<div class="empty-state">No anime found matching filters.</div>';
    const pagination = document.getElementById('browse-pagination');
    if (pagination) pagination.style.display = 'none';
    return;
  }

  grid.innerHTML = res.results.map(renderAnimeCard).join('');
  grid.querySelectorAll('.anime-card').forEach(card => {
    card.addEventListener('click', (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        openAnimeDetail(Number(card.dataset.id));
      });
  });

  const pagination = document.getElementById('browse-pagination');
  if (pagination) {
    pagination.style.display = 'flex';
    const pageInd = document.getElementById('browse-page-indicator');
    if (pageInd) pageInd.textContent = `Page ${state.browsePage}`;
    const prevBtn = document.getElementById('browse-prev-page');
    if (prevBtn) {
      prevBtn.disabled = state.browsePage <= 1;
    }
    const nextBtn = document.getElementById('browse-next-page');
    if (nextBtn) {
      nextBtn.disabled = (res.results || []).length < 24;
    }
  }

  refreshIcons();
}

// ─── Search View ────────────────────────────────────────────────────
async function performSearch(query) {
  const resultsGrid = document.getElementById('search-results-grid');
  const suggestionsBox = document.getElementById('search-recent-box');
  const clearBtn = document.getElementById('search-clear-btn');
  if (!resultsGrid) return;

  if (!query.trim()) {
    resultsGrid.innerHTML = '';
    if (suggestionsBox) suggestionsBox.style.display = 'block';
    if (clearBtn) clearBtn.style.display = 'none';
    return;
  }

  if (suggestionsBox) suggestionsBox.style.display = 'none';
  if (clearBtn) clearBtn.style.display = 'flex';
  resultsGrid.innerHTML = renderCardSkeletons(12);

  // Dynamic Search SEO and URL tracking
  updatePageSeo({
    title: `Watch ${query} Online Sub & Dub | KuroAnime`,
    description: `Search results and streaming episodes for ${query} in Japanese Sub and English Dub on KuroAnime.`,
    path: `/search?q=${encodeURIComponent(query)}`
  });
  try {
    window.history.replaceState(
      { viewId: 'view-search', tab: 'search', q: query },
      '',
      `/search?q=${encodeURIComponent(query)}`
    );
  } catch (e) {}

  const res = await fetchApi(`/search?q=${encodeURIComponent(query)}`);
  if (!res || !res.results?.length) {
    resultsGrid.innerHTML = '<div class="empty-state">No matching anime found.</div>';
    return;
  }

  resultsGrid.innerHTML = res.results.map(renderAnimeCard).join('');
  resultsGrid.querySelectorAll('.anime-card').forEach(card => {
    card.addEventListener('click', (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        openAnimeDetail(Number(card.dataset.id));
      });
  });
  refreshIcons();
}

// ─── Anime Detail View ──────────────────────────────────────────────
async function openAnimeDetail(animeId, autoPlayFirst = false, startEpIndex = 0, targetEpisodeNumber = null) {
  state.activeAnimeId = animeId;
  state.isFetchingDetail = true;

  const container = document.getElementById('detail-container');
  if (!autoPlayFirst) {
    state.playerEnteredFrom = 'detail';
    if (container && (!container.querySelector('.detail-body') || Number(container.dataset.renderedAnimeId) !== Number(animeId))) {
      container.innerHTML = renderDetailSkeleton();
      refreshIcons();
      document.getElementById('detail-loading-back-btn')?.addEventListener('click', () => {
        state.isFetchingDetail = false;
        switchTab('home');
      });
    }
    showView('view-detail');
  }

  let anime = null;
  let epsData = null;
  try {
    [anime, epsData] = await Promise.all([
      fetchApi(`/anime/${animeId}`),
      fetchApi(`/anime/${animeId}/episodes`)
    ]);
  } finally {
    state.isFetchingDetail = false;
  }

  if (!anime) {
    switchTab('home');
    showToast('Could not load anime details');
    return;
  }

  if (container) {
    container.dataset.renderedAnimeId = String(animeId);
  }

  state.activeAnimeData = anime;
  state.activeEpisodes = epsData?.episodes || [];

  // Update dynamic SEO title, description, canonical link, Open Graph and Schema.org TVSeries/Movie
  const animeTitle = anime.title_english || anime.title || 'Anime';
  const scoreVal = anime.anilist_score ? (anime.anilist_score / 10).toFixed(1) : (anime.mal_score || 'N/A');
  const synopsisClean = (anime.synopsis || anime.al_description || '').replace(/<[^>]*>?/gm, '').trim();
  const metaDesc = synopsisClean.length > 155
    ? synopsisClean.slice(0, 152) + '...'
    : (synopsisClean || `Watch ${animeTitle} in Sub or Dub on KuroAnime. Episodes, ratings, and My List.`);

  // Phase 2: Rich Multi-Entity Schema.org Graph (BreadcrumbList + TVSeries/Movie + FAQPage)
  const primaryGenre = (anime.genres || '').split(/\s+/).filter(Boolean)[0] || 'Anime';
  const totalEpisodeCount = epsData?.episodes?.length || anime.episode_count || anime.al_episodes || 0;

  const breadcrumbListSchema = {
    "@type": "BreadcrumbList",
    "itemListElement": [
      {
        "@type": "ListItem",
        "position": 1,
        "name": "Home",
        "item": "https://kuroanime-flax.vercel.app/"
      },
      {
        "@type": "ListItem",
        "position": 2,
        "name": "Browse",
        "item": "https://kuroanime-flax.vercel.app/browse"
      },
      {
        "@type": "ListItem",
        "position": 3,
        "name": primaryGenre,
        "item": `https://kuroanime-flax.vercel.app/browse?genre=${encodeURIComponent(primaryGenre)}`
      },
      {
        "@type": "ListItem",
        "position": 4,
        "name": animeTitle,
        "item": `https://kuroanime-flax.vercel.app/anime/${anime.anime_id}`
      }
    ]
  };

  const entitySchema = {
    "@type": anime.type === 'Movie' ? 'Movie' : 'TVSeries',
    "name": animeTitle,
    "alternateName": anime.title_english && anime.title_english !== anime.title ? anime.title_english : undefined,
    "description": synopsisClean,
    "image": anime.poster || anime.banner_image,
    "numberOfEpisodes": totalEpisodeCount || undefined,
    "inLanguage": ["Japanese", "English"],
    "genre": (anime.genres || '').split(/\s+/).filter(Boolean),
    "aggregateRating": scoreVal !== 'N/A' ? {
      "@type": "AggregateRating",
      "ratingValue": String(scoreVal),
      "bestRating": "10",
      "worstRating": "1"
    } : undefined
  };

  const faqSchema = {
    "@type": "FAQPage",
    "mainEntity": [
      {
        "@type": "Question",
        "name": `Where can I watch ${animeTitle} in English Sub and Dub?`,
        "acceptedAnswer": {
          "@type": "Answer",
          "text": `You can stream all released episodes of ${animeTitle} in HD with Japanese audio + English subtitles and English dub on KuroAnime with fast, verified servers.`
        }
      },
      {
        "@type": "Question",
        "name": `How many episodes are there in ${animeTitle}?`,
        "acceptedAnswer": {
          "@type": "Answer",
          "text": `${animeTitle} has ${totalEpisodeCount || 'multiple'} episodes available for instant streaming on KuroAnime.`
        }
      },
      {
        "@type": "Question",
        "name": `Is ${animeTitle} available in English Dub?`,
        "acceptedAnswer": {
          "@type": "Answer",
          "text": `${anime.dub === true ? `Yes, ${animeTitle} is available with English Dub on KuroAnime.` : `${animeTitle} is available with English Subtitles and multi-server playback.`}`
        }
      }
    ]
  };

  updatePageSeo({
    title: `Watch ${animeTitle} Sub & Dub | KuroAnime`,
    description: metaDesc,
    image: anime.banner_image || anime.poster_large || anime.poster || 'https://kuroanime-flax.vercel.app/goku-banner.jpg',
    path: `/anime/${anime.anime_id}`,
    type: 'video.tv_show',
    schema: {
      "@context": "https://schema.org",
      "@graph": [breadcrumbListSchema, entitySchema, faqSchema]
    }
  });

  // Resolve user's saved watch history for this anime so resume works accurately
  const savedHist = state.history.find(h => Number(h.anime_id) === Number(animeId));
  const desiredEpNum = targetEpisodeNumber || (savedHist ? Number(savedHist.episode_number) : null);

  let resolvedEpIdx = Math.min(Math.max(0, startEpIndex || 0), Math.max(0, state.activeEpisodes.length - 1));
  if (desiredEpNum && state.activeEpisodes.length > 0) {
    const matchByNum = state.activeEpisodes.findIndex(e => Number(e.episode_number) === Number(desiredEpNum));
    if (matchByNum !== -1) {
      resolvedEpIdx = matchByNum;
    } else if (savedHist?.episode_id) {
      const matchById = state.activeEpisodes.findIndex(e => String(e.episode_id) === String(savedHist.episode_id));
      if (matchById !== -1) resolvedEpIdx = matchById;
    }
  }

  const resumeEpObj = state.activeEpisodes[resolvedEpIdx] || state.activeEpisodes[0];
  const resumeEpLabel = savedHist && resumeEpObj
    ? `Continue Ep ${resumeEpObj.episode_number}`
    : 'Start Ep 1';

  const banner = anime.banner_image || anime.poster;
  const score = anime.anilist_score ? (anime.anilist_score / 10).toFixed(1) : (anime.mal_score || 'N/A');
  const watchlisted = isWatchlisted(animeId);

  // Group episodes into chunks of 50 for big anime (e.g. Black Clover, One Piece)
  const totalEps = state.activeEpisodes.length;
  const chunks = [];
  const chunkSize = 50;
  for (let i = 0; i < totalEps; i += chunkSize) {
    chunks.push({ start: i, end: Math.min(i + chunkSize, totalEps) });
  }

  // Select the chunk containing the user's current/last-watched episode (e.g., 101-150 for Ep 105)
  const activeChunkIdx = chunks.length > 0
    ? Math.max(0, chunks.findIndex(c => resolvedEpIdx >= c.start && resolvedEpIdx < c.end))
    : 0;

  if (container) {
    container.innerHTML = `
      <div class="detail-banner" style="background-image: url('${banner}')">
        <div class="detail-banner-overlay"></div>
        <button class="detail-back-btn" id="detail-back-home-btn">
          <i data-lucide="arrow-left"></i> Back
        </button>
      </div>

      <div class="detail-body">
        <!-- Breadcrumbs (SEO & Navigation) -->
        <nav class="detail-breadcrumbs" aria-label="Breadcrumb">
          <a href="/" class="kuro-bc-link" id="bc-nav-home">Home</a>
          <span class="kuro-bc-sep">&rsaquo;</span>
          <a href="/browse" class="kuro-bc-link" id="bc-nav-browse">Browse</a>
          ${anime.genres ? `
            <span class="kuro-bc-sep">&rsaquo;</span>
            <a href="/browse?genre=${encodeURIComponent(primaryGenre)}" class="kuro-bc-link kuro-bc-genre" data-genre="${escapeHtmlText(primaryGenre)}">${escapeHtmlText(primaryGenre)}</a>
          ` : ''}
          <span class="kuro-bc-sep">&rsaquo;</span>
          <span class="kuro-bc-current" title="${escapeHtmlText(anime.title)}">${escapeHtmlText(anime.title)}</span>
        </nav>

        <div class="detail-header-card">
          <img class="detail-poster" src="${anime.poster}" alt="${anime.title} poster" onerror="this.onerror=null;this.src='/favicon.svg';this.classList.add('is-fallback')" />
          <div class="detail-info">
            <h1 class="detail-title">${anime.title}</h1>
            <p class="detail-seo-subheading">Watch ${escapeHtmlText(anime.title)} Online in English Sub &amp; Dub Free HD</p>
            ${anime.title_english && anime.title_english !== anime.title ? `<div class="detail-japanese-title">${anime.title_english}</div>` : ''}
            <div class="detail-meta-row">
              <span class="badge-gold"><i data-lucide="star"></i> ${score}</span>
              ${anime.sub !== false ? '<span class="pill-sub">SUB</span>' : ''}
              ${anime.dub === true ? '<span class="pill-dub">DUB</span>' : ''}
              <span class="badge-eps">${totalEps} Episodes</span>
              <span class="badge-eps">${anime.type || 'TV'}</span>
              <span class="badge-eps">${anime.season_year || anime.premiered || ''}</span>
            </div>
          </div>
        </div>

        <div class="detail-actions-bar">
          <button class="btn-primary" style="flex: 1;" id="detail-start-watch-btn">
            <i data-lucide="play"></i> ${resumeEpLabel}
          </button>
          <button class="btn-secondary ${watchlisted ? 'active' : ''}" id="detail-bookmark-btn">
            <i data-lucide="bookmark"></i>
            <span>${watchlisted ? 'In Library' : 'Add to Library'}</span>
          </button>
        </div>

        <!-- Synopsis -->
        <div class="detail-synopsis-box">
          <strong>Synopsis:</strong><br />
          ${anime.synopsis || anime.al_description || 'No synopsis available.'}
        </div>

        <!-- Connected Seasons & Sequels -->
        ${anime.seasons?.length ? `
          <div class="seasons-block">
            <div class="section-label-row">
              <span class="section-label"><i data-lucide="layers"></i> Seasons & Sequels (${anime.seasons.length})</span>
            </div>
            <div class="seasons-track">
              ${anime.seasons.map(s => `
                <div class="season-card ${s.is_current ? 'active' : ''}" data-season-id="${s.season_anime_id}">
                  <img class="season-card-img" src="${s.season_poster || anime.poster}" alt="${s.season_name}" />
                  <div class="season-card-title">${s.season_name}</div>
                </div>
              `).join('')}
            </div>
          </div>
        ` : ''}

        <!-- Episodes Grid -->
        <div class="episodes-section">
          <div class="section-label-row" style="display: flex; justify-content: space-between; align-items: center;">
            <span class="section-label"><i data-lucide="play"></i> Episodes (${totalEps})</span>
          </div>

          ${chunks.length > 1 ? `
            <div class="episodes-range-tabs" id="ep-range-tabs">
              ${chunks.map((c, idx) => `
                <button class="range-tab-btn ${idx === activeChunkIdx ? 'active' : ''}" data-start="${c.start}" data-end="${c.end}">
                  ${c.start + 1} - ${c.end}
                </button>
              `).join('')}
            </div>
          ` : ''}

          <div class="episodes-list-grid" id="episodes-grid-list">
            <!-- Injected below -->
          </div>
        </div>

        <!-- Discussion Section (Below Episodes) -->
        <section class="kuro-discussion-section" id="detail-discussion-section"></section>

        <!-- SEO Streaming Guide & FAQ Section -->
        <section class="kuro-faq-section" aria-label="Streaming Guide and FAQs">
          <div class="section-label-row">
            <span class="section-label"><i data-lucide="info"></i> Streaming Guide &amp; FAQs</span>
          </div>
          <div class="kuro-faq-grid">
            <details class="kuro-faq-item" open>
              <summary class="kuro-faq-summary">
                <span>Where can I watch ${escapeHtmlText(anime.title)} in English Sub &amp; Dub?</span>
                <i data-lucide="chevron-down" class="kuro-faq-chevron"></i>
              </summary>
              <div class="kuro-faq-body">
                You can stream all released episodes of ${escapeHtmlText(anime.title)} in 1080p/720p HD on KuroAnime with fast multi-server playback. Enjoy Japanese audio with synchronized English subtitles or full English Dub where available.
              </div>
            </details>
            <details class="kuro-faq-item">
              <summary class="kuro-faq-summary">
                <span>How many episodes are available for ${escapeHtmlText(anime.title)}?</span>
                <i data-lucide="chevron-down" class="kuro-faq-chevron"></i>
              </summary>
              <div class="kuro-faq-body">
                ${escapeHtmlText(anime.title)} features ${totalEps || 'multiple'} episodes available for instant streaming. Use the episode selector buttons above to browse and watch any episode.
              </div>
            </details>
            <details class="kuro-faq-item">
              <summary class="kuro-faq-summary">
                <span>Is ${escapeHtmlText(anime.title)} free to watch on KuroAnime?</span>
                <i data-lucide="chevron-down" class="kuro-faq-chevron"></i>
              </summary>
              <div class="kuro-faq-body">
                Yes! KuroAnime provides high-speed streaming without mandatory subscription paywalls. Bookmark this anime to your personal Watchlist to track your progress and resume anytime.
              </div>
            </details>
          </div>
        </section>
      </div>
    `;

    // Render the episode chunk containing the user's current/last-watched episode
    const initialChunk = chunks[activeChunkIdx] || { start: 0, end: totalEps };
    renderEpisodeChunk(initialChunk.start, initialChunk.end);
    mountDiscussionSection('detail-discussion-section', anime, resumeEpObj?.episode_number || 1, 'all');

    // Events
    // Breadcrumb Navigation Handlers
    document.getElementById('bc-nav-home')?.addEventListener('click', (e) => {
      e.preventDefault();
      switchTab('home');
    });
    document.getElementById('bc-nav-browse')?.addEventListener('click', (e) => {
      e.preventDefault();
      switchTab('browse');
    });
    document.querySelectorAll('.kuro-bc-genre').forEach(gLink => {
      gLink.addEventListener('click', (e) => {
        e.preventDefault();
        const gName = gLink.dataset.genre;
        if (gName) {
          state.browseGenre = gName;
          switchTab('browse');
          const chip = document.querySelector(`.chip-btn[data-genre="${gName}"]`);
          if (chip) chip.click();
        }
      });
    });

    document.getElementById('detail-back-home-btn')?.addEventListener('click', () => {
      switchTab(state.currentTab || 'home');
    });

    document.getElementById('detail-start-watch-btn')?.addEventListener('click', () => {
      if (state.activeEpisodes.length > 0) {
        const latestHist = state.history.find(h => Number(h.anime_id) === Number(animeId));
        let targetIdx = resolvedEpIdx;
        if (latestHist) {
          const foundIdx = state.activeEpisodes.findIndex(e => Number(e.episode_number) === Number(latestHist.episode_number));
          if (foundIdx !== -1) targetIdx = foundIdx;
        }
        state.playerEnteredFrom = 'detail';
        openWatchPlayer(targetIdx);
      }
    });

    const bookmarkBtn = document.getElementById('detail-bookmark-btn');
    bookmarkBtn?.addEventListener('click', () => {
      toggleWatchlist(anime);
      const nowSaved = isWatchlisted(animeId);
      bookmarkBtn.classList.toggle('active', nowSaved);
      bookmarkBtn.innerHTML = `<i data-lucide="bookmark"></i><span>${nowSaved ? 'In Library' : 'Add to Library'}</span>`;
      refreshIcons();
    });

    // Season switcher cards
    container.querySelectorAll('.season-card').forEach(sc => {
      sc.addEventListener('click', () => {
        const targetId = Number(sc.dataset.seasonId);
        if (targetId && targetId !== animeId) {
          openAnimeDetail(targetId);
        }
      });
    });

    // Chunk tab switcher
    container.querySelectorAll('.range-tab-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        container.querySelectorAll('.range-tab-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        renderEpisodeChunk(Number(btn.dataset.start), Number(btn.dataset.end));
      });
    });
  }

  if (autoPlayFirst && state.activeEpisodes.length > 0) {
    openWatchPlayer(resolvedEpIdx);
    return;
  }

  refreshIcons();
}

function renderEpisodeChunk(start, end) {
  const grid = document.getElementById('episodes-grid-list');
  if (!grid) return;
  const slice = state.activeEpisodes.slice(start, end);
  const savedHist = state.history.find(h => Number(h.anime_id) === Number(state.activeAnimeId));
  const watchedEpNum = savedHist ? Number(savedHist.episode_number) : null;

  grid.innerHTML = slice.map((ep, idx) => {
    const globalIdx = start + idx;
    const isCurrentWatched = watchedEpNum !== null && Number(ep.episode_number) === watchedEpNum;
    return `
      <button class="episode-btn ${isCurrentWatched ? 'active' : ''}" data-idx="${globalIdx}">
        <span class="ep-num">${ep.episode_number}</span>
        <div class="ep-flags">
          ${ep.sub ? '<span class="ep-flag-sub">S</span>' : ''}
          ${ep.dub ? '<span class="ep-flag-dub">D</span>' : ''}
        </div>
      </button>
    `;
  }).join('');

  grid.querySelectorAll('.episode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      state.playerEnteredFrom = 'detail';
      openWatchPlayer(Number(btn.dataset.idx));
    });
  });
}

// ─── Watch Player View ──────────────────────────────────────────────
async function openWatchPlayer(episodeIndex) {
  state.activeEpisodeIndex = episodeIndex;
  const anime = state.activeAnimeData;
  const ep = state.activeEpisodes[episodeIndex];
  if (!anime || !ep) return;

  showView('view-watch');
  saveHistory(anime, ep);

  const container = document.getElementById('player-page-container');
  if (!container) return;

  const frameContainer = document.getElementById('video-frame-container');

  // If the player is already mounted and active for this anime:
  // Seamlessly transition in-place without wiping container.innerHTML
  // This preserves Fullscreen mode & Landscape orientation on mobile!
  if (frameContainer && activePlayerInstance && !activePlayerInstance.isDestroyed) {
    // 1. Update Episode Title in header
    const epTitleEl = container.querySelector('.player-ep-title');
    if (epTitleEl) {
      epTitleEl.textContent = `Episode ${ep.episode_number}${ep.title ? `: ${ep.title}` : ''}`;
    }

    // 2. Update Prev / Next buttons state outside player
    const prevBtn = document.getElementById('player-prev-ep-btn');
    if (prevBtn) {
      prevBtn.disabled = episodeIndex <= 0;
      prevBtn.style.opacity = episodeIndex <= 0 ? '0.4' : '1';
    }
    const nextBtn = document.getElementById('player-next-ep-btn');
    if (nextBtn) {
      nextBtn.disabled = episodeIndex >= state.activeEpisodes.length - 1;
      nextBtn.style.opacity = episodeIndex >= state.activeEpisodes.length - 1 ? '0.4' : '1';
    }

    // 3. Update Drawer active episode button highlight
    container.querySelectorAll('#player-episodes-drawer .episode-btn').forEach((btn, idx) => {
      btn.classList.toggle('active', idx === episodeIndex);
    });

    // 3b. Refresh Episode Discussion section for the newly selected episode
    mountDiscussionSection('player-discussion-section', anime, ep.episode_number || (episodeIndex + 1), 'episode');

    // 4. Show smooth loading overlay inside player while fetching new streams
    activePlayerInstance.setLoader(true, `Loading Episode ${ep.episode_number}...`);

    // 5. Fetch streams for the new episode
    const streamsData = await fetchApi(`/episode/${ep.episode_id}/streams?anime_id=${anime.anime_id}`);
    state.currentStreams = streamsData || { sub: [], dub: [] };
    state.activeServerIndex = 0;

    renderServerChips();

    // 6. Resolve selected server
    let list = state.currentStreams[state.activeAudioType] || [];
    if (!list.length) {
      const fallbackType = state.activeAudioType === 'sub' ? 'dub' : 'sub';
      if ((state.currentStreams[fallbackType] || []).length > 0) {
        state.activeAudioType = fallbackType;
        list = state.currentStreams[fallbackType];
      }
    }
    syncAudioLanguageUI(state.activeAudioType);
    renderServerChips();

    const server = list[state.activeServerIndex];
    if (server && server.embed_url) {
      await activePlayerInstance.updateEpisode({
        embedUrl: server.embed_url,
        episodeNumber: ep.episode_number || (episodeIndex + 1),
        episodeTitle: ep.title || '',
        episodeId: ep.episode_id || '',
        hasNextEp: episodeIndex < state.activeEpisodes.length - 1,
        hasPrevEp: episodeIndex > 0,
        audioType: state.activeAudioType,
        hasSub: (state.currentStreams.sub || []).length > 0,
        hasDub: (state.currentStreams.dub || []).length > 0,
      });
    } else {
      loadStreamIntoPlayer();
    }

    refreshIcons();
    return;
  }

  container.innerHTML = `
    <div class="player-header">
      <button class="detail-back-btn" id="player-back-detail-btn" style="position:static">
        <i data-lucide="arrow-left"></i>
      </button>
      <div class="player-title-box">
        <div class="player-anime-title">${anime.title}</div>
        <div class="player-ep-title">Episode ${ep.episode_number}${ep.title ? `: ${ep.title}` : ''}</div>
      </div>
      <button class="header-icon-btn ${isWatchlisted(anime.anime_id) ? 'active' : ''}" id="player-bookmark-btn" aria-label="${isWatchlisted(anime.anime_id) ? 'In Library' : 'Add to Library'}" title="${isWatchlisted(anime.anime_id) ? 'In Library' : 'Add to Library'}">
        <i data-lucide="bookmark"></i>
      </button>
    </div>

    <!-- Video Container -->
    <div class="video-frame-wrapper" id="video-frame-container">
      ${renderPlayerFrameSkeleton()}
    </div>

    <!-- Controls Panel -->
    <div class="player-controls-panel">
      <div class="player-settings-grid">
        <!-- Audio Selection -->
        <div class="player-control-group">
          <div class="server-section-label">Audio Language</div>
          <div class="audio-toggle-bar">
            <button class="audio-toggle-btn ${state.activeAudioType === 'sub' ? 'active' : ''}" id="btn-audio-sub">
              <span class="ctrl-main-label">SUB</span>
              <span class="ctrl-sub-label">Japanese</span>
            </button>
            <button class="audio-toggle-btn ${state.activeAudioType === 'dub' ? 'active' : ''}" id="btn-audio-dub">
              <span class="ctrl-main-label">DUB</span>
              <span class="ctrl-sub-label">English</span>
            </button>
          </div>
        </div>

        <!-- Server Chips -->
        <div class="player-control-group">
          <div class="server-section-label">Stream Server</div>
          <div class="server-chips-row" id="player-server-chips">
            <!-- Injected below -->
          </div>
        </div>
      </div>

      <!-- Previous / Next / Download Controls -->
      <div class="ep-nav-buttons-row">
        <button class="ep-nav-btn" id="player-prev-ep-btn" ${episodeIndex <= 0 ? 'disabled style="opacity:0.4"' : ''}>
          <span>Prev</span>
        </button>
        <button class="ep-nav-btn ep-nav-btn-download" id="player-download-ep-btn">
          <span>Download</span>
        </button>
        <button class="ep-nav-btn" id="player-reload-btn">
          <span>Reload</span>
        </button>
        <button class="ep-nav-btn" id="player-next-ep-btn" ${episodeIndex >= state.activeEpisodes.length - 1 ? 'disabled style="opacity:0.4"' : ''}>
          <span>Next</span>
        </button>
      </div>
    </div>

    <!-- Related Seasons (if multi-season anime) -->
    ${anime.seasons?.length > 1 ? `
      <div style="padding: 12px 16px 0;">
        <div class="server-section-label">Related Seasons & Sequels</div>
        <div class="seasons-track">
          ${anime.seasons.map(s => `
            <div class="season-card ${s.is_current ? 'active' : ''}" data-player-season-id="${s.season_anime_id}">
              <img class="season-card-img" src="${s.season_poster || anime.poster}" alt="${s.season_name}" />
              <div class="season-card-title">${s.season_name}</div>
            </div>
          `).join('')}
        </div>
      </div>
    ` : ''}

    <!-- Episode Drawer -->
    <div style="padding: 16px;">
      <div class="section-label-row">
        <span class="section-label">All Episodes (${state.activeEpisodes.length})</span>
      </div>
      <div class="episodes-list-grid" id="player-episodes-drawer">
        ${state.activeEpisodes.map((e, idx) => `
          <button class="episode-btn ${idx === episodeIndex ? 'active' : ''}" data-idx="${idx}">
            <span class="ep-num">${e.episode_number}</span>
          </button>
        `).join('')}
      </div>
    </div>

    <!-- Episode Discussion Section (Below Episode Count) -->
    <section class="kuro-discussion-section" id="player-discussion-section"></section>
  `;

  mountDiscussionSection('player-discussion-section', anime, ep.episode_number || (episodeIndex + 1), 'episode');

  // Attach Player Events
  document.getElementById('player-back-detail-btn')?.addEventListener('click', () => {
    const detailReady = document.querySelector('#detail-container .detail-body');
    if (state.playerEnteredFrom === 'home' || !detailReady) {
      switchTab('home');
    } else {
      showView('view-detail');
    }
  });

  const playerBookmarkBtn = document.getElementById('player-bookmark-btn');
  playerBookmarkBtn?.addEventListener('click', () => {
    toggleWatchlist(anime);
    const nowSaved = isWatchlisted(anime.anime_id);
    playerBookmarkBtn.classList.toggle('active', nowSaved);
    playerBookmarkBtn.setAttribute('aria-label', nowSaved ? 'In Library' : 'Add to Library');
    playerBookmarkBtn.setAttribute('title', nowSaved ? 'In Library' : 'Add to Library');
    playerBookmarkBtn.innerHTML = `<i data-lucide="bookmark"></i>`;
    refreshIcons();
  });

  document.getElementById('player-prev-ep-btn')?.addEventListener('click', () => {
    if (state.activeEpisodeIndex > 0) openWatchPlayer(state.activeEpisodeIndex - 1);
  });

  document.getElementById('player-next-ep-btn')?.addEventListener('click', () => {
    if (state.activeEpisodeIndex < state.activeEpisodes.length - 1) openWatchPlayer(state.activeEpisodeIndex + 1);
  });

  document.getElementById('player-reload-btn')?.addEventListener('click', () => {
    loadStreamIntoPlayer();
  });

  document.getElementById('player-download-ep-btn')?.addEventListener('click', () => {
    const dlModal = document.getElementById('nf-download-modal');
    if (dlModal) {
      dlModal.classList.add('open');
    } else {
      showToast('Stream is still loading...');
    }
  });

  document.getElementById('btn-audio-sub')?.addEventListener('click', () => {
    if ((state.currentStreams?.sub || []).length === 0) {
      showToast('No SUB stream available for this episode');
      return;
    }
    state.activeAudioType = 'sub';
    state.activeServerIndex = 0;
    syncAudioLanguageUI('sub');
    renderServerChips();
    loadStreamIntoPlayer();
  });

  document.getElementById('btn-audio-dub')?.addEventListener('click', () => {
    if ((state.currentStreams?.dub || []).length === 0) {
      showToast('No DUB stream available for this episode');
      return;
    }
    state.activeAudioType = 'dub';
    state.activeServerIndex = 0;
    syncAudioLanguageUI('dub');
    renderServerChips();
    loadStreamIntoPlayer();
  });

  container.querySelectorAll('#player-episodes-drawer .episode-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      openWatchPlayer(Number(btn.dataset.idx));
    });
  });

  container.querySelectorAll('[data-player-season-id]').forEach(sc => {
    sc.addEventListener('click', () => {
      const targetId = Number(sc.dataset.playerSeasonId);
      if (targetId && targetId !== anime.anime_id) {
        openAnimeDetail(targetId, true);
      }
    });
  });

  // Fetch streams for this episode
  const streamsData = await fetchApi(`/episode/${ep.episode_id}/streams?anime_id=${anime.anime_id}`);
  state.currentStreams = streamsData || { sub: [], dub: [] };
  state.activeServerIndex = 0;

  syncAudioLanguageUI(state.activeAudioType);
  renderServerChips();
  loadStreamIntoPlayer();
  refreshIcons();
}

function syncAudioLanguageUI(type) {
  const current = type || state.activeAudioType || 'sub';
  state.activeAudioType = current;

  const hasSub = (state.currentStreams?.sub || []).length > 0;
  const hasDub = (state.currentStreams?.dub || []).length > 0;

  // 1. Sync Frontend Page Buttons under "Audio Language"
  const subBtn = document.getElementById('btn-audio-sub');
  const dubBtn = document.getElementById('btn-audio-dub');
  if (subBtn) {
    subBtn.classList.toggle('active', current === 'sub');
    subBtn.disabled = !hasSub;
    subBtn.style.opacity = hasSub ? '1' : '0.45';
    subBtn.style.cursor = hasSub ? 'pointer' : 'not-allowed';
  }
  if (dubBtn) {
    dubBtn.classList.toggle('active', current === 'dub');
    dubBtn.disabled = !hasDub;
    dubBtn.style.opacity = hasDub ? '1' : '0.45';
    dubBtn.style.cursor = hasDub ? 'pointer' : 'not-allowed';
  }

  // 2. Sync Video Player Internal Pills
  if (activePlayerInstance && !activePlayerInstance.isDestroyed) {
    activePlayerInstance.audioType = current;
    activePlayerInstance.hasSub = hasSub;
    activePlayerInstance.hasDub = hasDub;
    if (typeof activePlayerInstance.updateAudioUI === 'function') {
      activePlayerInstance.updateAudioUI();
    }
  }
}

function renderServerChips() {
  const chipsContainer = document.getElementById('player-server-chips');
  if (!chipsContainer) return;
  const list = state.currentStreams[state.activeAudioType] || [];

  if (!list.length) {
    chipsContainer.innerHTML = `<span style="color:var(--text-muted);font-size:0.8rem">No ${state.activeAudioType.toUpperCase()} streams available. Try switching audio language.</span>`;
    return;
  }

  if (state.preferredServerName) {
    const prefIdx = list.findIndex(s => s.server_name === state.preferredServerName);
    if (prefIdx !== -1) {
      state.activeServerIndex = prefIdx;
    }
  }

  if (state.activeServerIndex >= list.length) {
    state.activeServerIndex = 0;
  }

  chipsContainer.innerHTML = list.map((s, idx) => `
    <button class="server-chip ${idx === state.activeServerIndex ? 'active' : ''}" data-sidx="${idx}">
      <span class="server-chip-name">${s.server_name || 'Server ' + (idx + 1)}</span>
    </button>
  `).join('');

  chipsContainer.querySelectorAll('.server-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      const idx = Number(btn.dataset.sidx);
      state.activeServerIndex = idx;
      if (list[idx]?.server_name) {
        state.preferredServerName = list[idx].server_name;
      }
      chipsContainer.querySelectorAll('.server-chip').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      loadStreamIntoPlayer();
    });
  });

  refreshIcons();
}

function loadStreamIntoPlayer() {
  const frameContainer = document.getElementById('video-frame-container');
  if (!frameContainer) return;

  let list = state.currentStreams[state.activeAudioType] || [];
  if (!list.length) {
    const fallbackType = state.activeAudioType === 'sub' ? 'dub' : 'sub';
    if ((state.currentStreams[fallbackType] || []).length > 0) {
      state.activeAudioType = fallbackType;
      list = state.currentStreams[fallbackType];
    }
  }
  syncAudioLanguageUI(state.activeAudioType);

  const server = list[state.activeServerIndex];

  if (!server || !server.embed_url) {
    stopVideoPlayback();
    frameContainer.innerHTML = `
      <div class="empty-state" style="padding: 60px 16px;">
        <i data-lucide="film" class="empty-state-icon"></i><br/>
        No active stream URL found on this server.<br/>
        <span style="font-size:0.8rem;color:var(--text-muted)">Please switch server or audio language.</span>
      </div>
    `;
    refreshIcons();
    return;
  }

  const anime = state.activeAnimeData || {};
  const ep = state.activeEpisodes[state.activeEpisodeIndex] || {};

  // If player already mounted, update stream in-place (keeps fullscreen & landscape!)
  if (activePlayerInstance && !activePlayerInstance.isDestroyed && activePlayerInstance.root) {
    activePlayerInstance.updateEpisode({
      embedUrl: server.embed_url,
      episodeNumber: ep.episode_number || (state.activeEpisodeIndex + 1),
      episodeTitle: ep.title || '',
      episodeId: ep.episode_id || '',
      audioType: state.activeAudioType,
      hasSub: (state.currentStreams.sub || []).length > 0,
      hasDub: (state.currentStreams.dub || []).length > 0,
      hasNextEp: state.activeEpisodeIndex < state.activeEpisodes.length - 1,
      hasPrevEp: state.activeEpisodeIndex > 0,
    });
    refreshIcons();
    return;
  }

  if (activePlayerInstance) {
    try { activePlayerInstance.destroy(); } catch (e) {}
    activePlayerInstance = null;
  }

  activePlayerInstance = new NetflixPlayer({
    container: frameContainer,
    apiBase: API_BASE,
    embedUrl: server.embed_url,
    animeTitle: anime.title || 'Anime',
    animeId: anime.anime_id,
    episodeNumber: ep.episode_number || (state.activeEpisodeIndex + 1),
    episodeTitle: ep.title || '',
    episodeId: ep.episode_id || '',
    audioType: state.activeAudioType,
    hasSub: (state.currentStreams.sub || []).length > 0,
    hasDub: (state.currentStreams.dub || []).length > 0,
    hasNextEp: state.activeEpisodeIndex < state.activeEpisodes.length - 1,
    hasPrevEp: state.activeEpisodeIndex > 0,
    poster: anime.banner_image || anime.poster_large || anime.poster || '',
    onProgress: (p) => {
      const curAnime = state.activeAnimeData || anime;
      const curEp = state.activeEpisodes[state.activeEpisodeIndex] || {
        episode_number: p.episodeNumber || ep.episode_number || (state.activeEpisodeIndex + 1),
        episode_id: p.episodeId || ep.episode_id || '',
        title: p.episodeTitle || ep.title || ''
      };
      saveHistory(curAnime, curEp, p.currentTime, p.duration, p.completed);
    },
    onNextEpisode: () => {
      if (state.activeEpisodeIndex < state.activeEpisodes.length - 1) {
        openWatchPlayer(state.activeEpisodeIndex + 1);
      }
    },
    onPrevEpisode: () => {
      if (state.activeEpisodeIndex > 0) {
        openWatchPlayer(state.activeEpisodeIndex - 1);
      }
    },
    onSwitchAudio: (targetAudio) => {
      if ((state.currentStreams?.[targetAudio] || []).length > 0) {
        state.activeAudioType = targetAudio;
        state.activeServerIndex = 0;
        syncAudioLanguageUI(targetAudio);
        renderServerChips();
        loadStreamIntoPlayer();
        showToast(`Switched to ${targetAudio.toUpperCase()} audio`);
      } else {
        syncAudioLanguageUI(state.activeAudioType);
        showToast(`No ${targetAudio.toUpperCase()} stream available for this episode`);
      }
    },
    onBack: () => {
      const detailReady = document.querySelector('#detail-container .detail-body');
      if (state.playerEnteredFrom === 'home' || !detailReady) {
        switchTab('home');
      } else {
        showView('view-detail');
      }
    },
    onToast: (msg) => showToast(msg),
  });

  refreshIcons();
}

// ─── Episode & Anime Discussion System (Google Auth Restricted) ─────
const activeDiscussionInstances = {};

function getGoogleAuthenticatedUser() {
  try {
    const u = JSON.parse(localStorage.getItem('kuro_user_profile') || 'null');
    if (!u || !u.email || !String(u.email).includes('@')) return null;
    if (u.provider && String(u.provider).toLowerCase() !== 'google') return null;
    return {
      id: String(u.id || u.email),
      name: String(u.name || u.email.split('@')[0] || 'Kuro Member'),
      email: String(u.email).toLowerCase(),
      avatar: u.avatar ? String(u.avatar) : '',
      provider: 'google',
    };
  } catch (e) {
    return null;
  }
}

function escapeHtmlText(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatCommentRelativeTime(ts) {
  const now = Date.now();
  const timeMs = Number(ts) > 1e12 ? Number(ts) : Number(ts) * 1000;
  const diffSec = Math.max(0, Math.floor((now - timeMs) / 1000));
  if (diffSec < 45) return 'Just now';
  const diffMin = Math.floor(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.floor(diffHr / 24);
  if (diffDay < 30) return `${diffDay}d ago`;
  const d = new Date(timeMs);
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function getLocalCommentsCache(animeId) {
  try {
    return JSON.parse(localStorage.getItem(`kuro_comments_${animeId}`) || '[]');
  } catch (e) {
    return [];
  }
}

function saveLocalCommentsCache(animeId, comments) {
  try {
    localStorage.setItem(`kuro_comments_${animeId}`, JSON.stringify(comments.slice(0, 100)));
  } catch (e) {}
}

function getLikedCommentSet() {
  try {
    return new Set(JSON.parse(localStorage.getItem('kuro_liked_comments') || '[]'));
  } catch (e) {
    return new Set();
  }
}

function saveLikedCommentSet(setObj) {
  try {
    localStorage.setItem('kuro_liked_comments', JSON.stringify(Array.from(setObj)));
  } catch (e) {}
}

function formatVoiceDuration(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${rem < 10 ? '0' : ''}${rem}`;
}

function readBlobAsDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = reject;
    reader.readAsDataURL(blob);
  });
}

async function compressImageFileToBlob(file, maxDim = 1280, quality = 0.82) {
  return new Promise((resolve) => {
    const img = new Image();
    const objUrl = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(objUrl);
      let { width, height } = img;
      if (width > maxDim || height > maxDim) {
        if (width > height) {
          height = Math.round((height * maxDim) / width);
          width = maxDim;
        } else {
          width = Math.round((width * maxDim) / height);
          height = maxDim;
        }
      }
      const canvas = document.createElement('canvas');
      canvas.width = width;
      canvas.height = height;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(img, 0, 0, width, height);
      canvas.toBlob(
        (blob) => resolve(blob || file),
        'image/webp',
        quality
      );
    };
    img.onerror = () => {
      URL.revokeObjectURL(objUrl);
      resolve(file);
    };
    img.src = objUrl;
  });
}

async function mountDiscussionSection(containerId, anime, episodeNumber = 1, defaultScope = 'episode') {
  const root = document.getElementById(containerId);
  if (!root || !anime || !anime.anime_id) return;

  const animeId = Number(anime.anime_id);
  const epNum = Math.max(1, Number(episodeNumber) || 1);
  const prevState = activeDiscussionInstances[containerId];
  if (prevState?.recorderTimer) clearInterval(prevState.recorderTimer);
  const scope = prevState && prevState.animeId === animeId ? prevState.scope : defaultScope;

  activeDiscussionInstances[containerId] = {
    containerId,
    anime,
    animeId,
    episodeNumber: epNum,
    scope,
    comments: prevState && prevState.animeId === animeId ? prevState.comments : [],
    pendingMedia: null, // { blob, dataUrl, type: 'image'|'video'|'voice', ext, mime, duration, name }
    isSpoiler: false,
    mediaRecorder: null,
    recorderStream: null,
    recorderChunks: [],
    recorderStartTime: 0,
    recorderTimer: null,
  };

  renderDiscussionShell(containerId);
  await loadDiscussionComments(containerId);
}

function refreshActiveDiscussions() {
  Object.keys(activeDiscussionInstances).forEach(cid => {
    if (document.getElementById(cid)) {
      renderDiscussionShell(cid);
      renderDiscussionFeed(cid);
    }
  });
}

function stopVoiceRecording(containerId, cancel = true) {
  const inst = activeDiscussionInstances[containerId];
  if (!inst) return;
  if (inst.recorderTimer) {
    clearInterval(inst.recorderTimer);
    inst.recorderTimer = null;
  }
  if (inst.mediaRecorder && inst.mediaRecorder.state !== 'inactive') {
    inst.mediaRecorder._cancelled = cancel;
    try { inst.mediaRecorder.stop(); } catch (e) {}
  }
  if (inst.recorderStream) {
    try { inst.recorderStream.getTracks().forEach(t => t.stop()); } catch (e) {}
    inst.recorderStream = null;
  }
}

function renderDiscussionShell(containerId) {
  const inst = activeDiscussionInstances[containerId];
  const root = document.getElementById(containerId);
  if (!inst || !root) return;

  const user = getGoogleAuthenticatedUser();
  const epNum = inst.episodeNumber;
  const scope = inst.scope;
  const count = (inst.comments || []).length;

  const googleSvg = `
    <svg class="kuro-disc-google-svg" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
      <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.06H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.94l2.85-2.22.81-.63z"/>
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.06l3.66 2.84c.87-2.6 3.3-4.52 6.16-4.52z"/>
    </svg>
  `;

  // Unslop Cinema-Grade Composer Bar (Unified '+' Attachment Popover for Photo, Video, Voice & Spoiler)
  const bottomBarHtml = user
    ? `
      <div class="kuro-wa-composer-wrap" id="${containerId}-composer-wrap">
        <!-- Attachment Preview Strip (Image / Video / Recorded Voice) -->
        <div class="kuro-wa-preview-bar" id="${containerId}-preview-bar" style="display:none"></div>

        <!-- Active Voice Recording Bar (Hidden until recording starts) -->
        <div class="kuro-wa-recording-bar" id="${containerId}-recording-bar" style="display:none">
          <div class="kuro-wa-rec-status">
            <span class="kuro-wa-rec-dot"></span>
            <span class="kuro-wa-rec-timer" id="${containerId}-rec-timer">0:00</span>
            <span class="kuro-wa-rec-label">Recording voice note...</span>
          </div>
          <div class="kuro-wa-rec-actions">
            <button type="button" class="kuro-wa-rec-cancel" id="${containerId}-rec-cancel">Cancel</button>
            <button type="button" class="kuro-wa-rec-send" id="${containerId}-rec-send">Send</button>
          </div>
        </div>

        <!-- Unified Single-Row Composer Bar -->
        <form class="kuro-wa-bar" id="${containerId}-form" autocomplete="off">
          <input
            type="file"
            id="${containerId}-file-input"
            accept="image/*,video/*"
            style="display:none"
          />

          <!-- Unified '+' Media & Options Hub (Photo, Video, Voice, Spoiler in one icon) -->
          <div class="kuro-disc-hub-wrap" id="${containerId}-hub-wrap">
            <button
              type="button"
              class="kuro-disc-plus-btn ${inst.isSpoiler ? 'has-spoiler' : ''}"
              id="${containerId}-hub-btn"
              aria-label="Add photo, video, voice note, or spoiler tag"
              aria-expanded="false"
              title="Add media or voice note"
            >
              <svg class="kuro-disc-plus-icon" viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <line x1="12" y1="5" x2="12" y2="19"></line>
                <line x1="5" y1="12" x2="19" y2="12"></line>
              </svg>
              <span class="kuro-disc-spoiler-dot" id="${containerId}-spoiler-dot" style="display:${inst.isSpoiler ? 'block' : 'none'}"></span>
            </button>

            <!-- Floating GPU-Animated Attachment Popover -->
            <div class="kuro-disc-attach-menu" id="${containerId}-attach-menu" role="menu" aria-hidden="true">
              <button type="button" class="kuro-disc-menu-item" id="${containerId}-photo-btn" role="menuitem">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="8.5" cy="8.5" r="1.5"/><polyline points="21 15 16 10 5 21"/></svg>
                <span>Photo</span>
              </button>
              <button type="button" class="kuro-disc-menu-item" id="${containerId}-video-btn" role="menuitem">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7"/><rect x="1" y="5" width="15" height="14" rx="2" ry="2"/></svg>
                <span>Video clip</span>
              </button>
              <button type="button" class="kuro-disc-menu-item" id="${containerId}-voice-btn" role="menuitem">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="23"/><line x1="8" y1="23" x2="16" y2="23"/></svg>
                <span>Voice note</span>
              </button>
              <div class="kuro-disc-menu-divider"></div>
              <button type="button" class="kuro-disc-menu-item kuro-wa-spoiler-pill ${inst.isSpoiler ? 'active' : ''}" id="${containerId}-spoiler-btn" role="menuitem">
                <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>
                <span>Spoiler blur</span>
                <span class="kuro-disc-menu-state" id="${containerId}-spoiler-state">${inst.isSpoiler ? 'On' : 'Off'}</span>
              </button>
            </div>
          </div>

          <div class="kuro-disc-input-wrap">
            <span class="kuro-disc-inline-spoiler" id="${containerId}-inline-spoiler" style="display:${inst.isSpoiler ? 'inline-flex' : 'none'}">Spoiler</span>
            <input
              type="text"
              class="kuro-wa-input"
              id="${containerId}-input"
              maxlength="1200"
              placeholder="Message Episode ${epNum}..."
            />
          </div>

          <button
            type="submit"
            class="kuro-wa-send-btn"
            id="${containerId}-submit-btn"
            aria-label="Send message"
            title="Send message"
          >
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <line x1="22" y1="2" x2="11" y2="13"></line>
              <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
            </svg>
          </button>
        </form>
      </div>
    `
    : `
      <div class="kuro-wa-auth-bar">
        <span class="kuro-wa-auth-text">Sign in to share messages, media, or voice notes</span>
        <button type="button" class="kuro-disc-google-btn" id="${containerId}-google-signin-btn">
          ${googleSvg}
          <span>Sign in with Google</span>
        </button>
      </div>
    `;

  root.innerHTML = `
    <div class="kuro-disc-card kuro-wa-container">
      <!-- Top WhatsApp-Style Chat Header -->
      <div class="kuro-disc-header">
        <div class="kuro-disc-title-group">
          <span class="kuro-disc-accent-bar"></span>
          <div class="kuro-wa-header-titles">
            <h3 class="kuro-disc-heading">Episode Discussion</h3>
            <span class="kuro-wa-header-sub" id="${containerId}-count-pill">${count} ${count === 1 ? 'Message' : 'Messages'} &middot; Ep ${epNum}</span>
          </div>
        </div>

        <div class="kuro-disc-scope-tabs" role="tablist" aria-label="Discussion filter">
          <button
            type="button"
            class="kuro-disc-scope-btn ${scope === 'episode' ? 'active' : ''}"
            data-scope="episode"
          >
            Ep ${epNum}
          </button>
          <button
            type="button"
            class="kuro-disc-scope-btn ${scope === 'all' ? 'active' : ''}"
            data-scope="all"
          >
            All Eps
          </button>
        </div>
      </div>

      <!-- Scrollable WhatsApp-Style Chat Messages Window -->
      <div class="kuro-wa-chat-viewport" id="${containerId}-feed">
        <div class="kuro-disc-loading">Loading chat messages...</div>
      </div>

      <!-- Bottom Bar (WhatsApp Input or Google Sign-In Prompt) -->
      ${bottomBarHtml}
    </div>
  `;

  // Scope filter buttons
  root.querySelectorAll('.kuro-disc-scope-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const nextScope = btn.dataset.scope || 'episode';
      if (inst.scope !== nextScope) {
        inst.scope = nextScope;
        renderDiscussionShell(containerId);
        loadDiscussionComments(containerId);
      }
    });
  });

  // Google Sign-In button for unauthenticated users
  document.getElementById(`${containerId}-google-signin-btn`)?.addEventListener('click', () => {
    if (typeof window.kuroSignInWithGoogle === 'function') {
      window.kuroSignInWithGoogle();
    } else {
      document.getElementById('btn-user-google-login')?.click();
    }
  });

  if (!user) return;

  // Authenticated Composer Controls (Unified '+' Hub Menu)
  const formEl = document.getElementById(`${containerId}-form`);
  const inputEl = document.getElementById(`${containerId}-input`);
  const fileInputEl = document.getElementById(`${containerId}-file-input`);
  const hubWrapEl = document.getElementById(`${containerId}-hub-wrap`);
  const hubBtnEl = document.getElementById(`${containerId}-hub-btn`);
  const attachMenuEl = document.getElementById(`${containerId}-attach-menu`);
  const photoBtnEl = document.getElementById(`${containerId}-photo-btn`);
  const videoBtnEl = document.getElementById(`${containerId}-video-btn`);
  const spoilerBtnEl = document.getElementById(`${containerId}-spoiler-btn`);
  const spoilerDotEl = document.getElementById(`${containerId}-spoiler-dot`);
  const spoilerStateEl = document.getElementById(`${containerId}-spoiler-state`);
  const inlineSpoilerEl = document.getElementById(`${containerId}-inline-spoiler`);
  const voiceBtnEl = document.getElementById(`${containerId}-voice-btn`);
  const submitBtnEl = document.getElementById(`${containerId}-submit-btn`);
  const previewBarEl = document.getElementById(`${containerId}-preview-bar`);
  const recordingBarEl = document.getElementById(`${containerId}-recording-bar`);
  const recTimerEl = document.getElementById(`${containerId}-rec-timer`);
  const recCancelEl = document.getElementById(`${containerId}-rec-cancel`);
  const recSendEl = document.getElementById(`${containerId}-rec-send`);

  function toggleAttachMenu(forceState) {
    if (!attachMenuEl || !hubBtnEl) return;
    const nextOpen = typeof forceState === 'boolean' ? forceState : !attachMenuEl.classList.contains('open');
    attachMenuEl.classList.toggle('open', nextOpen);
    hubBtnEl.classList.toggle('open', nextOpen);
    hubBtnEl.setAttribute('aria-expanded', String(nextOpen));
    attachMenuEl.setAttribute('aria-hidden', String(!nextOpen));
  }

  hubBtnEl?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleAttachMenu();
  });

  document.addEventListener('click', (e) => {
    if (hubWrapEl && !hubWrapEl.contains(e.target) && attachMenuEl?.classList.contains('open')) {
      toggleAttachMenu(false);
    }
  });

  function updateMediaPreviewUI() {
    if (!previewBarEl) return;
    const pm = inst.pendingMedia;
    if (!pm) {
      previewBarEl.style.display = 'none';
      previewBarEl.innerHTML = '';
      return;
    }
    previewBarEl.style.display = 'flex';
    let thumbHtml = '';
    if (pm.type === 'image') {
      thumbHtml = `<img src="${pm.dataUrl}" alt="Preview" class="kuro-wa-preview-thumb" />`;
    } else if (pm.type === 'video') {
      thumbHtml = `<video src="${pm.dataUrl}" class="kuro-wa-preview-thumb" muted playsinline></video>`;
    } else if (pm.type === 'voice') {
      thumbHtml = `<span class="kuro-wa-preview-voice-badge">VOICE ${formatVoiceDuration(pm.duration)}</span>`;
    }
    previewBarEl.innerHTML = `
      <div class="kuro-wa-preview-info">
        ${thumbHtml}
        <span class="kuro-wa-preview-name">${escapeHtmlText(pm.name || pm.type.toUpperCase())}</span>
      </div>
      <button type="button" class="kuro-wa-preview-remove" id="${containerId}-preview-remove">Remove</button>
    `;
    document.getElementById(`${containerId}-preview-remove`)?.addEventListener('click', () => {
      inst.pendingMedia = null;
      if (fileInputEl) fileInputEl.value = '';
      updateMediaPreviewUI();
    });
  }

  function syncSpoilerUI() {
    spoilerBtnEl?.classList.toggle('active', inst.isSpoiler);
    hubBtnEl?.classList.toggle('has-spoiler', inst.isSpoiler);
    if (spoilerDotEl) spoilerDotEl.style.display = inst.isSpoiler ? 'block' : 'none';
    if (spoilerStateEl) spoilerStateEl.textContent = inst.isSpoiler ? 'On' : 'Off';
    if (inlineSpoilerEl) inlineSpoilerEl.style.display = inst.isSpoiler ? 'inline-flex' : 'none';
  }

  spoilerBtnEl?.addEventListener('click', (e) => {
    e.stopPropagation();
    inst.isSpoiler = !inst.isSpoiler;
    syncSpoilerUI();
    toggleAttachMenu(false);
  });

  inlineSpoilerEl?.addEventListener('click', () => {
    inst.isSpoiler = false;
    syncSpoilerUI();
  });

  photoBtnEl?.addEventListener('click', () => {
    toggleAttachMenu(false);
    if (fileInputEl) {
      fileInputEl.accept = 'image/*';
      fileInputEl.click();
    }
  });

  videoBtnEl?.addEventListener('click', () => {
    toggleAttachMenu(false);
    if (fileInputEl) {
      fileInputEl.accept = 'video/*';
      fileInputEl.click();
    }
  });

  fileInputEl?.addEventListener('change', async () => {
    const file = fileInputEl.files?.[0];
    if (!file) return;
    const isImage = file.type.startsWith('image/');
    const isVideo = file.type.startsWith('video/');
    if (!isImage && !isVideo) {
      showToast('Please select an image or video file');
      return;
    }
    if (file.size > 25 * 1024 * 1024) {
      showToast('File must be under 25 MB');
      return;
    }

    let finalBlob = file;
    let ext = (file.name.split('.').pop() || (isImage ? 'webp' : 'mp4')).toLowerCase();
    let mime = file.type;
    if (isImage && !file.type.includes('gif')) {
      finalBlob = await compressImageFileToBlob(file, 1200, 0.82);
      ext = 'webp';
      mime = 'image/webp';
    }
    const dataUrl = await readBlobAsDataUrl(finalBlob);
    inst.pendingMedia = {
      blob: finalBlob,
      dataUrl,
      type: isImage ? 'image' : 'video',
      ext,
      mime,
      duration: 0,
      name: file.name,
    };
    updateMediaPreviewUI();
  });

  // Live Voice Note Recording (MediaRecorder API)
  voiceBtnEl?.addEventListener('click', async () => {
    toggleAttachMenu(false);
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      showToast('Microphone recording is not supported in this browser');
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      inst.recorderStream = stream;
      inst.recorderChunks = [];
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/mp4')
          ? 'audio/mp4'
          : '';
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      inst.mediaRecorder = recorder;
      inst.recorderStartTime = Date.now();

      if (formEl) formEl.style.display = 'none';
      if (recordingBarEl) recordingBarEl.style.display = 'flex';
      if (recTimerEl) recTimerEl.textContent = '0:00';

      inst.recorderTimer = setInterval(() => {
        const elapsed = Math.floor((Date.now() - inst.recorderStartTime) / 1000);
        if (recTimerEl) recTimerEl.textContent = formatVoiceDuration(elapsed);
        if (elapsed >= 60) {
          stopVoiceRecording(containerId, false);
        }
      }, 300);

      recorder.ondataavailable = (ev) => {
        if (ev.data && ev.data.size > 0) inst.recorderChunks.push(ev.data);
      };

      recorder.onstop = async () => {
        if (recordingBarEl) recordingBarEl.style.display = 'none';
        if (formEl) formEl.style.display = 'flex';
        const durationSec = Math.max(1, Math.round((Date.now() - inst.recorderStartTime) / 1000));
        if (recorder._cancelled || !inst.recorderChunks.length) {
          inst.recorderChunks = [];
          return;
        }
        const actualMime = recorder.mimeType || 'audio/webm';
        const ext = actualMime.includes('mp4') ? 'm4a' : 'webm';
        const voiceBlob = new Blob(inst.recorderChunks, { type: actualMime });
        const dataUrl = await readBlobAsDataUrl(voiceBlob);
        inst.pendingMedia = {
          blob: voiceBlob,
          dataUrl,
          type: 'voice',
          ext,
          mime: actualMime,
          duration: durationSec,
          name: `Voice Message (${formatVoiceDuration(durationSec)})`,
        };
        await submitDiscussionMessage(containerId);
      };

      recorder.start();
    } catch (err) {
      showToast('Microphone permission denied or unavailable');
    }
  });

  recCancelEl?.addEventListener('click', () => {
    stopVoiceRecording(containerId, true);
  });

  recSendEl?.addEventListener('click', () => {
    stopVoiceRecording(containerId, false);
  });

  async function submitDiscussionMessage() {
    const currentUser = getGoogleAuthenticatedUser();
    if (!currentUser) {
      showToast('Please sign in with Google to post');
      renderDiscussionShell(containerId);
      return;
    }

    const content = (inputEl?.value || '').trim();
    const pm = inst.pendingMedia;
    if (!content && !pm) {
      showToast('Write a message or attach media first');
      return;
    }

    if (submitBtnEl) {
      submitBtnEl.disabled = true;
      submitBtnEl.classList.add('is-sending');
    }

    // 1. Upload media to Supabase Storage (`discussion-media` bucket) if available, otherwise fallback to Data URL
    let mediaUrl = '';
    let mediaType = '';
    let mediaDuration = 0;
    if (pm) {
      mediaType = pm.type;
      mediaDuration = pm.duration || 0;
      const cloudUrl = await uploadDiscussionMediaToSupabase(pm.blob, pm.ext, pm.mime);
      mediaUrl = cloudUrl || pm.dataUrl || '';
    }

    const payload = {
      anime_id: inst.animeId,
      episode_number: inst.episodeNumber,
      user_id: currentUser.id,
      user_name: currentUser.name,
      user_email: currentUser.email,
      user_avatar: currentUser.avatar,
      auth_provider: 'google',
      content,
      media_url: mediaUrl,
      media_type: mediaType,
      media_duration: mediaDuration,
      is_spoiler: inst.isSpoiler,
    };

    // 2. Try posting to Supabase `anime_comments` AND backend API `/api/anime/{id}/comments`
    let createdComment = await postDiscussionMessageToSupabase(payload);
    try {
      const res = await fetch(`${API_BASE}/anime/${inst.animeId}/comments`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        const data = await res.json();
        if (data?.comment && !createdComment) {
          createdComment = data.comment;
        }
      }
    } catch (err) {}

    if (!createdComment) {
      createdComment = {
        comment_id: Date.now(),
        anime_id: inst.animeId,
        episode_number: inst.episodeNumber,
        user_id: currentUser.id,
        user_name: currentUser.name,
        user_email: currentUser.email,
        user_avatar: currentUser.avatar,
        auth_provider: 'google',
        content,
        media_url: mediaUrl,
        media_type: mediaType,
        media_duration: mediaDuration,
        is_spoiler: inst.isSpoiler ? 1 : 0,
        likes: 0,
        created_at: Date.now(),
      };
    }

    const cached = getLocalCommentsCache(inst.animeId);
    const mergedCache = [createdComment, ...cached.filter(c => Number(c.comment_id) !== Number(createdComment.comment_id))];
    saveLocalCommentsCache(inst.animeId, mergedCache);

    inst.comments = [createdComment, ...(inst.comments || [])];
    inst.pendingMedia = null;
    inst.isSpoiler = false;
    if (inputEl) inputEl.value = '';
    if (fileInputEl) fileInputEl.value = '';
    syncSpoilerUI();
    updateMediaPreviewUI();
    if (submitBtnEl) {
      submitBtnEl.disabled = false;
      submitBtnEl.classList.remove('is-sending');
    }

    renderDiscussionFeed(containerId, true);
  }

  formEl?.addEventListener('submit', async (e) => {
    e.preventDefault();
    await submitDiscussionMessage();
  });
}

async function loadDiscussionComments(containerId) {
  const inst = activeDiscussionInstances[containerId];
  if (!inst) return;

  const [supaComments, apiRes] = await Promise.all([
    fetchDiscussionMessagesFromSupabase(inst.animeId, inst.episodeNumber, inst.scope),
    fetchApi(`/anime/${inst.animeId}/comments?episode_number=${inst.episodeNumber}&scope=${encodeURIComponent(inst.scope)}`)
  ]);

  const serverComments = (apiRes && Array.isArray(apiRes.comments)) ? apiRes.comments : [];
  const localAll = getLocalCommentsCache(inst.animeId);
  const localFiltered = inst.scope === 'episode'
    ? localAll.filter(c => Number(c.episode_number) === Number(inst.episodeNumber))
    : localAll;

  const byKey = new Map();
  [...supaComments, ...serverComments, ...localFiltered].forEach(c => {
    if (c && (c.content || c.media_url)) {
      const key = `${c.user_id || c.user_name}_${Math.floor(Number(c.created_at || 0) / 2000)}_${(c.content || '').slice(0, 20)}`;
      if (!byKey.has(key)) byKey.set(key, c);
    }
  });

  // Sort oldest-to-newest inside WhatsApp chat viewport so latest message sits right above input bar
  inst.comments = Array.from(byKey.values()).sort((a, b) => Number(a.created_at || 0) - Number(b.created_at || 0));
  renderDiscussionFeed(containerId, true);
}

function renderDiscussionFeed(containerId, scrollToBottom = false) {
  const inst = activeDiscussionInstances[containerId];
  const feedEl = document.getElementById(`${containerId}-feed`);
  const countPill = document.getElementById(`${containerId}-count-pill`);
  if (!inst || !feedEl) return;

  const list = inst.comments || [];
  if (countPill) {
    countPill.innerHTML = `${list.length} ${list.length === 1 ? 'Message' : 'Messages'} &middot; Ep ${inst.episodeNumber}`;
  }

  if (list.length === 0) {
    const scopeLabel = inst.scope === 'episode' ? `Episode ${inst.episodeNumber}` : 'this anime';
    feedEl.innerHTML = `
      <div class="kuro-disc-empty">
        <div class="kuro-disc-empty-title">No messages for ${escapeHtmlText(scopeLabel)} yet</div>
        <div class="kuro-disc-empty-sub">Start the conversation with a comment, photo, video, or voice note.</div>
      </div>
    `;
    return;
  }

  const currentUser = getGoogleAuthenticatedUser();
  const likedSet = getLikedCommentSet();

  feedEl.innerHTML = list.map(c => {
    const cid = Number(c.comment_id);
    const isOwner = Boolean(
      currentUser &&
      (String(c.user_id) === String(currentUser.id) ||
       (c.user_email && String(c.user_email).toLowerCase() === String(currentUser.email).toLowerCase()))
    );
    const isLiked = likedSet.has(cid);
    const isSpoiler = Boolean(Number(c.is_spoiler));
    const initial = (c.user_name || 'K')[0].toUpperCase();
    const mUrl = c.media_url || '';
    const mType = (c.media_type || '').toLowerCase();
    const mDur = Number(c.media_duration) || 0;

    let mediaHtml = '';
    if (mUrl && mType === 'image') {
      mediaHtml = `
        <div class="kuro-wa-media-box">
          <img src="${escapeHtmlText(mUrl)}" alt="Shared photo" class="kuro-wa-chat-img" loading="lazy" />
        </div>
      `;
    } else if (mUrl && mType === 'video') {
      mediaHtml = `
        <div class="kuro-wa-media-box">
          <video src="${escapeHtmlText(mUrl)}" class="kuro-wa-chat-video" controls playsinline preload="metadata"></video>
        </div>
      `;
    } else if (mUrl && mType === 'voice') {
      mediaHtml = `
        <div class="kuro-wa-voice-player" data-voice-src="${escapeHtmlText(mUrl)}">
          <button type="button" class="kuro-wa-voice-play-btn" data-voice-toggle="${cid}">Play</button>
          <div class="kuro-wa-voice-wave">
            <div class="kuro-wa-voice-progress" id="voice-prog-${cid}"></div>
            <div class="kuro-wa-voice-bars">
              <span></span><span></span><span></span><span></span><span></span><span></span>
              <span></span><span></span><span></span><span></span><span></span><span></span>
              <span></span><span></span><span></span><span></span>
            </div>
          </div>
          <span class="kuro-wa-voice-time" id="voice-time-${cid}">${formatVoiceDuration(mDur || 4)}</span>
          <audio id="voice-audio-${cid}" src="${escapeHtmlText(mUrl)}" preload="metadata"></audio>
        </div>
      `;
    }

    const textHtml = c.content
      ? (isSpoiler
          ? `
            <div class="kuro-disc-spoiler-wrap" data-spoiler-wrap="${cid}">
              <div class="kuro-wa-msg-text is-blurred">${escapeHtmlText(c.content)}</div>
              <button type="button" class="kuro-disc-spoiler-reveal-btn" data-reveal-id="${cid}">
                Spoiler &mdash; Tap to Reveal
              </button>
            </div>
          `
          : `<div class="kuro-wa-msg-text">${escapeHtmlText(c.content)}</div>`)
      : '';

    return `
      <article class="kuro-wa-row ${isOwner ? 'is-own' : ''}" data-comment-id="${cid}">
        ${!isOwner ? `
          <div class="kuro-wa-avatar">
            ${c.user_avatar
              ? `<img src="${escapeHtmlText(c.user_avatar)}" alt="${escapeHtmlText(c.user_name)}" referrerpolicy="no-referrer" />`
              : `<span>${escapeHtmlText(initial)}</span>`}
          </div>
        ` : ''}

        <div class="kuro-wa-bubble ${isOwner ? 'is-own' : ''}">
          <div class="kuro-wa-bubble-top">
            <span class="kuro-wa-author">${escapeHtmlText(isOwner ? 'You' : (c.user_name || 'Kuro Member'))}</span>
            <span class="kuro-wa-ep-badge">EP ${Number(c.episode_number) || 1}</span>
          </div>

          ${mediaHtml}
          ${textHtml}

          <div class="kuro-wa-bubble-meta">
            <div class="kuro-wa-bubble-actions">
              <button type="button" class="kuro-wa-mini-btn ${isLiked ? 'liked' : ''}" data-like-id="${cid}">
                Helpful ${Number(c.likes) > 0 ? `(${Number(c.likes)})` : ''}
              </button>
              ${isOwner ? `
                <button type="button" class="kuro-wa-mini-btn kuro-wa-del-btn" data-delete-id="${cid}">
                  Delete
                </button>
              ` : ''}
            </div>
            <time class="kuro-wa-time">${formatCommentRelativeTime(c.created_at)}</time>
          </div>
        </div>
      </article>
    `;
  }).join('');

  if (scrollToBottom) {
    feedEl.scrollTop = feedEl.scrollHeight;
  }

  // Spoiler reveal buttons
  feedEl.querySelectorAll('[data-reveal-id]').forEach(btn => {
    btn.addEventListener('click', () => {
      const wrap = btn.closest('.kuro-disc-spoiler-wrap');
      wrap?.querySelector('.kuro-wa-msg-text')?.classList.remove('is-blurred');
      btn.remove();
    });
  });

  // Voice Note Audio Players
  feedEl.querySelectorAll('[data-voice-toggle]').forEach(btn => {
    const cid = btn.getAttribute('data-voice-toggle');
    const audio = document.getElementById(`voice-audio-${cid}`);
    const prog = document.getElementById(`voice-prog-${cid}`);
    const timeEl = document.getElementById(`voice-time-${cid}`);
    if (!audio) return;

    audio.addEventListener('loadedmetadata', () => {
      if (audio.duration && isFinite(audio.duration) && timeEl) {
        timeEl.textContent = formatVoiceDuration(audio.duration);
      }
    });

    audio.addEventListener('timeupdate', () => {
      if (audio.duration && isFinite(audio.duration)) {
        const pct = Math.min(100, (audio.currentTime / audio.duration) * 100);
        if (prog) prog.style.width = `${pct}%`;
        if (timeEl) timeEl.textContent = formatVoiceDuration(audio.currentTime);
      }
    });

    audio.addEventListener('ended', () => {
      btn.textContent = 'Play';
      btn.classList.remove('playing');
      if (prog) prog.style.width = '0%';
      if (audio.duration && isFinite(audio.duration) && timeEl) {
        timeEl.textContent = formatVoiceDuration(audio.duration);
      }
    });

    btn.addEventListener('click', () => {
      if (audio.paused) {
        document.querySelectorAll('.kuro-wa-voice-player audio').forEach(a => {
          if (a !== audio) a.pause();
        });
        audio.play().catch(() => {});
        btn.textContent = 'Pause';
        btn.classList.add('playing');
      } else {
        audio.pause();
        btn.textContent = 'Play';
        btn.classList.remove('playing');
      }
    });
  });

  // Like / Helpful buttons
  feedEl.querySelectorAll('[data-like-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const user = getGoogleAuthenticatedUser();
      if (!user) {
        showToast('Sign in with Google to react to messages');
        return;
      }
      const cid = Number(btn.getAttribute('data-like-id'));
      const setObj = getLikedCommentSet();
      const alreadyLiked = setObj.has(cid);
      if (alreadyLiked) setObj.delete(cid);
      else setObj.add(cid);
      saveLikedCommentSet(setObj);

      const target = inst.comments.find(x => Number(x.comment_id) === cid);
      if (target) {
        target.likes = Math.max(0, (Number(target.likes) || 0) + (alreadyLiked ? -1 : 1));
        saveLocalCommentsCache(inst.animeId, inst.comments);
      }
      renderDiscussionFeed(containerId, false);

      try {
        await fetch(`${API_BASE}/anime/${inst.animeId}/comments/${cid}/like`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ unlike: alreadyLiked }),
        });
      } catch (e) {}
    });
  });

  // Delete buttons (Author only)
  feedEl.querySelectorAll('[data-delete-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const user = getGoogleAuthenticatedUser();
      if (!user) return;
      const cid = Number(btn.getAttribute('data-delete-id'));
      inst.comments = (inst.comments || []).filter(x => Number(x.comment_id) !== cid);
      const cached = getLocalCommentsCache(inst.animeId).filter(x => Number(x.comment_id) !== cid);
      saveLocalCommentsCache(inst.animeId, cached);
      renderDiscussionFeed(containerId, false);
      showToast('Message deleted');

      deleteDiscussionMessageFromSupabase(cid);
      try {
        await fetch(
          `${API_BASE}/anime/${inst.animeId}/comments/${cid}?user_id=${encodeURIComponent(user.id)}&user_email=${encodeURIComponent(user.email)}`,
          { method: 'DELETE' }
        );
      } catch (e) {}
    });
  });
}

// ─── Watchlist & History View ───────────────────────────────────────
function renderWatchlist() {
  const grid = document.getElementById('watchlist-grid');
  if (!grid) return;

  const showSaved = document.getElementById('tab-btn-saved')?.classList.contains('active');
  const items = showSaved ? state.watchlist : state.history;

  if (!items.length) {
    grid.innerHTML = `
      <div class="empty-state" style="grid-column: 1 / -1;">
        <i data-lucide="bookmark" class="empty-state-icon" style="width:48px;height:48px"></i><br/>
        Your ${showSaved ? 'Watchlist' : 'Watch History'} is empty.<br/>
        <span style="font-size:0.8rem;color:var(--text-muted)">Start browsing anime to save your favorites!</span>
      </div>
    `;
    refreshIcons();
    return;
  }

  grid.innerHTML = items.map(renderAnimeCard).join('');
  grid.querySelectorAll('.anime-card').forEach(card => {
    card.addEventListener('click', (e) => {
        if (e.ctrlKey || e.metaKey || e.shiftKey || e.button !== 0) return;
        e.preventDefault();
        openAnimeDetail(Number(card.dataset.id));
      });
  });
  refreshIcons();
}

// ─── Global Event Listeners & App Mount ─────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  // Navigation Tabs (Mobile & Desktop)
  document.querySelectorAll('.tab-item').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
  document.querySelectorAll('.desktop-nav-menu .nav-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  document.getElementById('btn-brand-home')?.addEventListener('click', () => switchTab('home'));
  document.getElementById('header-search-btn')?.addEventListener('click', () => switchTab('search'));
  document.getElementById('header-watchlist-btn')?.addEventListener('click', () => switchTab('watchlist'));

  // Watchlist Tabs
  document.getElementById('tab-btn-watching')?.addEventListener('click', e => {
    e.target.classList.add('active');
    document.getElementById('tab-btn-saved')?.classList.remove('active');
    renderWatchlist();
  });

  document.getElementById('tab-btn-saved')?.addEventListener('click', e => {
    e.target.classList.add('active');
    document.getElementById('tab-btn-watching')?.classList.remove('active');
    renderWatchlist();
  });

  // Search Input
  const searchInput = document.getElementById('search-input-field');
  searchInput?.addEventListener('input', e => {
    clearTimeout(state.searchDebounceTimer);
    state.searchDebounceTimer = setTimeout(() => {
      performSearch(e.target.value);
    }, 280);
  });

  document.getElementById('search-clear-btn')?.addEventListener('click', () => {
    if (searchInput) searchInput.value = '';
    performSearch('');
  });

  document.querySelectorAll('.tag-chip').forEach(chip => {
    chip.addEventListener('click', () => {
      if (searchInput) {
        searchInput.value = chip.textContent;
        performSearch(chip.textContent);
      }
    });
  });

  // Browse Dropdowns
  document.getElementById('browse-status-select')?.addEventListener('change', e => {
    state.browseStatus = e.target.value;
    state.browsePage = 1;
    fetchBrowseResults();
  });

  document.getElementById('browse-type-select')?.addEventListener('change', e => {
    state.browseType = e.target.value;
    state.browsePage = 1;
    fetchBrowseResults();
  });

  document.getElementById('browse-sort-select')?.addEventListener('change', e => {
    state.browseSort = e.target.value;
    state.browsePage = 1;
    fetchBrowseResults();
  });

  document.getElementById('browse-prev-page')?.addEventListener('click', () => {
    if (state.browsePage > 1) {
      state.browsePage--;
      fetchBrowseResults();
      document.getElementById('view-browse')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  });

  document.getElementById('browse-next-page')?.addEventListener('click', () => {
    state.browsePage++;
    fetchBrowseResults();
    document.getElementById('view-browse')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });

  // Header Scroll Spy (Transparent at top, Frosted glass on scroll)
  const headerEl = document.getElementById('app-header');
  window.addEventListener('scroll', () => {
    if (headerEl) {
      headerEl.classList.toggle('scrolled', window.scrollY > 40);
    }
  }, { passive: true });

  // Keyboard navigation for Hero Carousel (Desktop)
  window.addEventListener('keydown', e => {
    if (state.currentTab !== 'home') return;
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (heroCarouselState.items.length < 2) return;
    if (e.key === 'ArrowRight') {
      goToHeroSlide((heroCarouselState.currentIndex + 1) % heroCarouselState.items.length);
    } else if (e.key === 'ArrowLeft') {
      goToHeroSlide((heroCarouselState.currentIndex - 1 + heroCarouselState.items.length) % heroCarouselState.items.length);
    }
  });

  // Centered Server Funding Announcement Modal (BNB Smart Chain)
  const BNB_WALLET_ADDRESS = '0x1Df63Bbf9fb1b9f1BfabD66C1c37450237454d93';
  const inboxModal = document.getElementById('brave-inbox-modal');
  const btnCloseInbox = document.getElementById('btn-close-inbox-modal');
  const btnContinue = document.getElementById('btn-continue-site');
  const btnCopyWallet = document.getElementById('btn-copy-wallet');
  const walletCardBox = document.getElementById('wallet-card-box');
  const copyWalletText = document.getElementById('copy-wallet-text');
  const headerInboxBtn = document.getElementById('header-inbox-btn');
  const inboxBadge = document.getElementById('inbox-badge');
  let isClosingModal = false;

  function openAnnouncementModal() {
    if (!inboxModal) return;
    isClosingModal = false;
    const card = inboxModal.querySelector('.inbox-modal-card');
    if (card) {
      card.style.removeProperty('--fly-x');
      card.style.removeProperty('--fly-y');
    }
    inboxModal.classList.remove('closing');
    inboxModal.classList.add('active');
    inboxModal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
  }

  function closeAnnouncementModal() {
    if (!inboxModal || isClosingModal || !inboxModal.classList.contains('active')) return;
    isClosingModal = true;

    // Instant lag-free GPU closing animation
    inboxModal.classList.add('closing');
    inboxModal.setAttribute('aria-hidden', 'true');

    setTimeout(() => {
      inboxModal.classList.remove('active', 'closing');
      document.body.style.overflow = '';
      isClosingModal = false;

      // Gentle pulse on notification bell without layout thrashing
      const bellBtn = document.getElementById('header-inbox-btn');
      if (bellBtn) {
        bellBtn.classList.remove('bell-ring-pulse');
        requestAnimationFrame(() => {
          bellBtn.classList.add('bell-ring-pulse');
          setTimeout(() => bellBtn.classList.remove('bell-ring-pulse'), 450);
        });
      }
    }, 180);
  }

  async function copyBnbWalletAddress() {
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(BNB_WALLET_ADDRESS);
      } else {
        const tempInput = document.createElement('textarea');
        tempInput.value = BNB_WALLET_ADDRESS;
        tempInput.style.position = 'fixed';
        tempInput.style.opacity = '0';
        document.body.appendChild(tempInput);
        tempInput.focus();
        tempInput.select();
        document.execCommand('copy');
        document.body.removeChild(tempInput);
      }

      // Visual feedback on buttons and strip
      document.querySelectorAll('#btn-copy-wallet, #wallet-card-box').forEach(el => el.classList.add('copied'));
      document.querySelectorAll('.icon-copy-default').forEach(el => el.style.display = 'none');
      document.querySelectorAll('.icon-copy-check').forEach(el => el.style.display = 'block');
      if (copyWalletText) copyWalletText.textContent = 'Copied';

      showToast('BNB Smart Chain address copied!');

      setTimeout(() => {
        document.querySelectorAll('#btn-copy-wallet, #wallet-card-box').forEach(el => el.classList.remove('copied'));
        document.querySelectorAll('.icon-copy-default').forEach(el => el.style.display = 'block');
        document.querySelectorAll('.icon-copy-check').forEach(el => el.style.display = 'none');
        if (copyWalletText) copyWalletText.textContent = 'Copy address';
      }, 2600);
    } catch (err) {
      showToast('Press and hold address to copy');
    }
  }

  headerInboxBtn?.addEventListener('click', () => {
    openAnnouncementModal();
  });

  btnCloseInbox?.addEventListener('click', closeAnnouncementModal);
  btnContinue?.addEventListener('click', closeAnnouncementModal);
  btnCopyWallet?.addEventListener('click', copyBnbWalletAddress);
  walletCardBox?.addEventListener('click', copyBnbWalletAddress);

  inboxModal?.addEventListener('click', (e) => {
    if (e.target === inboxModal) {
      closeAnnouncementModal();
    }
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && inboxModal?.classList.contains('active')) {
      closeAnnouncementModal();
    }
    if (e.key === 'Escape' && vegetaChatCard?.classList.contains('open')) {
      toggleVegetaChat(false);
    }
  });

  if (inboxBadge) {
    inboxBadge.style.display = 'block';
  }

  // Show server funding announcement on initial site open
  setTimeout(() => {
    openAnnouncementModal();
  }, 400);

  // ─── Prince Vegeta AI Anime Advisor ──────────────────────────────
  const vegetaToggleBtn = document.getElementById('btn-vegeta-toggle');
  const vegetaChatCard = document.getElementById('vegeta-chat-card');
  const vegetaCloseBtn = document.getElementById('btn-vegeta-close');
  const vegetaMessagesBox = document.getElementById('vegeta-messages-box');
  const vegetaTypingIndicator = document.getElementById('vegeta-typing-indicator');
  const vegetaChatForm = document.getElementById('vegeta-chat-form');
  const vegetaChatInput = document.getElementById('vegeta-chat-input');

  function toggleVegetaChat(forceOpen) {
    if (!vegetaChatCard) return;
    const shouldOpen = typeof forceOpen === 'boolean' ? forceOpen : !vegetaChatCard.classList.contains('open');
    if (shouldOpen) {
      vegetaChatCard.classList.add('open');
      vegetaChatCard.setAttribute('aria-hidden', 'false');
      setTimeout(() => vegetaChatInput?.focus(), 150);
    } else {
      vegetaChatCard.classList.remove('open');
      vegetaChatCard.setAttribute('aria-hidden', 'true');
    }
  }

  vegetaToggleBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleVegetaChat();
  });

  vegetaCloseBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleVegetaChat(false);
  });

  // Markdown renderer for Vegeta's formatted output with Material You icons
  function renderVegetaMarkdown(raw) {
    if (!raw) return '';
    let text = raw
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');

    // Strip stray backticks wrapped around watch links: `[Watch on KuroAnime](#anime/123)`
    text = text.replace(/`(\[[^\]]+\]\(#anime\/\d+\))`/g, '$1');

    // Headings: ## 1. Anime Title
    text = text.replace(/^##\s*(.*?)$/gm, '<div class="vegeta-rec-header"><span class="rec-hash">##</span> $1</div>');

    // Bold text: **bold**
    text = text.replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>');

    // Star rating highlight: ★ [Score]
    text = text.replace(/★\s*([\d\.]+(?:\/10)?(?:\s*\([^\)]+\))?)/g, '<span class="vegeta-star-pill">★ $1</span>');

    // Watch Links: [▶ Watch on KuroAnime](#anime/123) or [Watch on KuroAnime](#anime/123)
    text = text.replace(/\[([^\]]+)\]\(#anime\/(\d+)\)/g, (match, label, animeId) => {
      const cleanLabel = label.replace(/^[▶\s]+/, '');
      return `<button type="button" class="vegeta-watch-link" data-anime-id="${animeId}">${cleanLabel}</button>`;
    });

    // Handle quote blocks: > Quote
    text = text.replace(/^>\s*(.*?)$/gm, '<blockquote style="border-left: 2px solid #FF2A4D; padding-left: 8px; margin: 6px 0; color: #94A3B8; font-size: 0.72rem;">$1</blockquote>');

    // Split paragraphs
    const paragraphs = text.split(/\n{2,}/).map(p => {
      const trimmed = p.trim();
      if (!trimmed) return '';
      if (trimmed.startsWith('<div') || trimmed.startsWith('<blockquote')) return trimmed;
      return `<p>${trimmed.replace(/\n/g, '<br/>')}</p>`;
    }).filter(Boolean);

    return paragraphs.join('');
  }

  function appendVegetaMessage(role, content) {
    if (!vegetaMessagesBox) return;
    const row = document.createElement('div');
    row.className = `vegeta-msg-row vegeta-${role}`;

    if (role === 'bot') {
      const avatar = document.createElement('div');
      avatar.className = 'vegeta-msg-avatar';
      avatar.innerHTML = '<img src="/vegeta.gif?v=2" alt="Vegeta" />';
      row.appendChild(avatar);
    }

    const bubble = document.createElement('div');
    bubble.className = 'vegeta-msg-bubble';

    if (role === 'user') {
      const p = document.createElement('p');
      p.textContent = content;
      bubble.appendChild(p);
    } else {
      bubble.innerHTML = renderVegetaMarkdown(content);
      bubble.querySelectorAll('.vegeta-watch-link').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          const animeId = Number(btn.getAttribute('data-anime-id'));
          if (animeId) {
            openAnimeDetail(animeId, true);
          }
        });
      });
    }

    row.appendChild(bubble);
    vegetaMessagesBox.appendChild(row);
    vegetaMessagesBox.scrollTop = vegetaMessagesBox.scrollHeight;
  }

  const vegetaConversation = [];

  async function sendVegetaMessage(promptText) {
    const text = (promptText || vegetaChatInput?.value || '').trim();
    if (!text) return;

    if (vegetaChatInput) vegetaChatInput.value = '';

    appendVegetaMessage('user', text);

    if (vegetaTypingIndicator) {
      vegetaTypingIndicator.style.display = 'flex';
      vegetaMessagesBox.scrollTop = vegetaMessagesBox.scrollHeight;
    }

    localStorage.removeItem('kuro_gemini_api_key');
    // Send the last 12 turns so Vegeta remembers the conversation like a real person
    const payload = { message: text, history: vegetaConversation.slice(-12) };
    vegetaConversation.push({ role: 'user', text });

    try {
      let res;
      try {
        res = await fetch('/api/ai/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      } catch (networkErr) {
        res = await fetch('http://127.0.0.1:8000/api/ai/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
      }

      if (!res.ok) {
        throw new Error(`Server returned ${res.status}`);
      }

      const data = await res.json();
      if (vegetaTypingIndicator) vegetaTypingIndicator.style.display = 'none';

      if (data && data.reply) {
        vegetaConversation.push({ role: 'model', text: data.reply });
        if (vegetaConversation.length > 24) vegetaConversation.splice(0, vegetaConversation.length - 24);
        appendVegetaMessage('bot', data.reply);
      } else {
        appendVegetaMessage('bot', 'Hmph! Even the Prince of Saiyans got no answer from this archive. Try another genre, warrior!');
      }
    } catch (err) {
      console.error('Vegeta Advisor error:', err);
      if (vegetaTypingIndicator) vegetaTypingIndicator.style.display = 'none';
      appendVegetaMessage('bot', 'Tch! A temporary signal disruption stood in my way. Ask me again, warrior!');
    }
  }

  vegetaChatForm?.addEventListener('submit', (e) => {
    e.preventDefault();
    sendVegetaMessage();
  });

  vegetaMessagesBox?.addEventListener('click', (e) => {
    const chip = e.target.closest('.quick-chip');
    if (chip) {
      const prompt = chip.getAttribute('data-prompt') || chip.textContent.trim();
      sendVegetaMessage(prompt);
    }
  });

  document.addEventListener('click', (e) => {
    const root = document.getElementById('vegeta-ai-root');
    if (root && !root.contains(e.target) && vegetaChatCard?.classList.contains('open')) {
      toggleVegetaChat(false);
    }
  });


// ─── Visual Settings (Ambient Glow / Motion) ─────────────────────────
function applyVisualSettings() {
  const ambientOn = localStorage.getItem('kuro_setting_ambient') !== 'false';
  const physicsOn = localStorage.getItem('kuro_setting_physics') !== 'false';
  document.body.classList.toggle('kuro-ambient-off', !ambientOn);
  document.body.classList.toggle('kuro-reduced-motion', !physicsOn);
}

// ─── User Profile, Google Sign-In & Settings Modal Controller ───────
function initUserMenuAndSettings() {
  const userBtn = document.getElementById('header-user-btn');
  const userMenuWrap = document.getElementById('user-menu-wrapper');
  const userPopover = document.getElementById('user-dropdown-popover');
  const userStatusDot = document.getElementById('user-status-dot');
  const userDisplayName = document.getElementById('user-display-name');
  const userDisplayStatus = document.getElementById('user-display-status');
  const userAvatarBadge = document.getElementById('user-avatar-badge');
  const googleLoginBtn = document.getElementById('btn-user-google-login');
  const googleLoginLabel = document.getElementById('google-login-label');
  const googleLoginSub = document.getElementById('google-login-sub');

  // Modals
  const settingsModal = document.getElementById('kuro-settings-modal');
  const btnCloseSettings = document.getElementById('btn-close-settings-modal');
  const btnDoneSettings = document.getElementById('btn-done-settings');
  const btnUserSettings = document.getElementById('btn-user-settings');
  const btnClearHistory = document.getElementById('btn-clear-watch-history');

  const googleModal = document.getElementById('kuro-google-auth-modal');
  const btnCloseGoogle = document.getElementById('btn-close-google-auth');
  const btnConfirmGoogle = document.getElementById('btn-google-confirm-signin');
  const btnGoogleAccountSelect = document.getElementById('btn-google-account-select');

  // Load User Auth State
  function renderAuthState() {
    const savedUser = JSON.parse(localStorage.getItem('kuro_user_profile') || 'null');
    if (savedUser && savedUser.email) {
      if (userDisplayName) userDisplayName.textContent = savedUser.name || 'Kuro Member';
      if (userDisplayStatus) userDisplayStatus.textContent = savedUser.email;
      if (userStatusDot) {
        userStatusDot.classList.remove('guest');
      }
      if (userAvatarBadge) {
        if (savedUser.avatar) {
          userAvatarBadge.innerHTML = `<img src="${savedUser.avatar}" alt="${savedUser.name}" class="user-avatar-img" />`;
        } else {
          userAvatarBadge.innerHTML = `<span class="user-avatar-initial">${(savedUser.name || 'K')[0].toUpperCase()}</span>`;
        }
      }
      if (googleLoginLabel) googleLoginLabel.textContent = 'Sign Out';
      if (googleLoginSub) googleLoginSub.textContent = `Logged in as ${savedUser.name || 'Otaku'}`;
      if (googleLoginBtn) {
        googleLoginBtn.classList.add('logged-in');
      }
    } else {
      if (userDisplayName) userDisplayName.textContent = 'Guest';
      if (userDisplayStatus) userDisplayStatus.textContent = 'Ad-free streaming';
      if (userStatusDot) {
        userStatusDot.classList.add('guest');
      }
      if (userAvatarBadge) {
        userAvatarBadge.innerHTML = '<i data-lucide="user"></i>';
      }
      if (googleLoginLabel) googleLoginLabel.textContent = 'Sign in with Google';
      if (googleLoginSub) googleLoginSub.textContent = 'Sync library and watch history';
      if (googleLoginBtn) {
        googleLoginBtn.classList.remove('logged-in');
      }
    }
    refreshIcons();
    refreshActiveDiscussions();
  }

  // Toggle Dropdown
  userBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = userPopover?.classList.contains('open');
    if (isOpen) {
      userPopover?.classList.remove('open');
      userBtn.setAttribute('aria-expanded', 'false');
      userBtn.classList.remove('active');
    } else {
      userPopover?.classList.add('open');
      userBtn.setAttribute('aria-expanded', 'true');
      userBtn.classList.add('active');
    }
  });

  // Global Click Outside to close Popover
  document.addEventListener('click', (e) => {
    if (userMenuWrap && !userMenuWrap.contains(e.target)) {
      userPopover?.classList.remove('open');
      userBtn?.setAttribute('aria-expanded', 'false');
      userBtn?.classList.remove('active');
    }
  });

  // Helper to apply user state from Supabase Auth
  async function applySupabaseUser(user) {
    if (user && user.email) {
      const profile = {
        name: user.user_metadata?.full_name || user.user_metadata?.name || user.email.split('@')[0] || 'Kuro Member',
        email: user.email,
        avatar: user.user_metadata?.avatar_url || null,
        provider: 'google',
        id: user.id
      };
      localStorage.setItem('kuro_user_profile', JSON.stringify(profile));
      renderAuthState();
      await syncCloudUserData();
      return;
    } else {
      localStorage.removeItem('kuro_user_profile');
    }
    renderAuthState();
  }

  // Subscribe to live Supabase session and auth state changes
  if (supabase) {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session?.user) {
        applySupabaseUser(session.user);
      }
    }).catch(err => console.warn('[Supabase session check]', err));

    supabase.auth.onAuthStateChange((event, session) => {
      if (session?.user) {
        applySupabaseUser(session.user);
        if (event === 'SIGNED_IN') {
          showToast(`Welcome! Signed in as ${session.user.user_metadata?.full_name || session.user.email}`);
        }
      } else if (event === 'SIGNED_OUT') {
        applySupabaseUser(null);
      }
    });
  }

  // Google Login / Logout Flow via Supabase
  googleLoginBtn?.addEventListener('click', async (e) => {
    e.stopPropagation();
    userPopover?.classList.remove('open');
    userBtn?.setAttribute('aria-expanded', 'false');
    userBtn?.classList.remove('active');

    const currentUser = JSON.parse(localStorage.getItem('kuro_user_profile') || 'null');
    if (currentUser) {
      // User is already signed in -> Handle Sign Out
      if (supabase) {
        try { await supabase.auth.signOut(); } catch (err) { console.warn(err); }
      }
      localStorage.removeItem('kuro_user_profile');
      state.watchlist = [];
      state.history = [];
      localStorage.removeItem('kuro_watchlist');
      localStorage.removeItem('kuro_history');
      updateWatchlistBadge();
      renderContinueWatchingRail();
      if (state.currentTab === 'watchlist') renderWatchlist();
      renderAuthState();
      showToast('Signed out. Browsing as guest.');
    } else {
      // Trigger Supabase Google OAuth
      triggerGoogleOAuth();
    }
  });

  async function triggerGoogleOAuth() {
    if (supabase) {
      try {
        const { error } = await supabase.auth.signInWithOAuth({
          provider: 'google',
          options: {
            redirectTo: window.location.origin
          }
        });
        if (error) {
          console.warn('[Supabase OAuth]', error.message);
          showToast(`Supabase Google Auth: ${error.message}`);
          if (googleModal) {
            googleModal.classList.add('active');
            googleModal.setAttribute('aria-hidden', 'false');
            document.body.style.overflow = 'hidden';
          }
        }
      } catch (err) {
        console.warn('[Supabase OAuth Exception]', err);
        if (googleModal) {
          googleModal.classList.add('active');
          googleModal.setAttribute('aria-hidden', 'false');
          document.body.style.overflow = 'hidden';
        }
      }
    } else {
      if (googleModal) {
        googleModal.classList.add('active');
        googleModal.setAttribute('aria-hidden', 'false');
        document.body.style.overflow = 'hidden';
      }
    }
  }

  function completeDemoGoogleSignIn() {
    const mockGoogleUser = {
      id: 'google_kuro_otaku',
      name: 'Kuro Otaku',
      email: 'otaku.member@gmail.com',
      avatar: null,
      provider: 'google',
      signedInAt: Date.now()
    };
    localStorage.setItem('kuro_user_profile', JSON.stringify(mockGoogleUser));
    if (googleModal) {
      googleModal.classList.remove('active');
      googleModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }
    renderAuthState();
    showToast('Signed in with Google as Kuro Otaku');
  }

  window.kuroSignInWithGoogle = triggerGoogleOAuth;

  btnConfirmGoogle?.addEventListener('click', completeDemoGoogleSignIn);
  btnGoogleAccountSelect?.addEventListener('click', completeDemoGoogleSignIn);

  btnCloseGoogle?.addEventListener('click', () => {
    if (googleModal) {
      googleModal.classList.remove('active');
      googleModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }
  });

  googleModal?.addEventListener('click', (e) => {
    if (e.target === googleModal) {
      googleModal.classList.remove('active');
      googleModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }
  });

  // Settings Modal Controls
  btnUserSettings?.addEventListener('click', (e) => {
    e.stopPropagation();
    userPopover?.classList.remove('open');
    userBtn?.setAttribute('aria-expanded', 'false');
    userBtn?.classList.remove('active');

    if (settingsModal) {
      syncSettingsUI();
      settingsModal.classList.add('active');
      settingsModal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
    }
  });

  function closeSettings() {
    if (settingsModal) {
      settingsModal.classList.remove('active');
      settingsModal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';
    }
  }

  btnCloseSettings?.addEventListener('click', closeSettings);
  btnDoneSettings?.addEventListener('click', () => {
    closeSettings();
    showToast('Preferences saved successfully');
  });

  settingsModal?.addEventListener('click', (e) => {
    if (e.target === settingsModal) {
      closeSettings();
    }
  });

  // Synchronize Settings Values & Event Handlers
  const emitSetting = (key, value) => {
    window.dispatchEvent(new CustomEvent('kuro:settings-changed', { detail: { key, value, source: 'settings' } }));
  };

  function syncSettingsUI() {
    const autoplayToggle = document.getElementById('setting-toggle-autoplay');
    const skipToggle = document.getElementById('setting-toggle-skip');
    const ambientToggle = document.getElementById('setting-toggle-ambient');
    const physicsToggle = document.getElementById('setting-toggle-physics');

    if (autoplayToggle) autoplayToggle.checked = localStorage.getItem('kuro_setting_autoplay') !== 'false';
    // Shared key with the in-player "Auto-Skip Intro" checkbox
    if (skipToggle) skipToggle.checked = localStorage.getItem('kuro_auto_skip') === 'true';
    if (ambientToggle) ambientToggle.checked = localStorage.getItem('kuro_setting_ambient') !== 'false';
    if (physicsToggle) physicsToggle.checked = localStorage.getItem('kuro_setting_physics') !== 'false';

    const currentAudio = localStorage.getItem('kuro_preferred_audio') || 'sub';
    document.querySelectorAll('#setting-audio-control .segment-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.audio === currentAudio);
    });

    const currentQuality = localStorage.getItem('kuro_setting_quality') || 'auto';
    document.querySelectorAll('#setting-quality-control .segment-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.quality === currentQuality);
    });
  }

  // Keep the modal in sync when the player itself changes a setting
  window.addEventListener('kuro:settings-changed', (e) => {
    if (e.detail?.source === 'player' && settingsModal?.classList.contains('active')) syncSettingsUI();
  });

  document.getElementById('setting-toggle-autoplay')?.addEventListener('change', (e) => {
    localStorage.setItem('kuro_setting_autoplay', String(e.target.checked));
    emitSetting('autoplay', e.target.checked);
  });

  document.getElementById('setting-toggle-skip')?.addEventListener('change', (e) => {
    localStorage.setItem('kuro_auto_skip', String(e.target.checked));
    emitSetting('autoSkip', e.target.checked);
  });

  document.getElementById('setting-toggle-ambient')?.addEventListener('change', (e) => {
    localStorage.setItem('kuro_setting_ambient', String(e.target.checked));
    applyVisualSettings();
  });

  document.getElementById('setting-toggle-physics')?.addEventListener('change', (e) => {
    localStorage.setItem('kuro_setting_physics', String(e.target.checked));
    applyVisualSettings();
  });

  document.querySelectorAll('#setting-audio-control .segment-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#setting-audio-control .segment-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const audioVal = btn.dataset.audio;
      localStorage.setItem('kuro_preferred_audio', audioVal);

      // If an episode is playing, switch its audio track right away
      const watching = document.getElementById('view-watch')?.classList.contains('active');
      if (watching && state.activeAudioType !== audioVal) {
        document.getElementById(`btn-audio-${audioVal}`)?.click();
      } else {
        state.activeAudioType = audioVal;
        showToast(`Default audio set to ${audioVal.toUpperCase()}`);
      }
    });
  });

  document.querySelectorAll('#setting-quality-control .segment-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#setting-quality-control .segment-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const qVal = btn.dataset.quality;
      localStorage.setItem('kuro_setting_quality', qVal);
      emitSetting('quality', qVal);
      showToast(`Preferred quality set to ${qVal === 'auto' ? 'Auto' : qVal}`);
    });
  });

  btnClearHistory?.addEventListener('click', () => {
    // 1. Continue-watching list
    state.history = [];
    localStorage.removeItem('kuro_history');
    deleteWatchHistoryFromCloud();

    // 2. Saved playback positions for every episode (kuro_pos_<anime>_<episode>)
    Object.keys(localStorage)
      .filter(k => k.startsWith('kuro_pos_'))
      .forEach(k => localStorage.removeItem(k));

    // 3. Refresh any visible UI that shows history
    document.querySelectorAll('.continue-card').forEach(card => {
      card.closest('.rail-section')?.remove();
    });
    if (state.currentTab === 'watchlist') renderWatchlist();

    showToast('Watch history cleared');
  });

  // Global Escape Key Listener for Modals & Dropdown
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      if (googleModal?.classList.contains('active')) {
        googleModal.classList.remove('active');
        document.body.style.overflow = '';
      } else if (settingsModal?.classList.contains('active')) {
        closeSettings();
      } else if (userPopover?.classList.contains('open')) {
        userPopover.classList.remove('open');
        userBtn?.setAttribute('aria-expanded', 'false');
        userBtn?.classList.remove('active');
      }
    }
  });

  // Initialize display
  renderAuthState();
}

// ─── Share & Sync Watch History and Library (Google Auth Gated) ──────
function initSyncModal() {
  const modalBackdrop = document.getElementById('kuro-sync-share-modal');
  const btnClose = document.getElementById('btn-close-sync-modal');
  const btnOpenHeader = document.getElementById('btn-user-sync-share');
  const btnOpenCollection = document.getElementById('btn-open-sync-modal');

  const authGate = document.getElementById('sync-auth-gate');
  const authContent = document.getElementById('sync-auth-content');
  const btnAuthTrigger = document.getElementById('btn-sync-auth-trigger');

  const tabShare = document.getElementById('tab-btn-share-pane');
  const tabImport = document.getElementById('tab-btn-import-pane');
  const panelShare = document.getElementById('panel-sync-share');
  const panelImport = document.getElementById('panel-sync-import');

  const statLib = document.getElementById('sync-stat-lib-count');
  const statHist = document.getElementById('sync-stat-hist-count');
  const btnGenerate = document.getElementById('btn-trigger-generate-code');
  const btnGenerateLabel = document.getElementById('btn-generate-code-label');
  const codeSection = document.getElementById('sync-code-section');
  const codeTokenText = document.getElementById('sync-code-token-text');
  const linkInput = document.getElementById('sync-link-input');
  const btnCopyCode = document.getElementById('btn-copy-sync-code');
  const btnCopyCodeLabel = document.getElementById('btn-copy-code-label');
  const btnCopyLink = document.getElementById('btn-copy-sync-link');

  const inputCode = document.getElementById('input-sync-code');
  const btnLookup = document.getElementById('btn-trigger-lookup-code');
  const previewCard = document.getElementById('sync-preview-container');
  const previewAuthor = document.getElementById('sync-preview-author');
  const previewDetails = document.getElementById('sync-preview-details');
  const previewSampleGrid = document.getElementById('sync-preview-sample-grid');
  const btnImportConfirm = document.getElementById('btn-trigger-import-confirm');

  let activePreviewShare = null;

  function openModal(defaultTab = 'share', prefillCode = '') {
    const user = getGoogleAuthenticatedUser();
    if (!user) {
      if (authGate) authGate.style.display = 'block';
      if (authContent) authContent.style.display = 'none';
    } else {
      if (authGate) authGate.style.display = 'none';
      if (authContent) authContent.style.display = 'block';
      if (statLib) statLib.textContent = String(state.watchlist.length || 0);
      if (statHist) statHist.textContent = String(state.history.length || 0);
    }

    if (defaultTab === 'import') {
      switchSyncTab('import');
      if (prefillCode && inputCode) {
        inputCode.value = prefillCode.toUpperCase();
        triggerLookup(prefillCode);
      }
    } else {
      switchSyncTab('share');
    }

    modalBackdrop?.classList.add('open');
    modalBackdrop?.setAttribute('aria-hidden', 'false');
    refreshIcons();
  }

  function closeModal() {
    modalBackdrop?.classList.remove('open');
    modalBackdrop?.setAttribute('aria-hidden', 'true');
  }

  function switchSyncTab(tab) {
    if (tab === 'import') {
      tabShare?.classList.remove('active');
      tabShare?.setAttribute('aria-selected', 'false');
      tabImport?.classList.add('active');
      tabImport?.setAttribute('aria-selected', 'true');
      if (panelShare) panelShare.style.display = 'none';
      if (panelImport) panelImport.style.display = 'block';
      setTimeout(() => inputCode?.focus(), 100);
    } else {
      tabImport?.classList.remove('active');
      tabImport?.setAttribute('aria-selected', 'false');
      tabShare?.classList.add('active');
      tabShare?.setAttribute('aria-selected', 'true');
      if (panelImport) panelImport.style.display = 'none';
      if (panelShare) panelShare.style.display = 'block';
    }
  }

  // Trigger Google Login if Guest
  btnAuthTrigger?.addEventListener('click', () => {
    closeModal();
    document.getElementById('btn-user-google-login')?.click();
  });

  // Open triggers
  btnOpenCollection?.addEventListener('click', () => openModal('share'));
  btnOpenHeader?.addEventListener('click', (e) => {
    e.stopPropagation();
    document.getElementById('user-dropdown-popover')?.classList.remove('open');
    openModal('share');
  });

  // Close triggers
  btnClose?.addEventListener('click', closeModal);
  modalBackdrop?.addEventListener('click', (e) => {
    if (e.target === modalBackdrop) closeModal();
  });
  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modalBackdrop?.classList.contains('open')) {
      closeModal();
    }
  });

  // Tab switching
  tabShare?.addEventListener('click', () => switchSyncTab('share'));
  tabImport?.addEventListener('click', () => switchSyncTab('import'));

  // Generate Code Handler
  btnGenerate?.addEventListener('click', async () => {
    const user = getGoogleAuthenticatedUser();
    if (!user) {
      showToast('Please sign in with Google to generate a share code');
      openModal('share');
      return;
    }

    if (btnGenerateLabel) btnGenerateLabel.textContent = 'Generating Secure Code...';
    btnGenerate.disabled = true;

    try {
      const res = await generateSyncShareCode(state.watchlist, state.history);
      if (!res.ok) {
        showToast(res.error === 'auth_required' ? 'Please sign in with Google to generate a code' : (res.error || 'Could not generate code'));
        return;
      }

      if (codeTokenText) codeTokenText.textContent = res.code;
      const shareUrl = `https://kuroanime-flax.vercel.app/sync?code=${encodeURIComponent(res.code)}`;
      if (linkInput) linkInput.value = shareUrl;
      if (codeSection) {
        codeSection.style.display = 'block';
        codeSection.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }

      showToast(`Share code ${res.code} generated successfully!`);
    } catch (e) {
      console.error('[Sync Generation Error]', e);
      showToast('Failed to generate sync code');
    } finally {
      if (btnGenerateLabel) btnGenerateLabel.textContent = 'Generate New Sync Code';
      btnGenerate.disabled = false;
      refreshIcons();
    }
  });

  // Copy Code
  btnCopyCode?.addEventListener('click', async () => {
    const code = codeTokenText?.textContent?.trim();
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      if (btnCopyCodeLabel) btnCopyCodeLabel.textContent = 'Copied!';
      showToast(`Code ${code} copied to clipboard`);
      setTimeout(() => {
        if (btnCopyCodeLabel) btnCopyCodeLabel.textContent = 'Copy';
      }, 2000);
    } catch (e) {
      showToast('Could not copy to clipboard');
    }
  });

  // Copy Link
  btnCopyLink?.addEventListener('click', async () => {
    const url = linkInput?.value?.trim();
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      btnCopyLink.textContent = 'Copied!';
      showToast('Direct sync link copied to clipboard');
      setTimeout(() => {
        btnCopyLink.textContent = 'Copy Link';
      }, 2000);
    } catch (e) {
      showToast('Could not copy link');
    }
  });

  // Lookup Code Handler
  async function triggerLookup(overrideCode = '') {
    const code = (overrideCode || inputCode?.value || '').trim();
    if (!code) {
      showToast('Please enter a share code');
      return;
    }

    if (btnLookup) {
      btnLookup.disabled = true;
      btnLookup.textContent = 'Searching...';
    }

    try {
      const res = await lookupSyncShareCode(code);
      if (!res.ok || !res.share) {
        showToast(res.error || 'Share code not found');
        if (previewCard) previewCard.style.display = 'none';
        return;
      }

      activePreviewShare = res.share;
      const share = res.share;

      if (previewAuthor) previewAuthor.textContent = `${share.user_name}'s Collection`;
      if (previewDetails) {
        previewDetails.textContent = `${share.library_count || 0} anime in library · ${share.history_count || 0} episodes watched`;
      }

      // Preview poster thumbnails
      if (previewSampleGrid) {
        const samples = (share.library_data || []).slice(0, 5);
        if (samples.length > 0) {
          previewSampleGrid.innerHTML = samples.map(item => `
            <img
              src="${item.poster || item.anime_poster || '/favicon.svg'}"
              alt="${item.title || 'Anime'}"
              class="sync-sample-poster"
              onerror="this.onerror=null;this.src='/favicon.svg';"
            />
          `).join('');
        } else {
          previewSampleGrid.innerHTML = '<span style="font-size:0.75rem;color:var(--color-text-muted)">Ready to sync anime records</span>';
        }
      }

      if (previewCard) previewCard.style.display = 'block';
      refreshIcons();
    } catch (e) {
      showToast('Error looking up sync code');
    } finally {
      if (btnLookup) {
        btnLookup.disabled = false;
        btnLookup.textContent = 'Find';
      }
    }
  }

  btnLookup?.addEventListener('click', () => triggerLookup());
  inputCode?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      triggerLookup();
    }
  });

  // Confirm Import Handler
  btnImportConfirm?.addEventListener('click', async () => {
    if (!activePreviewShare) return;
    const user = getGoogleAuthenticatedUser();
    if (!user) {
      showToast('Please sign in with Google to sync to your account');
      openModal('import');
      return;
    }

    btnImportConfirm.disabled = true;
    btnImportConfirm.innerHTML = '<span>Syncing Cloud Records...</span>';

    try {
      const res = await importSyncShare(activePreviewShare);
      if (!res.ok) {
        showToast(res.error || 'Failed to sync library');
        return;
      }

      // Merge into local state.watchlist without duplicates
      const incomingLib = activePreviewShare.library_data || [];
      for (const item of incomingLib) {
        const aId = Number(item.anime_id);
        if (aId && !state.watchlist.some(w => Number(w.anime_id) === aId)) {
          state.watchlist.unshift({
            anime_id: aId,
            title: item.title || item.anime_title || 'Anime',
            poster: item.poster || item.anime_poster || '',
            type: item.type || item.anime_type || 'TV',
            anilist_score: item.anilist_score,
            added_at: Date.now()
          });
        }
      }

      // Merge into local state.history
      const incomingHist = activePreviewShare.history_data || [];
      for (const entry of incomingHist) {
        const aId = Number(entry.anime_id);
        if (aId) {
          const existingIdx = state.history.findIndex(h => Number(h.anime_id) === aId);
          if (existingIdx === -1) {
            state.history.unshift({
              anime_id: aId,
              title: entry.title || entry.anime_title || 'Anime',
              poster: entry.poster || entry.anime_poster || '',
              episode_id: entry.episode_id || '',
              episode_number: Number(entry.episode_number) || 1,
              episode_title: entry.episode_title || '',
              progress_seconds: Number(entry.progress_seconds) || 0,
              duration_seconds: Number(entry.duration_seconds) || 0,
              completed: Boolean(entry.completed),
              timestamp: Date.now()
            });
          }
        }
      }

      localStorage.setItem('kuro_watchlist', JSON.stringify(state.watchlist));
      localStorage.setItem('kuro_history', JSON.stringify(state.history));
      updateWatchlistBadge();
      renderWatchlist();

      showToast(`Successfully synced ${res.libraryCount} anime from ${res.authorName}!`);
      closeModal();
    } catch (e) {
      showToast('Sync error occurred');
    } finally {
      btnImportConfirm.disabled = false;
      btnImportConfirm.innerHTML = '<i data-lucide="download"></i><span>Sync to My Account</span>';
      refreshIcons();
    }
  });

  return { openModal, closeModal };
}

let syncModalInstance = null;

// ─── Initial URL Route Dispatcher ────────────────────────────────────
function handleInitialRoute() {
  const path = window.location.pathname;
  const animeMatch = path.match(/^\/anime\/(\d+)/);
  const watchMatch = path.match(/^\/watch\/(\d+)/);

  if (animeMatch) {
    const id = parseInt(animeMatch[1], 10);
    openAnimeDetail(id, false);
  } else if (watchMatch) {
    const id = parseInt(watchMatch[1], 10);
    const searchParams = new URLSearchParams(window.location.search);
    const epNum = parseInt(searchParams.get('ep') || '1', 10);
    openAnimeDetail(id, true, Math.max(0, epNum - 1), epNum);
  } else if (path === '/browse') {
    switchTab('browse');
    const searchParams = new URLSearchParams(window.location.search);
    const genre = searchParams.get('genre');
    if (genre) {
      state.browseGenre = genre;
      const chip = document.querySelector(`.chip-btn[data-genre="${genre}"]`);
      if (chip) chip.click();
    }
  } else if (path === '/search') {
    switchTab('search');
    const searchParams = new URLSearchParams(window.location.search);
    const q = searchParams.get('q');
    if (q) {
      const input = document.getElementById('search-input-field');
      if (input) input.value = q;
      performSearch(q);
    }
  } else if (path === '/watchlist') {
    switchTab('watchlist');
  } else if (path === '/sync') {
    switchTab('watchlist');
    const searchParams = new URLSearchParams(window.location.search);
    const code = searchParams.get('code') || searchParams.get('sync') || '';
    setTimeout(() => syncModalInstance?.openModal('import', code), 250);
  } else {
    initHome();
  }
}

  // Init App
  initCustomSelects();
  applyVisualSettings();
  initUserMenuAndSettings();
  updateWatchlistBadge();
  syncModalInstance = initSyncModal();
  handleInitialRoute();
  refreshIcons();
});

// ─── Lifecycle Background Audio / Video Cutoff & History Flush ───────
function flushActiveWatchHistory() {
  if (getActiveViewId() === 'view-watch' && state.activeAnimeData && state.activeEpisodes[state.activeEpisodeIndex]) {
    const curEp = state.activeEpisodes[state.activeEpisodeIndex];
    const curTime = activePlayerInstance?.video?.currentTime || 0;
    const curDur = activePlayerInstance?.video?.duration || 0;
    saveHistory(state.activeAnimeData, curEp, curTime, curDur, curDur > 0 && curTime >= curDur - 25);
  }
}

window.addEventListener('pagehide', () => {
  flushActiveWatchHistory();
  stopVideoPlayback();
});
window.addEventListener('beforeunload', () => {
  flushActiveWatchHistory();
  stopVideoPlayback();
});
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    flushActiveWatchHistory();
    if (activePlayerInstance?.video && !activePlayerInstance.video.paused) {
      try { activePlayerInstance.video.pause(); } catch (e) {}
    }
  }
});
