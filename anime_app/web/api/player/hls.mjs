import {
  STREAM_REQUEST_HEADERS,
  parseSafeUpstreamUrl,
  buildCandidateUrls,
  corsHeaders,
  fetchWithHedge,
  rewriteHlsPlaylist,
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
    return new Response('Invalid HLS URL', { status: 400, headers: corsHeaders() });
  }

  const candidates = buildCandidateUrls(parsed.href, parsed);

  try {
    const { res: upstream } = await fetchWithHedge(candidates, STREAM_REQUEST_HEADERS, {
      hedgeMs: 1500,
      timeoutMs: 6500,
      acceptOk: (s) => s === 200,
    });
    const rawText = await upstream.text();
    const rewritten = rewriteHlsPlaylist(rawText, parsed.href);

    return new Response(rewritten, {
      status: 200,
      headers: corsHeaders({
        'Content-Type': 'application/vnd.apple.mpegurl',
        'Cache-Control': 'public, max-age=180, s-maxage=300',
        'CDN-Cache-Control': 'public, max-age=300',
      }),
    });
  } catch {
    return new Response('HLS proxy error', {
      status: 502,
      headers: corsHeaders({ 'Cache-Control': 'no-store' }),
    });
  }
}
