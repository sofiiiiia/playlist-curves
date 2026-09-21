#!/usr/bin/env python3
"""Local web app for shaping playlist feature curves.
  ./.venv/bin/python server.py    -> http://127.0.0.1:8765
Serves index.html, exposes the cached tracks+features, and creates playlists on
Spotify using the login saved by spotify_sine.py (token.json)."""
import json, os, sys, urllib.parse, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import spotify_sine as ss

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 8765
CFG = json.load(open(os.path.join(HERE, "config.json")))
PLAYLIST = CFG["playlist_id"].rstrip("/").split("/")[-1].split("?")[0].split(":")[-1]


def load_data(refetch=False):
    if refetch or not os.path.exists(ss.TRACKS_F):
        tok = ss.auth(CFG["client_id"])
        ss.fetch_playlist(tok, PLAYLIST)
    d = json.load(open(ss.TRACKS_F))
    feats = ss.fetch_features(d["tracks"]) if refetch else json.load(open(ss.FEATS_F))
    for t in d["tracks"]:
        f = feats.get(t["id"])
        t["features"] = {k: f[k] for k in ("valence", "energy", "danceability", "tempo", "acousticness",
                                           "instrumentalness", "loudness", "speechiness", "liveness")} if f else None
    return d


class H(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        if p.path == "/api/data":
            return self._json(load_data(refetch="refetch" in p.query))
        if p.path in ("/", "/index.html"):
            b = open(os.path.join(HERE, "index.html"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); return self.wfile.write(b)
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != "/api/create":
            self.send_response(404); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
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
