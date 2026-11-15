import {
  STREAM_REQUEST_HEADERS,
  parseSafeUpstreamUrl,
  corsHeaders,
} from './_shared.mjs';

export const config = {
  runtime: 'edge',
};

export default async function handler(request) {
  if (request.method === 'OPTIONS') {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }

  const reqUrl = new URL(request.url);
  const rawTarget = reqUrl.searchParams.get('url') || '';
  const parsed = parseSafeUpstreamUrl(rawTarget);
  if (!parsed) {
    return new Response('Invalid VTT URL', { status: 400, headers: corsHeaders() });
  }

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 8000);
  try {
    const upstream = await fetch(parsed.href, {
      headers: STREAM_REQUEST_HEADERS,
      signal: ctrl.signal,
      redirect: 'follow',
    });
    clearTimeout(timer);
    if (!upstream.ok) {
      return new Response('Failed to fetch subtitle file', {
        status: upstream.status,
        headers: corsHeaders(),
      });
    }
    return new Response(upstream.body, {
      status: 200,
      headers: corsHeaders({
        'Content-Type': 'text/vtt; charset=utf-8',
        'Cache-Control': 'public, max-age=86400, s-maxage=86400',
        'CDN-Cache-Control': 'public, max-age=86400',
      }),
    });
  } catch {
    clearTimeout(timer);
    return new Response('Subtitle proxy error', {
      status: 502,
      headers: corsHeaders({ 'Cache-Control': 'no-store' }),
    });
  }
}
