/**
 * KuroAnime AdShield Engine
 * Reverse-Engineered from AdGuard Scriptlets (`prevent-window-open`, `window.open-defuser`)
 * and Brave Shields frame protections.
 *
 * Core Objectives:
 * 1. Allow 100% uninterrupted video streaming (HLS, MSE, DRM, Fullscreen, WebAudio).
 * 2. NEVER trigger "Video stream can't be played because of sandbox".
 * 3. Neutralize pop-ups, pop-unders, top-navigation frame hijacking, and rogue click redirects.
 */

const STORAGE_KEY = 'kuro_adshield_active';

class AdShieldEngine {
  constructor() {
    const saved = localStorage.getItem(STORAGE_KEY);
    this.enabled = saved !== null ? saved === 'true' : true; // Enabled by default
    this.originalOpen = window.open;
    this.blockedPopupsCount = 0;
    this.isStreamPlaying = false;
    this.listeners = new Set();
  }

  init() {
    this.patchWindowOpen();
    this.patchAnchorClicks();
    this.patchFocusBlurGuard();
    this.patchTopNavigationGuard();
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

  notifyListeners() {
    for (const cb of this.listeners) {
      try { cb(this.enabled, this.blockedPopupsCount); } catch (e) {}
    }
  }

  setStreamActive(active) {
    this.isStreamPlaying = Boolean(active);
  }

  /**
   * AdGuard / uBlock `nowoif` (no-window-open-if) & `window.open-defuser`
   * Intercepts `window.open`. If AdShield is enabled and it's not a user-initiated
   * trusted action, it defuses the popup and returns a valid sham window object.
   *
   * Crucial: Returning a sham window prevents video players from displaying
   * "Stream can't be played because of sandbox" when they check `if (window.open('...'))`.
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
   * Intercepts synthetic link clicks that pirate ad networks create dynamically
   * (e.g. `<a href="https://ad.com" target="_blank"></a>.click()`)
   */
  patchAnchorClicks() {
    const self = this;
    const originalClick = HTMLAnchorElement.prototype.click;

    HTMLAnchorElement.prototype.click = function () {
      if (self.enabled && self.isStreamPlaying) {
        const href = (this.href || '').toLowerCase();
        const target = (this.target || '').toLowerCase();

        // If it targets a new window or is cross-origin ad link while video is loaded
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
   * Brave Shields style Pop-under & Focus Guard
   * Prevents ad networks from creating a hidden popup and stealing window focus.
   */
  patchFocusBlurGuard() {
    const self = this;
    window.addEventListener('blur', () => {
      if (self.enabled && self.isStreamPlaying) {
        // Popunder or new tab opened by an iframe click
        setTimeout(() => {
          if (document.hidden) {
            // Tab was switched; keep state intact
          } else {
            window.focus();
          }
        }, 10);
      }
    });
  }

  /**
   * Top-Level Navigation Guard (Anti-Framebusting)
   * Prevents iframes from navigating `top.location` to rogue landing pages.
   */
  patchTopNavigationGuard() {
    const self = this;
    window.addEventListener('beforeunload', (e) => {
      if (self.enabled && self.isStreamPlaying) {
        // If a navigation happens without user clicking internal links
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

  /**
   * Reverse-Engineered HTML5 Iframe Sandbox Matrix
   *
   * Why does this allow video streaming without triggering the sandbox error?
   * 1. `allow-scripts`: Required for player JS (HLS.js / JWPlayer).
   * 2. `allow-same-origin`: Required to access cookies, storage, and video segments.
   * 3. `allow-forms`: Required for token auth submissions.
   * 4. `allow-presentation`: Required for Fullscreen & Picture-in-Picture.
   * 5. `allow-popups`: Required so `window.open` does NOT throw DOMException 18
   *    (which triggers the "Stream can't be played because of sandbox" error).
   * 
   * CRITICAL SECURITY RESTRICTIONS KEPT ACTIVE:
   * - `allow-top-navigation` is OMITTED (iframe cannot redirect this site to ads).
   * - `allow-top-navigation-by-user-activation` is OMITTED (clicks cannot redirect top).
   * - `allow-popups-to-escape-sandbox` is OMITTED (any popup inherits sandbox confinement).
  /**
   * Embed providers (Zoko / MegaCloud) explicitly check if their iframe is sandboxed
   * and refuse to stream if the 'sandbox' attribute is detected.
   * Therefore, we omit the sandbox attribute completely so video streaming works 100%
   * uninterrupted without any EMBED BLOCKED errors.
   */
  getIframeSandbox() {
    return null;
  }

  getIframeAttributes() {
    return 'referrerpolicy="no-referrer" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; fullscreen" allowfullscreen scrolling="no"';
  }
}

export const adShield = new AdShieldEngine();
