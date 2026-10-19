/**
 * KuroAnime AdShield Engine v2
 * High-performance, zero-popup ad defuser for embedded anime streams.
 *
 * Objectives:
 * 1. Allow 100% uninterrupted HLS/MSE streaming without sandbox errors.
 * 2. Defuse pop-up / pop-under tabs created on video click.
 * 3. Provide direct postMessage bridge to stream player (ZokoAnime / MegaCloud)
 *    so playback, pause, and seek happen without triggering iframe ad-traps.
 * 4. Maintain pristine top-window navigation lock.
 */

const STORAGE_KEY = 'kuro_adshield_active';

class AdShieldEngine {
  constructor() {
    const saved = localStorage.getItem(STORAGE_KEY);
    this.enabled = saved !== null ? saved === 'true' : true; // Active by default
    this.originalOpen = window.open;
    this.blockedPopupsCount = 0;
    this.isStreamPlaying = false;
    this.playerState = {
      isPaused: true,
      currentTime: 0,
      duration: 0,
    };
    this.listeners = new Set();
    this.playerListeners = new Set();
  }

  init() {
    this.patchWindowOpen();
    this.patchAnchorClicks();
    this.patchFocusBlurGuard();
    this.patchTopNavigationGuard();
    this.initPlayerMessageListener();
  }

  isEnabled() {
    return this.enabled;
  }

  setEnabled(state) {
    this.enabled = Boolean(state);
    localStorage.setItem(STORAGE_KEY, String(this.enabled));
    this.notifyListeners();
  }

  toggle() {
    this.setEnabled(!this.enabled);
    return this.enabled;
  }

  onStateChange(cb) {
    this.listeners.add(cb);
    return () => this.listeners.delete(cb);
  }

  onPlayerState(cb) {
    this.playerListeners.add(cb);
    return () => this.playerListeners.delete(cb);
  }

  notifyListeners() {
    for (const cb of this.listeners) {
      try { cb(this.enabled, this.blockedPopupsCount); } catch (e) {}
    }
  }

  setStreamActive(active) {
    this.isStreamPlaying = Boolean(active);
  }

  /**
   * Send control command directly to player iframe via postMessage.
   * This bypasses the pirate ad-trap overlay inside the iframe.
   */
  sendPlayerCommand(type, data = {}) {
    const iframe = document.getElementById('main-stream-iframe');
    if (iframe && iframe.contentWindow) {
      try {
        iframe.contentWindow.postMessage(
          Object.assign({ channel: 'zokoanime', type }, data),
          '*'
        );
      } catch (err) {
        console.warn('[AdShield] Error sending command to player:', err);
      }
    }
  }

  /**
   * Listen to state events emitted by the player core
   */
  initPlayerMessageListener() {
    window.addEventListener('message', (e) => {
      const d = e.data;
      if (!d || typeof d !== 'object' || d.channel !== 'zokoanime') return;

      if (d.type === 'state') {
        if (typeof d.paused === 'boolean') {
          this.playerState.isPaused = d.paused;
        }
        if (typeof d.time === 'number') {
          this.playerState.currentTime = d.time;
        }
        if (typeof d.duration === 'number') {
          this.playerState.duration = d.duration;
        }
        for (const cb of this.playerListeners) {
          try { cb(this.playerState); } catch (err) {}
        }
      }
    });
  }

  /**
   * Defuse window.open calls and return sham WindowProxy
   */
  patchWindowOpen() {
    const self = this;
    window.open = function (url, target, features) {
      if (!self.enabled) {
        return self.originalOpen.apply(this, arguments);
      }

      const strUrl = String(url || '');
      // Allow internal OAuth or valid intended navigation if ever used
      if (strUrl.startsWith('data:text/html') || strUrl.includes('accounts.google.com')) {
        return self.originalOpen.apply(this, arguments);
      }

      self.blockedPopupsCount++;
      self.notifyListeners();
      console.warn(`[AdShield] Defused popup attempt to: ${strUrl || 'about:blank'}`);

      // Return a sham WindowProxy object so player anti-adblock checks evaluate to truthy
      return {
        closed: false,
        name: target || '',
        opener: window,
        parent: window,
        top: window,
        close: function () { this.closed = true; },
        focus: function () {},
        blur: function () {},
        postMessage: function () {},
        addEventListener: function () {},
        removeEventListener: function () {},
        location: {
          href: strUrl,
          replace: function () {},
          assign: function () {},
          reload: function () {},
        },
        document: {
          write: function () {},
          open: function () {},
          close: function () {},
          getElementById: function () { return null; },
        },
      };
    };
  }

  /**
   * Intercept synthetic link clicks from ad networks
   */
  patchAnchorClicks() {
    const self = this;
    const originalClick = HTMLAnchorElement.prototype.click;

    HTMLAnchorElement.prototype.click = function () {
      if (self.enabled && self.isStreamPlaying) {
        const href = (this.href || '').toLowerCase();
        const target = (this.target || '').toLowerCase();

        if (target === '_blank' && !href.startsWith(window.location.origin)) {
          self.blockedPopupsCount++;
          self.notifyListeners();
          console.warn(`[AdShield] Blocked synthetic ad anchor click to: ${href}`);
          return;
        }
      }
      return originalClick.apply(this, arguments);
    };
  }

  /**
   * Pop-under & Focus Trap Neutralizer
   */
  patchFocusBlurGuard() {
    const self = this;
    window.addEventListener('blur', () => {
      if (self.enabled && self.isStreamPlaying) {
        // If an iframe click caused this blur (ad popup window opened)
        const isIframeActive = document.activeElement && document.activeElement.tagName === 'IFRAME';
        if (isIframeActive) {
          self.blockedPopupsCount++;
          self.notifyListeners();
          console.warn('[AdShield] Detected iframe ad pop-up blur event — reclaiming focus.');
        }
        setTimeout(() => {
          if (!document.hidden) {
            window.focus();
          }
        }, 30);
      }
    });
  }

  /**
   * Anti-Framebusting / Top navigation hijack protection
   */
  patchTopNavigationGuard() {
    const self = this;
    window.addEventListener('beforeunload', (e) => {
      if (self.enabled && self.isStreamPlaying) {
        const activeTag = document.activeElement ? document.activeElement.tagName : '';
        if (activeTag === 'IFRAME') {
          console.warn('[AdShield] Prevented iframe frame-busting top navigation attempt.');
          e.preventDefault();
          e.returnValue = '';
          return '';
        }
      }
    });
  }
}

export const adShield = new AdShieldEngine();
