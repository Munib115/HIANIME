/**
 * Shared security, CDN host failover, hedged racing, and playlist rewrite
 * helpers for Vercel Edge Runtime streaming proxies.
 */

export const STREAM_REQUEST_HEADERS = {
  'User-Agent':
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
  Referer: 'https://zokoanime.video/',
  Origin: 'https://zokoanime.video',
};

export const SEGMENT_MIRROR_HOSTS = [
  'https://hls1.drama1.cfd',
  'https://hls1.zoko1.cfd',
  'https://hls1.proxy1.cfd',
  'https://hls.dramahot.top',
];

const PRIVATE_HOST_RE =
  /^(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|169\.254\.\d+\.\d+|0\.0\.0\.0|::1|\[::1\]|.*\.local|.*\.internal)$/i;
const PRIVATE_172_RE = /^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$/;

export function parseSafeUpstreamUrl(rawUrl) {
  if (!rawUrl || typeof rawUrl !== 'string') return null;
  let parsed;
  try {
    parsed = new URL(rawUrl.trim());
  } catch {
    return null;
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return null;
  const host = (parsed.hostname || '').toLowerCase();
  if (!host || PRIVATE_HOST_RE.test(host) || PRIVATE_172_RE.test(host)) {
    return null;
  }
  return parsed;
}

export function buildCandidateUrls(rawUrl, parsed) {
  const urls = [rawUrl];
  if (parsed && (parsed.pathname.startsWith('/s/') || parsed.pathname.startsWith('/p/'))) {
    const suffix = `${parsed.pathname}${parsed.search || ''}`;
    for (const host of SEGMENT_MIRROR_HOSTS) {
      const alt = `${host}${suffix}`;
      if (!urls.includes(alt)) urls.push(alt);
    }
  }
  return urls;
}

export function corsHeaders(extra = {}) {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, HEAD, OPTIONS',
    'Access-Control-Allow-Headers': 'Range, Content-Type',
    'Access-Control-Expose-Headers': 'Content-Length, Content-Range, Accept-Ranges',
    ...extra,
  };
}

/**
 * Fetch primary upstream first; if TTFB exceeds `hedgeMs` or primary fails,
 * race mirror hosts in parallel and abort losing sockets cleanly.
 */
export async function fetchWithHedge(candidates, reqHeaders, { hedgeMs = 1550, timeoutMs = 6500, acceptOk = (s) => s === 200 || s === 206 } = {}) {
  const controllers = new Set();

  const tryOne = async (targetUrl, limitMs) => {
    const ctrl = new AbortController();
    controllers.add(ctrl);
    const timer = setTimeout(() => ctrl.abort(), limitMs);
    try {
      const res = await fetch(targetUrl, {
        method: 'GET',
        headers: reqHeaders,
        signal: ctrl.signal,
        redirect: 'follow',
      });
      if (!acceptOk(res.status)) {
        try { await res.body?.cancel(); } catch {}
        ctrl.abort();
        controllers.delete(ctrl);
        throw new Error(`HTTP ${res.status}`);
      }
      clearTimeout(timer);
      return { res, ctrl };
    } catch (err) {
      clearTimeout(timer);
      ctrl.abort();
      controllers.delete(ctrl);
      throw err;
    }
  };

  const primaryPromise = tryOne(candidates[0], timeoutMs);
  if (candidates.length === 1) {
    return await primaryPromise;
  }

  let hedgeTimer;
  const raceTrigger = new Promise((resolve) => {
    hedgeTimer = setTimeout(() => resolve({ hedge: true }), hedgeMs);
    primaryPromise.then(
      (win) => {
        clearTimeout(hedgeTimer);
        resolve({ win });
      },
      () => {
        clearTimeout(hedgeTimer);
        resolve({ hedge: true });
      }
    );
  });

  const firstOutcome = await raceTrigger;
  if (firstOutcome.win) {
    return firstOutcome.win;
  }

  // Primary stalled past hedgeMs or failed -> race mirrors alongside still-pending primary
  const racers = [primaryPromise, ...candidates.slice(1).map((u) => tryOne(u, timeoutMs))];
  try {
    const winner = await Promise.any(racers);
    for (const c of controllers) {
      if (c !== winner.ctrl) {
        try { c.abort(); } catch {}
      }
    }
    return winner;
  } catch (aggErr) {
    for (const c of controllers) {
      try { c.abort(); } catch {}
    }
    throw aggErr;
  }
}

export function rewriteHlsPlaylist(rawText, baseUrl) {
  const out = [];
  for (const rawLine of rawText.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line) {
      out.push('');
      continue;
    }
    if (line.startsWith('#')) {
      if (line.includes('URI="')) {
        const replaced = line.replace(/URI="([^"]+)"/g, (_, rawUri) => {
          let resolved;
          try {
            resolved = new URL(rawUri, baseUrl);
          } catch {
            return `URI="${rawUri}"`;
          }
          const enc = encodeURIComponent(resolved.href);
          if (resolved.pathname.endsWith('.m3u8')) {
            return `URI="/api/player/hls?url=${enc}"`;
          }
          return `URI="/api/player/segment?url=${enc}"`;
        });
        out.push(replaced);
      } else {
        out.push(line);
      }
      continue;
    }

    let resolved;
    try {
      resolved = new URL(line, baseUrl);
    } catch {
      out.push(line);
      continue;
    }
    const enc = encodeURIComponent(resolved.href);
    if (resolved.pathname.endsWith('.m3u8')) {
      out.push(`/api/player/hls?url=${enc}`);
    } else {
      out.push(`/api/player/segment?url=${enc}`);
    }
  }
  return out.join('\n');
}
