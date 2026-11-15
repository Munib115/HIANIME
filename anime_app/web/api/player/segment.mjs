import {
  STREAM_REQUEST_HEADERS,
  parseSafeUpstreamUrl,
  buildCandidateUrls,
  corsHeaders,
  fetchWithHedge,
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
    return new Response('Invalid segment URL', { status: 400, headers: corsHeaders() });
  }

  const headers = { ...STREAM_REQUEST_HEADERS };
  const rangeHdr = request.headers.get('range');
  if (rangeHdr) headers.Range = rangeHdr;

  const candidates = buildCandidateUrls(parsed.href, parsed);

  try {
    const { res: upstream } = await fetchWithHedge(candidates, headers, {
      hedgeMs: 1550,
      timeoutMs: 7000,
      acceptOk: (s) => s === 200 || s === 206,
    });

    const respHeaders = corsHeaders({
      'Content-Type': upstream.headers.get('content-type') || 'video/mp2t',
      'Cache-Control': 'public, max-age=86400, s-maxage=86400, immutable',
      'CDN-Cache-Control': 'public, max-age=86400',
    });

    for (const hName of ['content-length', 'content-range', 'accept-ranges']) {
      const val = upstream.headers.get(hName);
      if (val) respHeaders[hName] = val;
    }

    return new Response(upstream.body, {
      status: upstream.status,
      headers: respHeaders,
    });
  } catch {
    return new Response('Failed to fetch video segment', {
      status: 502,
      headers: corsHeaders({ 'Cache-Control': 'no-store' }),
    });
  }
}
