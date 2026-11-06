import './style.css';
import { inject } from '@vercel/analytics';
import { NetflixPlayer } from './netflix_player.js';
import { 
  supabase, 
  fetchWatchHistoryFromCloud, 
  saveWatchHistoryToCloud, 
  deleteWatchHistoryFromCloud, 
  fetchLibraryFromCloud, 
  saveLibraryItemToCloud 
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
  Trash2
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

const API_BASE = (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE)
  ? import.meta.env.VITE_API_BASE.replace(/\/+$/, '')
  : (typeof window !== 'undefined' && window.__API_BASE__)
    ? window.__API_BASE__.replace(/\/+$/, '')
    : '/api';


// ─── Lucide Icons Initializer ───────────────────────────────────────
function refreshIcons() {
  createIcons({
    icons: {
      Home, Compass, Search, Bookmark, Play, Star, ArrowLeft,
      Layers, Volume2, Shield, ShieldCheck, ShieldAlert, Clock, Check, X, ChevronRight,
      ChevronLeft, RotateCcw, Sparkles, Film, Bell, ExternalLink, Download, Plus,
      User, Settings, LogOut, Trash2
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
      initHome();
    }
  }

  // Update tab active state (mobile & desktop)
  document.querySelectorAll('.tab-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === state.currentTab);
  });
  document.querySelectorAll('.desktop-nav-menu .nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === state.currentTab);
  });

  // Push browser history state so hardware/browser Back button never hits a black screen
  if (pushHistory && !isHandlingPopState) {
    try {
      const currentState = window.history.state;
      if (!currentState || currentState.viewId !== viewId || currentState.animeId !== state.activeAnimeId) {
        window.history.pushState(
          { viewId, tab: state.currentTab, animeId: state.activeAnimeId },
          '',
          window.location.pathname + window.location.search
        );
      }
    } catch (e) {}
  }
}

