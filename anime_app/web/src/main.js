import './style.css';
import { NetflixPlayer } from './netflix_player.js';

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
  Plus
} from 'lucide';

// ─── Global State & Storage ─────────────────────────────────────────
const state = {
  currentTab: 'home',
  activeAnimeId: null,
  activeAnimeData: null,
  activeEpisodes: [],
  activeEpisodeIndex: 0,
  activeAudioType: 'sub', // 'sub' | 'dub'
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
      ChevronLeft, RotateCcw, Sparkles, Film, Bell, ExternalLink, Download, Plus
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
}

// ─── View Routing ───────────────────────────────────────────────────
function showView(viewId) {
  // If moving away from player, kill video playback immediately
  if (viewId !== 'view-watch') {
    stopVideoPlayback();
  }

  document.querySelectorAll('.view-screen').forEach(el => el.classList.remove('active'));
  const target = document.getElementById(viewId);
  if (target) {
    target.classList.add('active');
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    document.documentElement.scrollLeft = 0;
    document.body.scrollLeft = 0;
  }

  // Update tab active state (mobile & desktop)
  document.querySelectorAll('.tab-item').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === state.currentTab);
  });
  document.querySelectorAll('.desktop-nav-menu .nav-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === state.currentTab);
  });
}

function switchTab(tabName) {
  state.currentTab = tabName;
  if (tabName === 'home') {
    showView('view-home');
  } else if (tabName === 'browse') {
    showView('view-browse');
    loadBrowse();
  } else if (tabName === 'search') {
    showView('view-search');
    setTimeout(() => document.getElementById('search-input-field')?.focus(), 150);
  } else if (tabName === 'watchlist') {
    showView('view-watchlist');
    renderWatchlist();
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
  } else {
    state.watchlist.unshift({
      anime_id: animeId,
      title: anime.title,
      poster: anime.poster || anime.poster_large || anime.poster_url,
      type: anime.type,
      anilist_score: anime.anilist_score,
      added_at: Date.now()
    });
    showToast(`Added to Watchlist!`);
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

function saveHistory(anime, episode) {
  const animeId = Number(anime.anime_id);
  const existingIdx = state.history.findIndex(h => Number(h.anime_id) === animeId);
  const thumb = anime.banner_image || anime.poster || anime.poster_large || anime.poster_url;
  const entry = {
    anime_id: animeId,
    title: anime.title,
    poster: thumb,
    episode_number: episode.episode_number,
    episode_title: episode.title,
    episode_id: episode.episode_id,
    timestamp: Date.now()
  };
  if (existingIdx > -1) {
    state.history.splice(existingIdx, 1);
  }
  state.history.unshift(entry);
  if (state.history.length > 50) state.history.pop();
  localStorage.setItem('kuro_history', JSON.stringify(state.history));
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
    openAnimeDetail(item.anime_id, true);
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
    // 1. Continue Watching (if history available)
    const historyHtml = (state.history && state.history.length > 0) ? `
      <div class="rail-section">
        <div class="rail-header">
          <span class="rail-title"><span class="rail-title-line"></span> Continue Watching</span>
        </div>
        <div class="rail-scroll-track">
          ${state.history.slice(0, 10).map(h => `
            <div class="continue-card" data-anime-id="${h.anime_id}" data-ep-idx="${(h.episode_number || 1) - 1}">
              <div class="continue-thumb-wrapper">
                <img class="continue-thumb-img" src="${h.poster}" alt="${h.title}" loading="lazy" />
                <div class="continue-play-overlay">
                  <div class="continue-play-icon"><i data-lucide="play"></i></div>
                </div>
                <span class="continue-ep-badge">EP ${h.episode_number || 1}</span>
                <div class="continue-progress-bar"><div class="continue-progress-fill"></div></div>
              </div>
              <div class="continue-title">${h.title}</div>
              <div class="continue-subtitle">Resume Ep ${h.episode_number || 1}</div>
            </div>
          `).join('')}
        </div>
      </div>
    ` : '';

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

    railsContainer.innerHTML = historyHtml + catalogRailsHtml;

    // Attach card clicks
    railsContainer.querySelectorAll('.anime-card').forEach(card => {
      card.addEventListener('click', () => openAnimeDetail(Number(card.dataset.id)));
    });

    railsContainer.querySelectorAll('.continue-card').forEach(cc => {
      cc.addEventListener('click', () => {
        const aId = Number(cc.dataset.animeId);
        const epIdx = Math.max(0, Number(cc.dataset.epIdx) || 0);
        openAnimeDetail(aId, true, epIdx);
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
        state.browsePage = 1;
        switchTab('browse');
      });
    });
  }

  refreshIcons();
}

// ─── Browse View ────────────────────────────────────────────────────
async function loadBrowse() {
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
    return;
  }

  grid.innerHTML = res.results.map(renderAnimeCard).join('');
  grid.querySelectorAll('.anime-card').forEach(card => {
    card.addEventListener('click', () => openAnimeDetail(Number(card.dataset.id)));
  });

  const pagination = document.getElementById('browse-pagination');
  if (pagination) {
    pagination.style.display = 'flex';
    document.getElementById('browse-page-indicator').textContent = `Page ${state.browsePage}`;
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
async function openAnimeDetail(animeId, autoPlayFirst = false, startEpIndex = 0) {
  state.activeAnimeId = animeId;
  showView('view-detail');

  const container = document.getElementById('detail-container');
  if (!container) return;
  container.innerHTML = '<div class="empty-state">Loading anime details...</div>';

  const [anime, epsData] = await Promise.all([
    fetchApi(`/anime/${animeId}`),
    fetchApi(`/anime/${animeId}/episodes`)
  ]);

  if (!anime) {
    container.innerHTML = '<div class="empty-state">Failed to load anime.</div>';
    return;
  }

  state.activeAnimeData = anime;
  state.activeEpisodes = epsData?.episodes || [];

  if (autoPlayFirst && state.activeEpisodes.length > 0) {
    const validIdx = Math.min(Math.max(0, startEpIndex), state.activeEpisodes.length - 1);
    openWatchPlayer(validIdx);
    return;
  }

  const banner = anime.banner_image || anime.poster;
  const score = anime.anilist_score ? (anime.anilist_score / 10).toFixed(1) : (anime.mal_score || 'N/A');
  const watchlisted = isWatchlisted(animeId);

  // Group episodes into chunks of 50 for big anime (e.g. One Piece)
  const totalEps = state.activeEpisodes.length;
  const chunks = [];
  const chunkSize = 50;
  for (let i = 0; i < totalEps; i += chunkSize) {
    chunks.push({ start: i, end: Math.min(i + chunkSize, totalEps) });
  }

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
          <i data-lucide="play"></i> Start Ep 1
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
              <button class="range-tab-btn ${idx === 0 ? 'active' : ''}" data-start="${c.start}" data-end="${c.end}">
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

  // Render first episode chunk
  renderEpisodeChunk(0, chunks.length > 0 ? chunks[0].end : totalEps);

  // Events
  document.getElementById('detail-back-home-btn')?.addEventListener('click', () => {
    switchTab(state.currentTab);
  });

  document.getElementById('detail-start-watch-btn')?.addEventListener('click', () => {
    if (state.activeEpisodes.length > 0) openWatchPlayer(0);
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

  refreshIcons();
}

function renderEpisodeChunk(start, end) {
  const grid = document.getElementById('episodes-grid-list');
  if (!grid) return;
  const slice = state.activeEpisodes.slice(start, end);

  grid.innerHTML = slice.map((ep, idx) => {
    const globalIdx = start + idx;
    return `
      <button class="episode-btn" data-idx="${globalIdx}">
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
      <!-- Audio Selection -->
      <div class="server-section-label">Audio Language</div>
      <div class="audio-toggle-bar">
        <button class="audio-toggle-btn ${state.activeAudioType === 'sub' ? 'active' : ''}" id="btn-audio-sub">
          <i data-lucide="volume-2"></i> SUB (Japanese)
        </button>
        <button class="audio-toggle-btn ${state.activeAudioType === 'dub' ? 'active' : ''}" id="btn-audio-dub">
          <i data-lucide="volume-2"></i> DUB (English)
        </button>
      </div>

      <!-- Server Chips -->
      <div class="server-section-label">Stream Server (Default: Zoko HD)</div>
      <div class="server-chips-row" id="player-server-chips">
        <!-- Injected below -->
      </div>

      <!-- Previous / Next / Download Controls -->
      <div class="ep-nav-buttons-row">
        <button class="ep-nav-btn" id="player-prev-ep-btn" ${episodeIndex <= 0 ? 'disabled style="opacity:0.4"' : ''}>
          <i data-lucide="chevron-left"></i> Prev
        </button>
        <button class="ep-nav-btn" id="player-download-ep-btn" style="border-color:rgba(255,42,77,0.45);color:#fff;">
          <i data-lucide="download"></i> Download Ep
        </button>
        <button class="ep-nav-btn" id="player-reload-btn">
          <i data-lucide="rotate-ccw"></i> Reload
        </button>
        <button class="ep-nav-btn" id="player-next-ep-btn" ${episodeIndex >= state.activeEpisodes.length - 1 ? 'disabled style="opacity:0.4"' : ''}>
          Next <i data-lucide="chevron-right"></i>
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
    showView('view-detail');
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
    state.activeAudioType = 'sub';
    state.activeServerIndex = 0;
    document.getElementById('btn-audio-sub')?.classList.add('active');
    document.getElementById('btn-audio-dub')?.classList.remove('active');
    renderServerChips();
    loadStreamIntoPlayer();
  });

  document.getElementById('btn-audio-dub')?.addEventListener('click', () => {
    state.activeAudioType = 'dub';
    state.activeServerIndex = 0;
    document.getElementById('btn-audio-dub')?.classList.add('active');
    document.getElementById('btn-audio-sub')?.classList.remove('active');
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

  renderServerChips();
  loadStreamIntoPlayer();
  refreshIcons();
}

function renderServerChips() {
  const chipsContainer = document.getElementById('player-server-chips');
  if (!chipsContainer) return;
  const list = state.currentStreams[state.activeAudioType] || [];

  if (!list.length) {
    chipsContainer.innerHTML = `<span style="color:var(--text-muted);font-size:0.8rem">No ${state.activeAudioType.toUpperCase()} streams available. Try switching audio language.</span>`;
    return;
  }

  if (state.activeServerIndex >= list.length) {
    state.activeServerIndex = 0;
  }

  chipsContainer.innerHTML = list.map((s, idx) => `
    <button class="server-chip ${idx === state.activeServerIndex ? 'active' : ''}" data-sidx="${idx}">
      <span class="server-status-dot"></span>
      <span>${s.server_name || 'Server ' + (idx + 1)}</span>
      <span class="server-badge-hd">HD</span>
    </button>
  `).join('');

  chipsContainer.querySelectorAll('.server-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      state.activeServerIndex = Number(btn.dataset.sidx);
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

  if (activePlayerInstance) {
    try { activePlayerInstance.destroy(); } catch (e) {}
    activePlayerInstance = null;
  }

  let list = state.currentStreams[state.activeAudioType] || [];
  if (!list.length) {
    const fallbackType = state.activeAudioType === 'sub' ? 'dub' : 'sub';
    if ((state.currentStreams[fallbackType] || []).length > 0) {
      state.activeAudioType = fallbackType;
      list = state.currentStreams[fallbackType];
      document.getElementById('btn-audio-sub')?.classList.toggle('active', fallbackType === 'sub');
      document.getElementById('btn-audio-dub')?.classList.toggle('active', fallbackType === 'dub');
      renderServerChips();
    }
  }

  const server = list[state.activeServerIndex];

  if (!server || !server.embed_url) {
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
      if ((state.currentStreams[targetAudio] || []).length > 0) {
        state.activeAudioType = targetAudio;
        state.activeServerIndex = 0;
        document.getElementById('btn-audio-sub')?.classList.toggle('active', targetAudio === 'sub');
        document.getElementById('btn-audio-dub')?.classList.toggle('active', targetAudio === 'dub');
        renderServerChips();
        loadStreamIntoPlayer();
        showToast(`Switched to ${targetAudio.toUpperCase()} audio`);
      } else {
        showToast(`No ${targetAudio.toUpperCase()} stream available for this episode`);
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
    }
  });

  document.getElementById('browse-next-page')?.addEventListener('click', () => {
    state.browsePage++;
    fetchBrowseResults();
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

  // Centered Inbox Message / Announcement Modal (Brave Browser)
  const inboxModal = document.getElementById('brave-inbox-modal');
  const btnCloseInbox = document.getElementById('btn-close-inbox-modal');
  const btnContinue = document.getElementById('btn-continue-site');
  const btnGetBrave = document.getElementById('btn-get-brave');
  const cbDontShow = document.getElementById('brave-dont-show-again');
  const headerInboxBtn = document.getElementById('header-inbox-btn');
  const inboxBadge = document.getElementById('inbox-badge');

  function openBraveModal() {
    if (!inboxModal) return;
    inboxModal.classList.add('active');
    inboxModal.setAttribute('aria-hidden', 'false');
    document.body.style.overflow = 'hidden';
    refreshIcons();
  }

  function closeBraveModal() {
    if (!inboxModal) return;
    inboxModal.classList.remove('active');
    inboxModal.setAttribute('aria-hidden', 'true');
    document.body.style.overflow = '';
    if (cbDontShow?.checked) {
      localStorage.setItem('kuro_brave_notice_dismissed', 'true');
      if (inboxBadge) inboxBadge.style.display = 'none';
    }
  }

  headerInboxBtn?.addEventListener('click', () => {
    openBraveModal();
  });

  btnCloseInbox?.addEventListener('click', closeBraveModal);
  btnContinue?.addEventListener('click', closeBraveModal);
  btnGetBrave?.addEventListener('click', () => {
    // When user clicks download, close modal after short delay
    setTimeout(closeBraveModal, 600);
  });

  inboxModal?.addEventListener('click', (e) => {
    if (e.target === inboxModal) {
      closeBraveModal();
    }
  });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && inboxModal?.classList.contains('active')) {
      closeBraveModal();
    }
  });

  // Check dismissal state
  const isDismissed = localStorage.getItem('kuro_brave_notice_dismissed') === 'true';
  if (inboxBadge) {
    inboxBadge.style.display = isDismissed ? 'none' : 'block';
  }

  // When user opens website, show inbox message in center
  if (!isDismissed) {
    setTimeout(() => {
      openBraveModal();
    }, 450);
  }

  // Init App
  updateWatchlistBadge();
  initHome();
  refreshIcons();
});
