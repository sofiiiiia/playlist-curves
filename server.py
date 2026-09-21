#!/usr/bin/env python3
"""Local web app for shaping playlist feature curves.
  ./.venv/bin/python server.py    -> http://127.0.0.1:8765
Serves index.html; /api/data?link=<spotify playlist or album link> fetches (and caches)
that item's tracks + audio features; /api/create writes playlists using the saved login."""
import json, os, sys, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import spotify_sine as ss

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
CFG = json.load(open(os.path.join(HERE, "config.json")))


CACHE = os.path.join(HERE, "cache"); os.makedirs(CACHE, exist_ok=True)


def load_data(link, refetch=False):
    kind, item_id = ss.parse_link(link or CFG.get("playlist_id", ""))
    cache_f = os.path.join(CACHE, f"{kind}_{item_id}.json")
    if refetch or not os.path.exists(cache_f):
        tok = ss.auth(CFG["client_id"])
        d = ss.fetch_tracks(tok, kind, item_id)
        json.dump(d, open(cache_f, "w"))
    d = json.load(open(cache_f))
    feats = ss.fetch_features(d["tracks"])  # cached in features.json; only new ids hit the network
    for t in d["tracks"]:
        f = feats.get(t["id"])
        t["features"] = {k: f[k] for k in ("valence", "energy", "danceability", "tempo", "acousticness",
                                           "instrumentalness", "loudness", "speechiness")} if f else None
        t["estimated"] = bool(f and f.get("estimated"))
    d["url"] = f"https://open.spotify.com/{kind}/{item_id}"
    return d


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/data":
            q = urllib.parse.parse_qs(p.query)
            try:
                return self._json(load_data(q.get("link", [""])[0], refetch="refetch" in q))
            except Exception as e:
                return self._json({"error": str(e)}, 400)
        if p.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); return self.wfile.write(b)
        self.send_response(404); self.end_headers()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/estimate":
            try:
                import estimate_features as ef
                kind, item_id = ss.parse_link(body.get("link") or CFG.get("playlist_id", ""))
                item = json.load(open(os.path.join(CACHE, f"{kind}_{item_id}.json")))
                urls = json.load(open(ef.URLS_F)) if os.path.exists(ef.URLS_F) else {}
                feats = json.load(open(ef.FEATS_F))
                known = [(tid, f) for tid, f in feats.items() if f and not f.get("estimated")]
                missing = [t["id"] for t in item["tracks"] if not feats.get(t["id"])]
                est = ef.estimate(missing, known, urls) if missing else {}
                for tid, e in est.items():
                    if e: feats[tid] = e
                json.dump(feats, open(ef.FEATS_F, "w"))
                return self._json({"estimated": sum(1 for e in est.values() if e), "no_preview": sum(1 for e in est.values() if not e)})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if self.path != "/api/create":
            self.send_response(404); self.end_headers(); return
        try:
            tok = ss.auth(CFG["client_id"])
            H_ = {"Authorization": "Bearer " + tok}
            out = []
            for pl in body["playlists"]:
                created = ss.http("https://api.spotify.com/v1/me/playlists", headers=H_,
                                  data={"name": pl["name"], "public": False, "description": pl.get("description", "")})
                uris = pl["uris"]
                for k in range(0, len(uris), 100):
                    ss.http(f"https://api.spotify.com/v1/playlists/{created['id']}/items", headers=H_,
                            data={"uris": uris[k:k + 100]})
                out.append({"name": pl["name"], "url": created["external_urls"]["spotify"], "count": len(uris)})
            self._json({"created": out})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *a):
        if "/api/" in (a[0] if a else ""): sys.stderr.write("%s\n" % (fmt % a))


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}/"
    print("serving", url)
    if "--no-browser" not in sys.argv: webbrowser.open(url)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