window.addEventListener('popstate', (e) => {
  const currentView = getActiveViewId();
  isHandlingPopState = true;
  try {
    if (currentView === 'view-watch') {
      if (state.playerEnteredFrom === 'home') {
        switchTab('home');
      } else {
        showView('view-detail', { pushHistory: false });
      }
    } else if (currentView === 'view-detail') {
      switchTab(state.currentTab || 'home');
    } else if (e.state && e.state.viewId) {
      if (e.state.viewId === 'view-home') {
        switchTab('home');
      } else {
        showView(e.state.viewId, { pushHistory: false });
      }
    } else if (currentView !== 'view-home') {
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
  } else if (state.currentTab === 'browse') {
    showView('view-browse');
    loadBrowse();
  } else if (state.currentTab === 'search') {
    showView('view-search');
    setTimeout(() => document.getElementById('search-input-field')?.focus(), 150);
  } else if (state.currentTab === 'watchlist') {
    showView('view-watchlist');
    renderWatchlist();
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

// ─── Card Renderer Component ────────────────────────────────────────
function renderAnimeCard(anime) {
  const poster = anime.poster || anime.poster_large || anime.poster_url || '';
  const score = anime.anilist_score ? (anime.anilist_score / 10).toFixed(1) : (anime.mal_score || '');
  const hasSub = anime.sub !== false;
  const hasDub = anime.dub === true;
  const inWatchlist = isWatchlisted(anime.anime_id);

  return `
    <div class="anime-card" data-id="${anime.anime_id}">
      <div class="card-poster-wrapper">
        <img class="card-poster-img" src="${poster}" alt="${anime.title}" loading="lazy" />
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
    </div>
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

  // 1. Render slides in stage with high-res 1080p/4K backdrops
  stageEl.innerHTML = items.map((item, idx) => {
    let banner = item.banner_image || item.poster_large || item.poster || '';
    if (banner.includes('image.tmdb.org')) {
      banner = banner.replace(/\/t\/p\/[^/]+/, '/t/p/w1920_and_h1080_multi_faces');
    }
    return `
      <div class="hero-slide ${idx === 0 ? 'active' : ''}" data-idx="${idx}">
        <div class="hero-slide-ambient" style="background-image: url('${banner}')" aria-hidden="true"></div>
        <img class="hero-slide-img" src="${banner}" alt="${item.title}" loading="${idx === 0 ? 'eager' : 'lazy'}" />
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
  // 1. Catalog Stats (Populates footer quietly)
  fetchApi('/stats').then(stats => {
    if (stats) {
      const footerStat = document.getElementById('footer-catalog-stats');
      if (footerStat) {
        footerStat.textContent = `Streaming ${stats.anime.toLocaleString()} Anime Series & Movies • ${stats.streams.toLocaleString()} Verified High-Speed Streams`;
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
      { key: 'top_rated', title: 'Top Rated Masterpieces' },
      { key: 'airing', title: 'Currently Airing' },
      { key: 'movies', title: 'Feature Films & Movies' },
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
      card.addEventListener('click', () => openAnimeDetail(Number(card.dataset.id)));
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
  grid.innerHTML = '<div class="empty-state">Loading anime catalog...</div>';

  const params = new URLSearchParams({
    page: state.browsePage,
    sort: state.browseSort,
    per_page: 24,
  });
  if (state.browseGenre) params.append('genre', state.browseGenre);
  if (state.browseType) params.append('type', state.browseType);
  if (state.browseStatus) params.append('status', state.browseStatus);

  const res = await fetchApi(`/browse?${params.toString()}`);
  if (!res || !res.results?.length) {
    grid.innerHTML = '<div class="empty-state">No anime found matching filters.</div>';
    const pagination = document.getElementById('browse-pagination');
    if (pagination) pagination.style.display = 'none';
    return;
  }

  grid.innerHTML = res.results.map(renderAnimeCard).join('');
  grid.querySelectorAll('.anime-card').forEach(card => {
    card.addEventListener('click', () => openAnimeDetail(Number(card.dataset.id)));
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
  resultsGrid.innerHTML = '<div class="empty-state">Searching anime...</div>';

  const res = await fetchApi(`/search?q=${encodeURIComponent(query)}`);
  if (!res || !res.results?.length) {
    resultsGrid.innerHTML = '<div class="empty-state">No matching anime found.</div>';
    return;
  }

  resultsGrid.innerHTML = res.results.map(renderAnimeCard).join('');
  resultsGrid.querySelectorAll('.anime-card').forEach(card => {
    card.addEventListener('click', () => openAnimeDetail(Number(card.dataset.id)));
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
      container.innerHTML = `
        <div class="empty-state" style="padding: 80px 20px;">
          <div style="margin-bottom: 14px;">Loading anime details...</div>
          <button type="button" class="ep-nav-btn" id="detail-loading-back-btn" style="max-width: 160px; margin: 0 auto;">Back to Home</button>
        </div>
      `;
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
        <div class="detail-header-card">
          <img class="detail-poster" src="${anime.poster}" alt="${anime.title}" />
          <div class="detail-info">
            <h1 class="detail-title">${anime.title}</h1>
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
            <i data-lucide="${watchlisted ? 'check' : 'bookmark'}"></i>
            <span>${watchlisted ? 'Saved' : 'Watchlist'}</span>
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
      </div>
    `;

    // Render the episode chunk containing the user's current/last-watched episode
    const initialChunk = chunks[activeChunkIdx] || { start: 0, end: totalEps };
    renderEpisodeChunk(initialChunk.start, initialChunk.end);

    // Events
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
      bookmarkBtn.innerHTML = `<i data-lucide="${nowSaved ? 'check' : 'bookmark'}"></i><span>${nowSaved ? 'Saved' : 'Watchlist'}</span>`;
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
      <button class="header-icon-btn ${isWatchlisted(anime.anime_id) ? 'active' : ''}" id="player-bookmark-btn" aria-label="Bookmark anime">
        <i data-lucide="${isWatchlisted(anime.anime_id) ? 'check' : 'bookmark'}"></i>
      </button>
    </div>

    <!-- Video Container -->
    <div class="video-frame-wrapper" id="video-frame-container">
      <div class="empty-state" style="padding: 60px 16px;">
        <i data-lucide="sparkles" class="empty-state-icon"></i><br/>
        Loading video stream servers...
      </div>
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
  `;

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
    playerBookmarkBtn.innerHTML = `<i data-lucide="${nowSaved ? 'check' : 'bookmark'}"></i>`;
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
    card.addEventListener('click', () => openAnimeDetail(Number(card.dataset.id)));
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
    const payload = { message: text };

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
      if (userDisplayName) userDisplayName.textContent = 'Guest Otaku';
      if (userDisplayStatus) userDisplayStatus.textContent = 'Free Member • Ad-Free';
      if (userStatusDot) {
        userStatusDot.classList.add('guest');
      }
      if (userAvatarBadge) {
        userAvatarBadge.innerHTML = '<i data-lucide="user"></i>';
      }
      if (googleLoginLabel) googleLoginLabel.textContent = 'Login with Google';
      if (googleLoginSub) googleLoginSub.textContent = 'Sync watchlist & history';
      if (googleLoginBtn) {
        googleLoginBtn.classList.remove('logged-in');
      }
    }
    refreshIcons();
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
      showToast('Signed out successfully. You are now browsing as Guest Otaku.');
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
    showToast('Signed in as Kuro Otaku (Demo Mode)');
  }

  btnConfirmGoogle?.addEventListener('click', triggerGoogleOAuth);
  btnGoogleAccountSelect?.addEventListener('click', triggerGoogleOAuth);

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

  // Init App
  initCustomSelects();
  applyVisualSettings();
  initUserMenuAndSettings();
  updateWatchlistBadge();
  initHome();
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
