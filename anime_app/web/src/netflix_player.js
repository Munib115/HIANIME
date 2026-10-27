/**
 * KuroAnime Netflix-Style Glassmorphic Video Player Engine
 * ========================================================
 * - 100% Ad-Free Direct HLS Streaming (via hls.js & FastAPI Resolver/Proxy)
 * - Multi-Quality Selector (Auto, 1080p FHD, 720p HD, 360p SD)
 * - Custom Multi-Language WebVTT Subtitles Renderer with Size Controls
 * - Netflix Skip Intro / Skip Outro & Auto-Next Episode Countdown
 * - Built-In Multi-Quality Episode Downloader with Live Progress Bar
 * - Mobile-First Touch Gestures (Double-tap seek, Auto-hide controls, Landscape lock)
 */
import Hls from 'hls.js';

function formatTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '0:00';
  const total = Math.floor(sec);
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hrs > 0) {
    return `${hrs}:${String(mins).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }
  return `${mins}:${String(secs).padStart(2, '0')}`;
}

function parseVtt(vttText) {
  if (!vttText) return [];
  const cues = [];
  const blocks = vttText.replace(/\r\n/g, '\n').split(/\n\n+/);
  const timeRegex = /(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{2}):(\d{2})[.,](\d{3})/;

  for (const block of blocks) {
    const lines = block.trim().split('\n');
    let timeLineIdx = -1;
    for (let i = 0; i < lines.length; i++) {
      if (timeRegex.test(lines[i])) {
        timeLineIdx = i;
        break;
      }
    }
    if (timeLineIdx === -1) continue;
    const m = lines[timeLineIdx].match(timeRegex);
    if (!m) continue;

    const start =
      (parseInt(m[1] || '0', 10) * 3600) +
      (parseInt(m[2], 10) * 60) +
      parseInt(m[3], 10) +
      parseInt(m[4], 10) / 1000;
    const end =
      (parseInt(m[5] || '0', 10) * 3600) +
      (parseInt(m[6], 10) * 60) +
      parseInt(m[7], 10) +
      parseInt(m[8], 10) / 1000;

    const text = lines
      .slice(timeLineIdx + 1)
      .join('\n')
      .replace(/<[^>]+>/g, '')
      .trim();

    if (text) {
      cues.push({ start, end, text });
    }
  }
  return cues;
}

export class NetflixPlayer {
  constructor(options) {
    this.container = options.container;
    this.apiBase = options.apiBase || '/api';
    this.embedUrl = options.embedUrl;
    this.animeTitle = options.animeTitle || 'Anime';
    this.animeId = options.animeId;
    this.episodeNumber = options.episodeNumber || 1;
    this.episodeTitle = options.episodeTitle || '';
    this.episodeId = options.episodeId || '';
    this.audioType = options.audioType || 'sub';
    this.hasSub = options.hasSub !== false;
    this.hasDub = Boolean(options.hasDub);
    this.hasNextEp = Boolean(options.hasNextEp);
    this.hasPrevEp = Boolean(options.hasPrevEp);
    this.poster = options.poster || '';
    this.onNextEpisode = options.onNextEpisode || (() => {});
    this.onPrevEpisode = options.onPrevEpisode || (() => {});
    this.onSwitchAudio = options.onSwitchAudio || (() => {});
    this.onToast = options.onToast || (() => {});

    this.hls = null;
    this.video = null;
    this.streamData = null;
    this.cues = [];
    this.activeSubIndex = -1; // -1 = Off
    this.subSize = localStorage.getItem('kuro_sub_size') || 'md'; // sm | md | lg | xl
    this.autoSkip = localStorage.getItem('kuro_auto_skip') === 'true';
    this.playbackSpeed = 1;
    this.activeQuality = 'auto'; // 'auto' or height number
    this.controlsTimeout = null;
    this.isDestroyed = false;
    this.downloadAbortController = null;
    this.isDownloading = false;
    this.lastTapTime = 0;
    this.lastTapSide = null;
    this.resumeKey = `kuro_pos_${this.animeId}_${this.episodeId}`;

    this.mount();
  }

  async mount() {
    if (!this.container) return;
    this.renderShell();
    this.bindEvents();
    await this.resolveAndPlay();
  }

  renderShell() {
    const epLabel = `E${this.episodeNumber}${this.episodeTitle ? ` • ${this.episodeTitle}` : ''}`;
    this.container.innerHTML = `
      <div class="nf-player-root controls-visible" id="nf-player-root" tabindex="0">
        <!-- Ambient Video Canvas -->
        <video
          id="nf-video-el"
          class="nf-video-element"
          playsinline
          webkit-playsinline
          preload="auto"
          crossorigin="anonymous"
          ${this.poster ? `poster="${this.poster}"` : ''}
        ></video>

        <!-- Custom Subtitle Overlay -->
        <div class="nf-subtitle-layer sub-size-${this.subSize}" id="nf-subtitle-layer"></div>

        <!-- Double-Tap Seek Ripple Feedback -->
        <div class="nf-seek-ripple left" id="nf-ripple-left">
          <div class="nf-ripple-icon">⏪ 10s</div>
        </div>
        <div class="nf-seek-ripple right" id="nf-ripple-right">
          <div class="nf-ripple-icon">10s ⏩</div>
        </div>

        <!-- Center Loading / Buffering Glass Spinner -->
        <div class="nf-loader-overlay active" id="nf-loader-overlay">
          <div class="nf-spinner-ring"></div>
          <div class="nf-loader-text" id="nf-loader-text">Extracting Ad-Free 1080p Stream...</div>
        </div>

        <!-- Top Glass Bar -->
        <div class="nf-top-bar" id="nf-top-bar">
          <div class="nf-top-left">
            <span class="nf-badge-adfree">
              <span class="nf-live-dot"></span> AD-FREE
            </span>
            <div class="nf-top-titles">
              <span class="nf-top-anime">${this.animeTitle}</span>
              <span class="nf-top-ep">${epLabel}</span>
            </div>
          </div>
          <div class="nf-top-right">
            <div class="nf-audio-pill-group">
              <button type="button" class="nf-audio-pill ${this.audioType === 'sub' ? 'active' : ''}" data-audio="sub" ${!this.hasSub ? 'disabled' : ''}>SUB</button>
              <button type="button" class="nf-audio-pill ${this.audioType === 'dub' ? 'active' : ''}" data-audio="dub" ${!this.hasDub ? 'disabled' : ''}>DUB</button>
            </div>
            <button type="button" class="nf-glass-pill-btn" id="nf-top-download-btn" title="Download Episode">
              <svg viewBox="0 0 24 24" width="15" height="15" stroke="currentColor" stroke-width="2.2" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              <span>Download</span>
            </button>
            <button type="button" class="nf-glass-pill-btn nf-embed-fallback-btn" id="nf-fallback-embed-btn" title="Switch to Embed Server">
              <span>Embed</span>
            </button>
          </div>
        </div>

        <!-- Center Glass Playback Controls -->
        <div class="nf-center-controls" id="nf-center-controls">
          <button type="button" class="nf-center-btn secondary" id="nf-center-rw" aria-label="Rewind 10 seconds">
            <svg viewBox="0 0 24 24" width="24" height="24" stroke="currentColor" stroke-width="2" fill="none">
              <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/>
              <path d="M3 3v5h5"/>
            </svg>
            <span class="nf-center-sec">10</span>
          </button>

          <button type="button" class="nf-center-btn primary" id="nf-center-play" aria-label="Play or Pause">
            <svg class="icon-play" viewBox="0 0 24 24" width="32" height="32" fill="currentColor"><polygon points="6 3 20 12 6 21 6 3"/></svg>
            <svg class="icon-pause" viewBox="0 0 24 24" width="32" height="32" fill="currentColor" style="display:none"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
          </button>

          <button type="button" class="nf-center-btn secondary" id="nf-center-ff" aria-label="Forward 10 seconds">
            <svg viewBox="0 0 24 24" width="24" height="24" stroke="currentColor" stroke-width="2" fill="none">
              <path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/>
              <path d="M21 3v5h-5"/>
            </svg>
            <span class="nf-center-sec">10</span>
          </button>
        </div>

        <!-- Netflix Skip Intro / Skip Outro Floating Button -->
        <button type="button" class="nf-skip-btn" id="nf-skip-btn" style="display:none">
          <span id="nf-skip-label">Skip Intro</span>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2.5"/></svg>
        </button>

        <!-- Next Episode Floating Prompt -->
        <button type="button" class="nf-next-ep-prompt" id="nf-next-ep-prompt" style="display:none">
          <span>Next Episode</span>
          <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2.5"/></svg>
        </button>

        <!-- Popover Menu: Subtitles (CC) -->
        <div class="nf-popover" id="nf-popover-subs">
          <div class="nf-popover-header">
            <span>Subtitles / CC</span>
            <div class="nf-sub-size-pills">
              <button type="button" class="nf-size-btn ${this.subSize === 'sm' ? 'active' : ''}" data-size="sm">S</button>
              <button type="button" class="nf-size-btn ${this.subSize === 'md' ? 'active' : ''}" data-size="md">M</button>
              <button type="button" class="nf-size-btn ${this.subSize === 'lg' ? 'active' : ''}" data-size="lg">L</button>
              <button type="button" class="nf-size-btn ${this.subSize === 'xl' ? 'active' : ''}" data-size="xl">XL</button>
            </div>
          </div>
          <div class="nf-popover-list" id="nf-subs-list">
            <button type="button" class="nf-popover-item active" data-sub-idx="-1">Off</button>
          </div>
        </div>

        <!-- Popover Menu: Quality & Speed Settings -->
        <div class="nf-popover" id="nf-popover-settings">
          <div class="nf-popover-header">
            <span>Video Quality</span>
            <label class="nf-autoskip-toggle">
              <input type="checkbox" id="nf-autoskip-cb" ${this.autoSkip ? 'checked' : ''} />
              <span>Auto-Skip Intro</span>
            </label>
          </div>
          <div class="nf-popover-list" id="nf-quality-list">
            <button type="button" class="nf-popover-item active" data-quality="auto">Auto (1080p HD)</button>
          </div>
          <div class="nf-popover-header" style="margin-top:8px">
            <span>Playback Speed</span>
          </div>
          <div class="nf-speed-pills" id="nf-speed-pills">
            ${[0.5, 0.75, 1, 1.25, 1.5, 2].map(s => `
              <button type="button" class="nf-speed-btn ${s === 1 ? 'active' : ''}" data-speed="${s}">${s}x</button>
            `).join('')}
          </div>
        </div>

        <!-- In-Player Episode Download Glass Modal -->
        <div class="nf-download-modal" id="nf-download-modal">
          <div class="nf-download-card">
            <div class="nf-download-header">
              <div>
                <div class="nf-download-title">Download Episode ${this.episodeNumber}</div>
                <div class="nf-download-sub">${this.animeTitle} • ${this.audioType.toUpperCase()}</div>
              </div>
              <button type="button" class="nf-modal-close" id="nf-download-close" aria-label="Close download menu">✕</button>
            </div>

            <div class="nf-download-body" id="nf-download-options">
              <!-- Populated once stream resolves -->
            </div>

            <div class="nf-download-progress-box" id="nf-download-progress-box" style="display:none">
              <div class="nf-dl-status-row">
                <span id="nf-dl-status-label">Downloading 1080p FHD...</span>
                <span id="nf-dl-status-pct">0%</span>
              </div>
              <div class="nf-dl-bar-track">
                <div class="nf-dl-bar-fill" id="nf-dl-bar-fill" style="width:0%"></div>
              </div>
              <div class="nf-dl-meta-row">
                <span id="nf-dl-meta-bytes">0.0 MB downloaded</span>
                <button type="button" class="nf-dl-cancel-btn" id="nf-dl-cancel-btn">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Bottom Glass Control Bar -->
        <div class="nf-bottom-bar" id="nf-bottom-bar">
          <!-- Scrubber Timeline -->
          <div class="nf-timeline-container" id="nf-timeline-container">
            <div class="nf-timeline-tooltip" id="nf-timeline-tooltip">0:00</div>
            <div class="nf-timeline-track">
              <div class="nf-timeline-buffered" id="nf-timeline-buffered"></div>
              <div class="nf-timeline-marker" id="nf-timeline-intro-marker" style="display:none"></div>
              <div class="nf-timeline-progress" id="nf-timeline-progress">
                <div class="nf-timeline-thumb"></div>
              </div>
            </div>
          </div>

          <!-- Controls Row -->
          <div class="nf-controls-row">
            <div class="nf-controls-left">
              <button type="button" class="nf-ctrl-btn" id="nf-btn-play" aria-label="Play/Pause">
                <svg class="icon-play" viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
                <svg class="icon-pause" viewBox="0 0 24 24" width="20" height="20" fill="currentColor" style="display:none"><rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/></svg>
              </button>

              <button type="button" class="nf-ctrl-btn nf-hide-xs" id="nf-btn-rw" title="Rewind 10s">
                <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
              </button>

              <button type="button" class="nf-ctrl-btn nf-hide-xs" id="nf-btn-ff" title="Forward 10s">
                <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></svg>
              </button>

              <div class="nf-volume-group">
                <button type="button" class="nf-ctrl-btn" id="nf-btn-mute" aria-label="Mute/Unmute">
                  <svg class="icon-vol-high" viewBox="0 0 24 24" width="19" height="19" stroke="currentColor" stroke-width="2" fill="none"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
                  <svg class="icon-vol-mute" viewBox="0 0 24 24" width="19" height="19" stroke="currentColor" stroke-width="2" fill="none" style="display:none"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="23" y1="9" x2="17" y2="15"/><line x1="17" y1="9" x2="23" y2="15"/></svg>
                </button>
                <input type="range" class="nf-volume-slider nf-hide-mobile" id="nf-volume-slider" min="0" max="1" step="0.05" value="1" aria-label="Volume" />
              </div>

              <div class="nf-time-display">
                <span id="nf-time-cur">0:00</span>
                <span class="nf-time-sep">/</span>
                <span id="nf-time-dur">0:00</span>
              </div>
            </div>

            <div class="nf-controls-right">
              <button type="button" class="nf-ctrl-btn nf-badge-trigger" id="nf-btn-subs" title="Subtitles (CC)">
                <svg viewBox="0 0 24 24" width="19" height="19" stroke="currentColor" stroke-width="2" fill="none"><rect x="2" y="4" width="20" height="16" rx="2"/><path d="M7 15h4M15 15h2M7 11h2M13 11h4"/></svg>
                <span class="nf-mini-tag" id="nf-cc-tag">CC</span>
              </button>

              <button type="button" class="nf-ctrl-btn nf-badge-trigger" id="nf-btn-settings" title="Quality & Speed">
                <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
                <span class="nf-mini-tag" id="nf-quality-tag">HD</span>
              </button>

              <button type="button" class="nf-ctrl-btn" id="nf-btn-download" title="Download Episode">
                <svg viewBox="0 0 24 24" width="19" height="19" stroke="currentColor" stroke-width="2" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              </button>

              ${this.hasNextEp ? `
                <button type="button" class="nf-ctrl-btn" id="nf-btn-next" title="Next Episode">
                  <svg viewBox="0 0 24 24" width="19" height="19" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2.2"/></svg>
                </button>
              ` : ''}

              <button type="button" class="nf-ctrl-btn nf-hide-xs" id="nf-btn-pip" title="Picture-in-Picture">
                <svg viewBox="0 0 24 24" width="18" height="18" stroke="currentColor" stroke-width="2" fill="none"><rect x="2" y="4" width="20" height="15" rx="2"/><rect x="12" y="11" width="8" height="6" rx="1"/></svg>
              </button>

              <button type="button" class="nf-ctrl-btn" id="nf-btn-fs" aria-label="Fullscreen">
                <svg class="icon-fs-enter" viewBox="0 0 24 24" width="19" height="19" stroke="currentColor" stroke-width="2" fill="none"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>
                <svg class="icon-fs-exit" viewBox="0 0 24 24" width="19" height="19" stroke="currentColor" stroke-width="2" fill="none" style="display:none"><path d="M8 3v3a2 2 0 0 1-2 2H3m18 0h-3a2 2 0 0 1-2-2V3m0 18v-3a2 2 0 0 1 2-2h3M3 16h3a2 2 0 0 1 2 2v3"/></svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    `;

    this.root = this.container.querySelector('#nf-player-root');
    this.video = this.container.querySelector('#nf-video-el');
  }

  bindEvents() {
    if (!this.root || !this.video) return;

    const showControls = () => {
      if (!this.root) return;
      this.root.classList.add('controls-visible');
      clearTimeout(this.controlsTimeout);
      if (!this.video.paused) {
        this.controlsTimeout = setTimeout(() => {
          if (!this.isPopoverOpen() && !this.video.paused) {
            this.root?.classList.remove('controls-visible');
          }
        }, 3200);
      }
    };

    this.root.addEventListener('mousemove', showControls, { passive: true });
    this.root.addEventListener('touchstart', (e) => {
      // Handle double-tap left/right seek on video stage
      if (e.target === this.video || e.target.id === 'nf-center-controls' || e.target.id === 'nf-subtitle-layer') {
        const now = Date.now();
        const rect = this.root.getBoundingClientRect();
        const touchX = e.touches[0].clientX - rect.left;
        const side = touchX < rect.width * 0.38 ? 'left' : (touchX > rect.width * 0.62 ? 'right' : 'center');

        if (side !== 'center' && now - this.lastTapTime < 300 && this.lastTapSide === side) {
          e.preventDefault();
          if (side === 'left') {
            this.seekRelative(-10);
            this.triggerRipple('left');
          } else {
            this.seekRelative(10);
            this.triggerRipple('right');
          }
          this.lastTapTime = 0;
          return;
        }
        this.lastTapTime = now;
        this.lastTapSide = side;
      }
      showControls();
    }, { passive: false });

    // Click on video background toggles play/pause on desktop or controls on mobile
    this.video.addEventListener('click', () => {
      if (this.isPopoverOpen()) {
        this.closeAllPopovers();
        return;
      }
      if (window.matchMedia('(pointer: coarse)').matches) {
        this.root.classList.toggle('controls-visible');
      } else {
        this.togglePlay();
      }
    });

    // Play / Pause buttons
    const playBtns = [
      this.root.querySelector('#nf-center-play'),
      this.root.querySelector('#nf-btn-play'),
    ];
    playBtns.forEach(btn => btn?.addEventListener('click', (e) => {
      e.stopPropagation();
      this.togglePlay();
    }));

    // Rewind / Forward 10s
    [this.root.querySelector('#nf-center-rw'), this.root.querySelector('#nf-btn-rw')].forEach(btn => {
      btn?.addEventListener('click', (e) => {
        e.stopPropagation();
        this.seekRelative(-10);
        this.triggerRipple('left');
      });
    });

    [this.root.querySelector('#nf-center-ff'), this.root.querySelector('#nf-btn-ff')].forEach(btn => {
      btn?.addEventListener('click', (e) => {
        e.stopPropagation();
        this.seekRelative(10);
        this.triggerRipple('right');
      });
    });

    // Volume & Mute
    const muteBtn = this.root.querySelector('#nf-btn-mute');
    const volSlider = this.root.querySelector('#nf-volume-slider');
    muteBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      this.video.muted = !this.video.muted;
      this.updateVolumeUI();
    });
    volSlider?.addEventListener('input', (e) => {
      e.stopPropagation();
      const val = parseFloat(e.target.value);
      this.video.volume = val;
      this.video.muted = val === 0;
      this.updateVolumeUI();
    });

    // Video Media Events
    this.video.addEventListener('play', () => {
      this.updatePlayUI();
      showControls();
    });
    this.video.addEventListener('pause', () => {
      this.updatePlayUI();
      this.root?.classList.add('controls-visible');
    });
    this.video.addEventListener('waiting', () => {
      this.setLoader(true, 'Buffering stream...');
    });
    this.video.addEventListener('playing', () => {
      this.setLoader(false);
    });
    this.video.addEventListener('canplay', () => {
      this.setLoader(false);
    });
    this.video.addEventListener('loadedmetadata', () => {
      this.updateTimeUI();
      this.restoreSavedPosition();
      this.renderIntroMarker();
    });
    this.video.addEventListener('timeupdate', () => {
      this.updateTimeUI();
      this.updateSubtitles();
      this.updateSkipButtons();
      this.saveCurrentPosition();
    });
    this.video.addEventListener('progress', () => {
      this.updateBufferedUI();
    });
    this.video.addEventListener('ended', () => {
      if (this.hasNextEp) {
        this.onToast('Playing Next Episode...');
        this.onNextEpisode();
      }
    });

    // Timeline Scrubber (Mouse & Touch)
    const timeline = this.root.querySelector('#nf-timeline-container');
    const tooltip = this.root.querySelector('#nf-timeline-tooltip');
    let isScrubbing = false;

    const scrubToEvent = (clientX, commit = false) => {
      if (!timeline || !this.video.duration) return;
      const rect = timeline.getBoundingClientRect();
      const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const targetTime = pct * this.video.duration;
      if (tooltip) {
        tooltip.textContent = formatTime(targetTime);
        tooltip.style.left = `${pct * 100}%`;
      }
      if (commit) {
        this.video.currentTime = targetTime;
        this.updateTimeUI();
      }
    };

    timeline?.addEventListener('mousemove', (e) => {
      scrubToEvent(e.clientX, isScrubbing);
    });
    timeline?.addEventListener('mousedown', (e) => {
      isScrubbing = true;
      scrubToEvent(e.clientX, true);
    });
    window.addEventListener('mouseup', () => {
      isScrubbing = false;
    });
    timeline?.addEventListener('touchstart', (e) => {
      e.stopPropagation();
      isScrubbing = true;
      if (e.touches[0]) scrubToEvent(e.touches[0].clientX, true);
    }, { passive: true });
    timeline?.addEventListener('touchmove', (e) => {
      e.stopPropagation();
      if (e.touches[0]) scrubToEvent(e.touches[0].clientX, true);
    }, { passive: true });
    timeline?.addEventListener('touchend', () => {
      isScrubbing = false;
    });

    // Popover Toggles (CC & Settings)
    const subsBtn = this.root.querySelector('#nf-btn-subs');
    const settingsBtn = this.root.querySelector('#nf-btn-settings');
    const subsPop = this.root.querySelector('#nf-popover-subs');
    const settingsPop = this.root.querySelector('#nf-popover-settings');

    subsBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = subsPop?.classList.contains('open');
      this.closeAllPopovers();
      if (!wasOpen) subsPop?.classList.add('open');
    });

    settingsBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = settingsPop?.classList.contains('open');
      this.closeAllPopovers();
      if (!wasOpen) settingsPop?.classList.add('open');
    });

    // Subtitle Size Buttons
    this.root.querySelectorAll('.nf-size-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.subSize = btn.dataset.size;
        localStorage.setItem('kuro_sub_size', this.subSize);
        this.root.querySelectorAll('.nf-size-btn').forEach(b => b.classList.toggle('active', b === btn));
        const subLayer = this.root.querySelector('#nf-subtitle-layer');
        if (subLayer) {
          subLayer.className = `nf-subtitle-layer sub-size-${this.subSize}`;
        }
      });
    });

    // Playback Speed Buttons
    this.root.querySelectorAll('.nf-speed-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const spd = parseFloat(btn.dataset.speed);
        this.playbackSpeed = spd;
        this.video.playbackRate = spd;
        this.root.querySelectorAll('.nf-speed-btn').forEach(b => b.classList.toggle('active', b === btn));
        this.onToast(`Playback speed: ${spd}x`);
      });
    });

    // Auto-Skip Intro Checkbox
    const autoSkipCb = this.root.querySelector('#nf-autoskip-cb');
    autoSkipCb?.addEventListener('change', (e) => {
      this.autoSkip = e.target.checked;
      localStorage.setItem('kuro_auto_skip', String(this.autoSkip));
      this.onToast(this.autoSkip ? 'Auto-Skip Intro enabled' : 'Auto-Skip Intro disabled');
    });

    // Skip Intro / Outro Button
    const skipBtn = this.root.querySelector('#nf-skip-btn');
    skipBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      const target = parseFloat(skipBtn.dataset.target || '0');
      if (target > 0) {
        this.video.currentTime = target;
        skipBtn.style.display = 'none';
      }
    });

    // Next Episode Buttons
    [this.root.querySelector('#nf-btn-next'), this.root.querySelector('#nf-next-ep-prompt')].forEach(btn => {
      btn?.addEventListener('click', (e) => {
        e.stopPropagation();
        this.onNextEpisode();
      });
    });

    // Audio Quick Switch Pills inside Player
    this.root.querySelectorAll('.nf-audio-pill').forEach(pill => {
      pill.addEventListener('click', (e) => {
        e.stopPropagation();
        const targetAudio = pill.dataset.audio;
        if (targetAudio && targetAudio !== this.audioType) {
          this.onSwitchAudio(targetAudio);
        }
      });
    });

    // Download Modal Triggers
    const dlModal = this.root.querySelector('#nf-download-modal');
    [this.root.querySelector('#nf-top-download-btn'), this.root.querySelector('#nf-btn-download')].forEach(btn => {
      btn?.addEventListener('click', (e) => {
        e.stopPropagation();
        this.closeAllPopovers();
        dlModal?.classList.add('open');
      });
    });
    this.root.querySelector('#nf-download-close')?.addEventListener('click', (e) => {
      e.stopPropagation();
      dlModal?.classList.remove('open');
    });
    dlModal?.addEventListener('click', (e) => {
      if (e.target === dlModal) dlModal.classList.remove('open');
    });
    this.root.querySelector('#nf-dl-cancel-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      if (this.downloadAbortController) {
        this.downloadAbortController.abort();
      }
    });

    // Fallback to Embed Server Button
    this.root.querySelector('#nf-fallback-embed-btn')?.addEventListener('click', (e) => {
      e.stopPropagation();
      this.switchToIframeFallback();
    });

    // Picture-in-Picture
    this.root.querySelector('#nf-btn-pip')?.addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        if (document.pictureInPictureElement) {
          await document.exitPictureInPicture();
        } else if (this.video.requestPictureInPicture) {
          await this.video.requestPictureInPicture();
        }
      } catch (err) {
        this.onToast('Picture-in-Picture not supported on this device');
      }
    });

    // Fullscreen
    this.root.querySelector('#nf-btn-fs')?.addEventListener('click', (e) => {
      e.stopPropagation();
      this.toggleFullscreen();
    });

    document.addEventListener('fullscreenchange', () => this.updateFullscreenUI());
    document.addEventListener('webkitfullscreenchange', () => this.updateFullscreenUI());

    // Keyboard Shortcuts
    this.keyHandler = (e) => {
      if (this.isDestroyed) return;
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
      if (!document.getElementById('view-watch')?.classList.contains('active')) return;

      if (e.code === 'Space' || e.key === 'k') {
        e.preventDefault();
        this.togglePlay();
      } else if (e.key === 'ArrowRight' || e.key === 'l') {
        e.preventDefault();
        this.seekRelative(10);
        this.triggerRipple('right');
      } else if (e.key === 'ArrowLeft' || e.key === 'j') {
        e.preventDefault();
        this.seekRelative(-10);
        this.triggerRipple('left');
      } else if (e.key === 'f' || e.key === 'F') {
        e.preventDefault();
        this.toggleFullscreen();
      } else if (e.key === 'm' || e.key === 'M') {
        e.preventDefault();
        this.video.muted = !this.video.muted;
        this.updateVolumeUI();
      }
    };
    window.addEventListener('keydown', this.keyHandler);
  }

  isPopoverOpen() {
    if (!this.root) return false;
    return (
      this.root.querySelector('#nf-popover-subs')?.classList.contains('open') ||
      this.root.querySelector('#nf-popover-settings')?.classList.contains('open') ||
      this.root.querySelector('#nf-download-modal')?.classList.contains('open')
    );
  }

  closeAllPopovers() {
    if (!this.root) return;
    this.root.querySelector('#nf-popover-subs')?.classList.remove('open');
    this.root.querySelector('#nf-popover-settings')?.classList.remove('open');
  }

  async resolveAndPlay() {
    this.setLoader(true, 'Extracting Ad-Free 1080p Stream...');
    try {
      const res = await fetch(`${this.apiBase}/player/resolve?url=${encodeURIComponent(this.embedUrl)}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!data || !data.ok || !data.master_url) {
        throw new Error('Invalid stream payload');
      }

      if (this.isDestroyed) return;
      this.streamData = data;

      this.populateQualityMenu(data.qualities || []);
      this.populateSubtitlesMenu(data.subtitles || []);
      this.populateDownloadOptions(data.qualities || []);

      const masterSrc = data.master_url.startsWith('http')
        ? data.master_url
        : `${this.apiBase}${data.master_url.replace(/^\/api/, '')}`;

      this.attachHlsSource(masterSrc);
    } catch (err) {
      console.warn('[NetflixPlayer] Direct stream resolve failed, switching to Clean Embed fallback:', err);
      this.switchToIframeFallback();
    }
  }

  attachHlsSource(playlistUrl, seekTime = null) {
    if (this.isDestroyed || !this.video) return;
    this.setLoader(true, 'Loading video stream...');

    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }

    if (Hls.isSupported()) {
      const hls = new Hls({
        maxBufferLength: 30,
        maxMaxBufferLength: 60,
        backBufferLength: 30,
        enableWorker: true,
      });
      this.hls = hls;
      hls.loadSource(playlistUrl);
      hls.attachMedia(this.video);

      hls.on(Hls.Events.MANIFEST_PARSED, () => {
        if (this.isDestroyed) return;
        if (seekTime !== null && seekTime > 0) {
          this.video.currentTime = seekTime;
        }
        this.video.playbackRate = this.playbackSpeed;
        this.video.play().catch(() => {
          this.setLoader(false);
          this.updatePlayUI();
        });
      });

      hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            hls.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          } else {
            this.switchToIframeFallback();
          }
        }
      });
    } else if (this.video.canPlayType('application/vnd.apple.mpegurl')) {
      this.video.src = playlistUrl;
      this.video.addEventListener('loadedmetadata', () => {
        if (seekTime !== null && seekTime > 0) {
          this.video.currentTime = seekTime;
        }
        this.video.play().catch(() => {
          this.setLoader(false);
        });
      }, { once: true });
    } else {
      this.switchToIframeFallback();
    }
  }

  populateQualityMenu(qualities) {
    const qList = this.root?.querySelector('#nf-quality-list');
    const qTag = this.root?.querySelector('#nf-quality-tag');
    if (!qList) return;

    const topLabel = qualities.length ? `${qualities[0].height}p` : 'HD';
    if (qTag) qTag.textContent = topLabel;

    let html = `<button type="button" class="nf-popover-item active" data-q-idx="auto">Auto (${topLabel})</button>`;
    qualities.forEach((q, idx) => {
      html += `<button type="button" class="nf-popover-item" data-q-idx="${idx}">${q.label}</button>`;
    });
    qList.innerHTML = html;

    qList.querySelectorAll('.nf-popover-item').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        qList.querySelectorAll('.nf-popover-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        const qIdx = btn.dataset.qIdx;
        const curTime = this.video?.currentTime || 0;

        if (qIdx === 'auto') {
          if (qTag) qTag.textContent = topLabel;
          const masterSrc = this.streamData.master_url.startsWith('http')
            ? this.streamData.master_url
            : `${this.apiBase}${this.streamData.master_url.replace(/^\/api/, '')}`;
          this.attachHlsSource(masterSrc, curTime);
          this.onToast(`Quality: Auto (${topLabel})`);
        } else {
          const chosen = qualities[Number(qIdx)];
          if (chosen) {
            if (qTag) qTag.textContent = `${chosen.height}p`;
            const qUrl = chosen.url.startsWith('http')
              ? chosen.url
              : `${this.apiBase}${chosen.url.replace(/^\/api/, '')}`;
            this.attachHlsSource(qUrl, curTime);
            this.onToast(`Quality: ${chosen.label}`);
          }
        }
        this.closeAllPopovers();
      });
    });
  }

  populateSubtitlesMenu(subtitles) {
    const sList = this.root?.querySelector('#nf-subs-list');
    const ccTag = this.root?.querySelector('#nf-cc-tag');
    if (!sList) return;

    // Prioritize English and popular languages at the top
    const sorted = [...subtitles].sort((a, b) => {
      const aEn = a.label.toLowerCase().includes('english') ? -2 : (a.default ? -1 : 0);
      const bEn = b.label.toLowerCase().includes('english') ? -2 : (b.default ? -1 : 0);
      return aEn - bEn || a.label.localeCompare(b.label);
    });
    this.sortedSubtitles = sorted;

    // Default to English for SUB audio, or Off for DUB audio
    let defaultIdx = -1;
    if (this.audioType === 'sub' && sorted.length > 0) {
      defaultIdx = sorted.findIndex(s => s.default) !== -1
        ? sorted.findIndex(s => s.default)
        : sorted.findIndex(s => s.label.toLowerCase().includes('english'));
      if (defaultIdx === -1) defaultIdx = 0;
    }

    let html = `<button type="button" class="nf-popover-item ${defaultIdx === -1 ? 'active' : ''}" data-sub-idx="-1">Off</button>`;
    sorted.forEach((s, idx) => {
      html += `<button type="button" class="nf-popover-item ${idx === defaultIdx ? 'active' : ''}" data-sub-idx="${idx}">${s.label}</button>`;
    });
    sList.innerHTML = html;

    if (defaultIdx >= 0) {
      this.loadSubtitleTrack(defaultIdx);
      if (ccTag) ccTag.textContent = sorted[defaultIdx].lang.toUpperCase().slice(0, 2);
    } else if (ccTag) {
      ccTag.textContent = 'OFF';
    }

    sList.querySelectorAll('.nf-popover-item').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        sList.querySelectorAll('.nf-popover-item').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        const idx = parseInt(btn.dataset.subIdx, 10);
        if (idx === -1) {
          this.activeSubIndex = -1;
          this.cues = [];
          const subLayer = this.root?.querySelector('#nf-subtitle-layer');
          if (subLayer) subLayer.innerHTML = '';
          if (ccTag) ccTag.textContent = 'OFF';
          this.onToast('Subtitles: Off');
        } else {
          this.loadSubtitleTrack(idx);
          const chosen = this.sortedSubtitles[idx];
          if (ccTag && chosen) ccTag.textContent = chosen.lang.toUpperCase().slice(0, 2);
          if (chosen) this.onToast(`Subtitles: ${chosen.label}`);
        }
        this.closeAllPopovers();
      });
    });
  }

  async loadSubtitleTrack(idx) {
    this.activeSubIndex = idx;
    const sub = this.sortedSubtitles?.[idx];
    if (!sub) return;

    try {
      const vttUrl = sub.url.startsWith('http')
        ? sub.url
        : `${this.apiBase}${sub.url.replace(/^\/api/, '')}`;
      const res = await fetch(vttUrl);
      if (!res.ok) return;
      const text = await res.text();
      if (this.activeSubIndex === idx) {
        this.cues = parseVtt(text);
        this.updateSubtitles();
      }
    } catch (err) {
      console.warn('[NetflixPlayer] Subtitle load failed:', err);
    }
  }

  updateSubtitles() {
    const subLayer = this.root?.querySelector('#nf-subtitle-layer');
    if (!subLayer) return;
    if (this.activeSubIndex === -1 || !this.cues.length || !this.video) {
      subLayer.innerHTML = '';
      return;
    }
    const t = this.video.currentTime;
    const activeCues = this.cues.filter(c => t >= c.start && t <= c.end);
    if (!activeCues.length) {
      subLayer.innerHTML = '';
      return;
    }
    subLayer.innerHTML = activeCues
      .map(c => `<span class="nf-sub-box">${c.text.replace(/\n/g, '<br/>')}</span>`)
      .join('');
  }

  populateDownloadOptions(qualities) {
    const box = this.root?.querySelector('#nf-download-options');
    if (!box) return;

    const list = qualities.length
      ? qualities
      : [{ height: 1080, label: '1080p FHD', raw_url: this.streamData?.raw_master_url }];

    const cleanTitle = this.animeTitle.replace(/[^a-zA-Z0-9]+/g, '_').replace(/^_|_$/g, '');

    box.innerHTML = `
      <div class="nf-dl-list">
        ${list.map((q, idx) => {
          const fileName = `${cleanTitle}_Ep${this.episodeNumber}_${this.audioType.toUpperCase()}_${q.height || 'HD'}p.ts`;
          const directHref = `${this.apiBase}/player/download?url=${encodeURIComponent(q.raw_url)}&filename=${encodeURIComponent(fileName)}`;
          const estSize = q.height >= 1080 ? '~160-240 MB' : (q.height >= 720 ? '~90-140 MB' : '~45-75 MB');
          return `
            <div class="nf-dl-row">
              <div class="nf-dl-info">
                <span class="nf-dl-quality-badge">${q.label}</span>
                <span class="nf-dl-est">${estSize} • MPEG-TS Video</span>
              </div>
              <div class="nf-dl-actions">
                <button type="button" class="nf-dl-start-btn" data-q-idx="${idx}" data-filename="${fileName}">
                  <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2.4" fill="none"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
                  <span>Turbo Download</span>
                </button>
                <a href="${directHref}" download="${fileName}" class="nf-dl-direct-link" title="Direct Stream Download">
                  Direct
                </a>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;

    box.querySelectorAll('.nf-dl-start-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const q = list[Number(btn.dataset.qIdx)];
        if (q && q.raw_url) {
          this.startInPlayerDownload(q, btn.dataset.filename);
        }
      });
    });
  }

  async startInPlayerDownload(qualityObj, fileName) {
    if (this.isDownloading) {
      this.onToast('A download is already in progress');
      return;
    }

    const progBox = this.root?.querySelector('#nf-download-progress-box');
    const statusLabel = this.root?.querySelector('#nf-dl-status-label');
    const statusPct = this.root?.querySelector('#nf-dl-status-pct');
    const barFill = this.root?.querySelector('#nf-dl-bar-fill');
    const metaBytes = this.root?.querySelector('#nf-dl-meta-bytes');

    if (progBox) progBox.style.display = 'block';
    if (statusLabel) statusLabel.textContent = `Preparing ${qualityObj.label} segments...`;
    if (statusPct) statusPct.textContent = '0%';
    if (barFill) barFill.style.width = '0%';

    this.isDownloading = true;
    this.downloadAbortController = new AbortController();
    const signal = this.downloadAbortController.signal;

    try {
      const segRes = await fetch(
        `${this.apiBase}/player/playlist-segments?url=${encodeURIComponent(qualityObj.raw_url)}`,
        { signal }
      );
      if (!segRes.ok) throw new Error(`HTTP ${segRes.status}`);
      const segData = await segRes.json();
      const segments = segData.segments || [];
      if (!segments.length) throw new Error('No segments found in playlist');

      const total = segments.length;
      const chunks = new Array(total);
      let completed = 0;
      let totalBytes = 0;
      let cursor = 0;

      const worker = async () => {
        while (cursor < total) {
          if (signal.aborted) throw new Error('Download cancelled');
          const idx = cursor++;
          const segUrl = segments[idx].url.startsWith('http')
            ? segments[idx].url
            : `${this.apiBase}${segments[idx].url.replace(/^\/api/, '')}`;

          const r = await fetch(segUrl, { signal });
          if (!r.ok) throw new Error(`Segment ${idx} failed`);
          const buf = await r.arrayBuffer();
          chunks[idx] = new Uint8Array(buf);
          completed++;
          totalBytes += buf.byteLength;

          const pct = Math.round((completed / total) * 100);
          if (statusLabel) statusLabel.textContent = `Downloading ${qualityObj.label} (${completed}/${total})...`;
          if (statusPct) statusPct.textContent = `${pct}%`;
          if (barFill) barFill.style.width = `${pct}%`;
          if (metaBytes) metaBytes.textContent = `${(totalBytes / (1024 * 1024)).toFixed(1)} MB downloaded`;
        }
      };

      const concurrency = 6;
      await Promise.all(Array.from({ length: Math.min(concurrency, total) }, () => worker()));

      if (statusLabel) statusLabel.textContent = `Saving ${fileName}...`;
      const blob = new Blob(chunks, { type: 'video/mp2t' });
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl;
      a.download = fileName;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 15000);

      if (statusLabel) statusLabel.textContent = `Download Complete! (${(totalBytes / (1024 * 1024)).toFixed(1)} MB)`;
      this.onToast(`Downloaded ${fileName}`);
    } catch (err) {
      if (signal.aborted) {
        if (statusLabel) statusLabel.textContent = 'Download cancelled.';
        this.onToast('Download cancelled');
      } else {
        if (statusLabel) statusLabel.textContent = `Download failed: ${err.message}`;
        this.onToast('Download error — try Direct link');
      }
    } finally {
      this.isDownloading = false;
      this.downloadAbortController = null;
    }
  }

  renderIntroMarker() {
    const marker = this.root?.querySelector('#nf-timeline-intro-marker');
    const dur = this.video?.duration || 0;
    const intro = this.streamData?.skip?.intro;
    if (!marker || !dur || !intro || intro.end <= intro.start) {
      if (marker) marker.style.display = 'none';
      return;
    }
    const leftPct = Math.max(0, Math.min(100, (intro.start / dur) * 100));
    const widthPct = Math.max(0.5, Math.min(100 - leftPct, ((intro.end - intro.start) / dur) * 100));
    marker.style.left = `${leftPct}%`;
    marker.style.width = `${widthPct}%`;
    marker.style.display = 'block';
  }

  updateSkipButtons() {
    if (!this.video || !this.root) return;
    const t = this.video.currentTime;
    const dur = this.video.duration || 0;
    const skipBtn = this.root.querySelector('#nf-skip-btn');
    const skipLabel = this.root.querySelector('#nf-skip-label');
    const nextPrompt = this.root.querySelector('#nf-next-ep-prompt');

    const intro = this.streamData?.skip?.intro;
    const outro = this.streamData?.skip?.outro;

    if (intro && intro.end > intro.start && t >= intro.start && t < intro.end - 1) {
      if (this.autoSkip) {
        this.video.currentTime = intro.end;
        this.onToast('Skipped Intro');
        return;
      }
      if (skipBtn && skipLabel) {
        skipLabel.textContent = 'Skip Intro';
        skipBtn.dataset.target = String(intro.end);
        skipBtn.style.display = 'inline-flex';
      }
    } else if (outro && outro.end > outro.start && t >= outro.start && t < outro.end - 1) {
      if (skipBtn && skipLabel) {
        skipLabel.textContent = 'Skip Outro';
        skipBtn.dataset.target = String(outro.end);
        skipBtn.style.display = 'inline-flex';
      }
    } else if (skipBtn) {
      skipBtn.style.display = 'none';
    }

    if (nextPrompt) {
      if (this.hasNextEp && dur > 60 && dur - t <= 20 && dur - t > 0) {
        nextPrompt.style.display = 'inline-flex';
      } else {
        nextPrompt.style.display = 'none';
      }
    }
  }

  restoreSavedPosition() {
    try {
      const saved = parseFloat(localStorage.getItem(this.resumeKey) || '0');
      if (saved > 10 && this.video.duration && saved < this.video.duration - 25) {
        this.video.currentTime = saved;
      }
    } catch (e) {}
  }

  saveCurrentPosition() {
    if (!this.video || !this.video.duration) return;
    const now = Date.now();
    if (this._lastSave && now - this._lastSave < 4000) return;
    this._lastSave = now;
    try {
      if (this.video.currentTime > 5) {
        localStorage.setItem(this.resumeKey, String(Math.floor(this.video.currentTime)));
      }
    } catch (e) {}
  }

  togglePlay() {
    if (!this.video) return;
    if (this.video.paused) {
      this.video.play().catch(() => {});
    } else {
      this.video.pause();
    }
  }

  seekRelative(delta) {
    if (!this.video) return;
    const dur = this.video.duration || 0;
    const next = Math.max(0, Math.min(dur || Infinity, this.video.currentTime + delta));
    this.video.currentTime = next;
    this.updateTimeUI();
  }

  triggerRipple(side) {
    const el = this.root?.querySelector(side === 'left' ? '#nf-ripple-left' : '#nf-ripple-right');
    if (!el) return;
    el.classList.remove('active');
    void el.offsetWidth;
    el.classList.add('active');
    setTimeout(() => el.classList.remove('active'), 550);
  }

  updatePlayUI() {
    if (!this.root || !this.video) return;
    const paused = this.video.paused;
    this.root.classList.toggle('is-paused', paused);
    this.root.querySelectorAll('.icon-play').forEach(el => {
      el.style.display = paused ? 'block' : 'none';
    });
    this.root.querySelectorAll('.icon-pause').forEach(el => {
      el.style.display = paused ? 'none' : 'block';
    });
  }

  updateVolumeUI() {
    if (!this.root || !this.video) return;
    const muted = this.video.muted || this.video.volume === 0;
    const highIcon = this.root.querySelector('.icon-vol-high');
    const muteIcon = this.root.querySelector('.icon-vol-mute');
    const slider = this.root.querySelector('#nf-volume-slider');
    if (highIcon) highIcon.style.display = muted ? 'none' : 'block';
    if (muteIcon) muteIcon.style.display = muted ? 'block' : 'none';
    if (slider) slider.value = muted ? '0' : String(this.video.volume);
  }

  updateTimeUI() {
    if (!this.root || !this.video) return;
    const cur = this.video.currentTime || 0;
    const dur = this.video.duration || 0;
    const curEl = this.root.querySelector('#nf-time-cur');
    const durEl = this.root.querySelector('#nf-time-dur');
    const progEl = this.root.querySelector('#nf-timeline-progress');

    if (curEl) curEl.textContent = formatTime(cur);
    if (durEl) durEl.textContent = formatTime(dur);
    if (progEl && dur > 0) {
      const pct = Math.max(0, Math.min(100, (cur / dur) * 100));
      progEl.style.width = `${pct}%`;
    }
  }

  updateBufferedUI() {
    if (!this.root || !this.video || !this.video.duration) return;
    const buf = this.video.buffered;
    const bufEl = this.root.querySelector('#nf-timeline-buffered');
    if (bufEl && buf.length > 0) {
      const end = buf.end(buf.length - 1);
      const pct = Math.max(0, Math.min(100, (end / this.video.duration) * 100));
      bufEl.style.width = `${pct}%`;
    }
  }

  setLoader(visible, text = '') {
    const loader = this.root?.querySelector('#nf-loader-overlay');
    const label = this.root?.querySelector('#nf-loader-text');
    if (loader) loader.classList.toggle('active', Boolean(visible));
    if (label && text) label.textContent = text;
  }

  async toggleFullscreen() {
    if (!this.root) return;
    const isFs = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
    try {
      if (!isFs) {
        if (this.root.requestFullscreen) {
          await this.root.requestFullscreen();
        } else if (this.root.webkitRequestFullscreen) {
          this.root.webkitRequestFullscreen();
        } else if (this.video?.webkitEnterFullscreen) {
          this.video.webkitEnterFullscreen();
        }
        try {
          if (screen.orientation && screen.orientation.lock) {
            await screen.orientation.lock('landscape');
          }
        } catch (e) {}
      } else {
        if (document.exitFullscreen) {
          await document.exitFullscreen();
        } else if (document.webkitExitFullscreen) {
          document.webkitExitFullscreen();
        }
        try {
          if (screen.orientation && screen.orientation.unlock) {
            screen.orientation.unlock();
          }
        } catch (e) {}
      }
    } catch (e) {}
  }

  updateFullscreenUI() {
    if (!this.root) return;
    const isFs = Boolean(document.fullscreenElement || document.webkitFullscreenElement);
    const enterIcon = this.root.querySelector('.icon-fs-enter');
    const exitIcon = this.root.querySelector('.icon-fs-exit');
    if (enterIcon) enterIcon.style.display = isFs ? 'none' : 'block';
    if (exitIcon) exitIcon.style.display = isFs ? 'block' : 'none';
  }

  switchToIframeFallback() {
    if (this.isDestroyed || !this.container) return;
    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }
    const cleanSrc = `${this.apiBase}/clean-stream?url=${encodeURIComponent(this.embedUrl)}`;
    this.container.innerHTML = `
      <iframe
        id="main-stream-iframe"
        class="video-iframe"
        src="${cleanSrc}"
        referrerpolicy="no-referrer"
        allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen"
        allowfullscreen
        scrolling="no"
      ></iframe>
    `;
  }

  destroy() {
    this.isDestroyed = true;
    clearTimeout(this.controlsTimeout);
    if (this.downloadAbortController) {
      this.downloadAbortController.abort();
    }
    if (this.keyHandler) {
      window.removeEventListener('keydown', this.keyHandler);
    }
    if (this.hls) {
      this.hls.destroy();
      this.hls = null;
    }
    if (this.video) {
      try {
        this.video.pause();
        this.video.removeAttribute('src');
        this.video.load();
      } catch (e) {}
    }
  }
}

