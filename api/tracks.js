// Vercel serverless function: read any public Spotify playlist or album without a user login,
// using the app's client-credentials token. Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET
// in the Vercel project's environment variables. Nothing here touches user accounts.
let cached = { token: null, expires: 0 };

async function appToken() {
  if (cached.token && cached.expires > Date.now() + 60000) return cached.token;
  const r = await fetch('https://accounts.spotify.com/api/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'client_credentials',
      client_id: process.env.SPOTIFY_CLIENT_ID, client_secret: process.env.SPOTIFY_CLIENT_SECRET }) });
  const j = await r.json();
  if (!j.access_token) throw new Error('Spotify token: ' + (j.error_description || j.error || r.status));
  cached = { token: j.access_token, expires: Date.now() + j.expires_in * 1000 };
  return cached.token;
}

function parseLink(link) {
  link = (link || '').trim();
  const m = link.match(/open\.spotify\.com\/(?:intl-[a-z]+\/)?(playlist|album)\/([A-Za-z0-9]+)/) || link.match(/spotify:(playlist|album):([A-Za-z0-9]+)/);
  if (m) return { kind: m[1], id: m[2] };
  if (/^[A-Za-z0-9]{22}$/.test(link)) return { kind: 'playlist', id: link };
  throw new Error('Not a Spotify playlist or album link');
}

async function sp(url, token) {
  const r = await fetch(url, { headers: { Authorization: 'Bearer ' + token } });
  if (r.status === 429) { await new Promise(s => setTimeout(s, (+r.headers.get('Retry-After') || 2) * 1000)); return sp(url, token); }
  if (!r.ok) { const e = await r.json().catch(() => ({})); const err = new Error((e.error && e.error.message) || `Spotify ${r.status}`); err.status = r.status; throw err; }
  return r.json();
}
const rowOf = t => ({ id: t.id, uri: t.uri, title: t.name, artist: t.artists.map(a => a.name).join(', '), duration: t.duration_ms });

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 's-maxage=300, stale-while-revalidate=600');
  try {
    if (!process.env.SPOTIFY_CLIENT_ID || !process.env.SPOTIFY_CLIENT_SECRET) {
      return res.status(503).json({ error: 'server not configured: set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET' });
    }
    const link = (req.query && req.query.link) || '';
    if (!link) return res.status(400).json({ error: 'missing link' });
    const { kind, id } = parseLink(link);
    const token = await appToken(), tracks = [];
    let name, image, owner, description, url;
    if (kind === 'playlist') {
      const meta = await sp(`https://api.spotify.com/v1/playlists/${id}?fields=name,images,description,owner(display_name)`, token); name = meta.name; image = (meta.images || [])[0]?.url; owner = meta.owner?.display_name; description = meta.description || '';
      url = `https://api.spotify.com/v1/playlists/${id}/items?limit=100&fields=next,items(item(id,uri,name,duration_ms,artists(name)))`;
      while (url) { const page = await sp(url, token); for (const it of page.items) if (it.item && it.item.id) tracks.push(rowOf(it.item)); url = page.next; }
    } else {
      const meta = await sp(`https://api.spotify.com/v1/albums/${id}`, token); name = meta.name; image = (meta.images || [])[0]?.url; owner = (meta.artists || []).map(a => a.name).join(', '); description = meta.release_date ? `Released ${meta.release_date}` : '';
      url = `https://api.spotify.com/v1/albums/${id}/tracks?limit=50`;
      while (url) { const page = await sp(url, token); for (const t of page.items) if (t && t.id) tracks.push(rowOf(t)); url = page.next; }
    }
    res.status(200).json({ name, image, owner, description, kind, id, url: `https://open.spotify.com/${kind}/${id}`, tracks });
  } catch (e) {
    const status = e.status === 404 ? 404 : 400;
    res.status(status).json({ error: e.status === 404 ? 'Not found. Is the playlist public?' : e.message });
  }
};
