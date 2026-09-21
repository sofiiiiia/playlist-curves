#!/usr/bin/env python3
"""Reorder a Spotify playlist so valence over time traces a sine wave.

Steps (each cached in this folder so re-runs are cheap):
  1. auth     - Spotify PKCE login (client id only, no secret)
  2. fetch    - pull every track in the source playlist
  3. features - look up valence per track via ReccoBeats (free, no key)
  4. plan     - pick + order tracks into N playlists of H hours, each one sine cycle
  5. create   - write the playlists to your Spotify account

Usage (client id + playlist id can live in config.json, see config.example.json):
  python3 spotify_sine.py --client-id YOUR_ID --playlist URL_OR_ID            # full run
  python3 spotify_sine.py --client-id YOUR_ID --playlist URL_OR_ID --dry-run  # everything except create
  python3 spotify_sine.py --plan-only                                         # re-plan from cache, no network
Options: --playlists 2 --hours-each 3 --cycles 1 --phase 0 --seed 0
"""
import argparse, base64, csv, hashlib, http.server as httpserver, json, math, os, random, secrets, sys, time
import urllib.parse, urllib.request, webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_F = os.path.join(HERE, "config.json")
CONFIG = json.load(open(CONFIG_F)) if os.path.exists(CONFIG_F) else {}
SRC_PLAYLIST = CONFIG.get("playlist_id", "")
REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-public playlist-modify-private"
TOKEN_F = os.path.join(HERE, "token.json")
TRACKS_F = os.path.join(HERE, "playlist_tracks.json")
FEATS_F = os.path.join(HERE, "features.json")


def http(url, data=None, headers=None, method=None):
    h = {"Accept": "application/json", "User-Agent": "sine-playlist/1.0"}
    h.update(headers or {})
    body = None
    if data is not None:
        if isinstance(data, dict) and h.get("Content-Type", "").startswith("application/x-www-form"):
            body = urllib.parse.urlencode(data).encode()
        else:
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read().decode() or "{}"
                return json.loads(txt)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = int(e.headers.get("Retry-After", "2"))
                print(f"  rate limited, waiting {wait}s"); time.sleep(wait); continue
            print(f"HTTP {e.code} {url}\n{e.read().decode()[:500]}", file=sys.stderr)
            raise
    raise RuntimeError("too many retries")


# ---------- 1. auth (PKCE) ----------
def auth(client_id):
    if os.path.exists(TOKEN_F):
        tok = json.load(open(TOKEN_F))
        if tok.get("expires_at", 0) > time.time() + 60:
            return tok["access_token"]
        if tok.get("refresh_token"):
            r = http("https://accounts.spotify.com/api/token",
                     {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id},
                     {"Content-Type": "application/x-www-form-urlencoded"})
            r.setdefault("refresh_token", tok["refresh_token"])
            r["expires_at"] = time.time() + r["expires_in"]
            json.dump(r, open(TOKEN_F, "w"))
            return r["access_token"]
    if not client_id:
        sys.exit("Need --client-id (create an app at https://developer.spotify.com/dashboard, "
                 f"redirect URI {REDIRECT}, Web API checked).")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": client_id, "response_type": "code", "redirect_uri": REDIRECT, "scope": SCOPES,
        "code_challenge_method": "S256", "code_challenge": challenge, "state": state})
    got = {}

    class H(httpserver.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            got.update({k: v[0] for k, v in q.items()})
            self.send_response(200); self.end_headers()
            self.wfile.write(b"<h2>Authorized. You can close this tab.</h2>")
        def log_message(self, *a): pass

    srv = httpserver.HTTPServer(("127.0.0.1", 8888), H)
    print("Opening browser for Spotify login. If it doesn't open, visit:\n" + url)
    webbrowser.open(url)
    while "code" not in got and "error" not in got:
        srv.handle_request()
    srv.server_close()
    if "error" in got: sys.exit("auth error: " + got["error"])
    if got.get("state") != state: sys.exit("state mismatch")
    r = http("https://accounts.spotify.com/api/token",
             {"grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
              "client_id": client_id, "code_verifier": verifier},
             {"Content-Type": "application/x-www-form-urlencoded"})
    r["expires_at"] = time.time() + r["expires_in"]
    json.dump(r, open(TOKEN_F, "w"))
    return r["access_token"]


# ---------- 2. fetch ----------
def fetch_playlist(token, playlist_id):
    H = {"Authorization": "Bearer " + token}
    meta = http(f"https://api.spotify.com/v1/playlists/{playlist_id}?fields=name", headers=H)
    tracks, url = [], (f"https://api.spotify.com/v1/playlists/{playlist_id}/items?limit=100"
                      "&fields=next,items(item(id,uri,name,duration_ms,artists(name)))")
    while url:
        page = http(url, headers=H)
        for it in page["items"]:
            t = it.get("item")
            if t and t.get("id"):
                tracks.append({"id": t["id"], "uri": t["uri"], "title": t["name"],
                               "artist": ", ".join(a["name"] for a in t["artists"]), "duration": t["duration_ms"]})
        url = page.get("next")
    print(f"fetched {len(tracks)} tracks from '{meta['name']}' "
          f"({sum(t['duration'] for t in tracks)/3.6e6:.2f} h)")
    json.dump({"name": meta["name"], "tracks": tracks}, open(TRACKS_F, "w"), indent=1)
    return tracks


# ---------- 3. features ----------
def fetch_features(tracks):
    feats = json.load(open(FEATS_F)) if os.path.exists(FEATS_F) else {}
    missing = [t["id"] for t in tracks if t["id"] not in feats]
    for i in range(0, len(missing), 40):
        batch = missing[i:i + 40]
        r = http("https://api.reccobeats.com/v1/audio-features?ids=" + ",".join(batch))
        found = {c["href"].rsplit("/", 1)[-1]: c for c in r.get("content", [])}
        for tid in batch:
            feats[tid] = found.get(tid)  # None = not in ReccoBeats
        time.sleep(0.3)
    json.dump(feats, open(FEATS_F, "w"))
    n = sum(1 for t in tracks if feats.get(t["id"]))
    print(f"valence available for {n}/{len(tracks)} tracks")
    return feats


# ---------- 4. plan ----------
def target_curve(pool, cycles, phase):
    """Return f(t01) -> target valence. Amplitude/midline come from the pool's
    valence distribution so the wave actually spans what the tracks can deliver."""
    vs = sorted(t["valence"] for t in pool)
    lo, hi = vs[int(0.05 * len(vs))], vs[int(0.95 * len(vs)) - 1]
    mid, amp = (lo + hi) / 2, (hi - lo) / 2
    return lambda t01: mid + amp * math.sin(2 * math.pi * (cycles * t01 + phase)), mid, amp


def solve_assignment(tracks_sorted, targets_sorted):
    """Choose len(targets) tracks from tracks_sorted (by valence) and match them to
    targets_sorted (by value) minimizing sum |valence - target|. For 1-D costs the
    optimal matching keeps sorted order, so a DP over (tracks used, slots filled) is exact."""
    n, m = len(tracks_sorted), len(targets_sorted)
    INF = float("inf")
    dp = [[INF] * (m + 1) for _ in range(n + 1)]
    choice = [[False] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1): dp[i][0] = 0.0
    for i in range(1, n + 1):
        v = tracks_sorted[i - 1]["valence"]
        for j in range(1, min(i, m) + 1):
            skip = dp[i - 1][j]
            take = dp[i - 1][j - 1] + abs(v - targets_sorted[j - 1])
            if take < skip: dp[i][j], choice[i][j] = take, True
            else: dp[i][j] = skip
    picks, i, j = [None] * m, n, m
    while j > 0:
        if choice[i][j]: picks[j - 1] = tracks_sorted[i - 1]; j -= 1
        i -= 1
    return picks, dp[n][m]


def plan(tracks, feats, n_playlists, hours, cycles, phase, seed):
    """Fixed slots per playlist -> joint optimal track selection + assignment across
    all playlists (so no playlist gets leftovers). Slot count is chosen so the summed
    duration lands near the target; slot times are refined from real durations."""
    pool = [{**t, "valence": feats[t["id"]]["valence"]} for t in tracks if feats.get(t["id"])]
    rng = random.Random(seed); rng.shuffle(pool)  # tie-break order for equal valences
    total_ms = hours * 3.6e6
    f, mid, amp = target_curve(pool, cycles, phase)
    print(f"target sine: midline {mid:.2f}, amplitude {amp:.2f} (from the pool's 5th-95th percentile valence)")
    avg = sum(t["duration"] for t in pool) / len(pool)
    K = max(1, round(total_ms / avg))
    if K * n_playlists > len(pool):
        K = len(pool) // n_playlists
        print(f"  not enough tracks for {hours} h each; using all -> {K} per playlist")
    tracks_sorted = sorted(pool, key=lambda t: t["valence"])

    def solve(slot_times):  # slot_times[p] = list of midpoint times (ms)
        slots = [(p, k, f(slot_times[p][k] / total_ms)) for p in range(n_playlists) for k in range(len(slot_times[p]))]
        slots.sort(key=lambda s: s[2])
        picks, cost = solve_assignment(tracks_sorted, [s[2] for s in slots])
        plans = [[None] * len(slot_times[p]) for p in range(n_playlists)]
        for (p, k, tgt), tr in zip(slots, picks):
            plans[p][k] = {**tr, "target": tgt}
        return plans, cost

    slot_times = [[(k + 0.5) * avg for k in range(K)] for _ in range(n_playlists)]
    for it in range(4):
        plans, cost = solve(slot_times)
        # recompute midpoints from actual durations; also nudge K if duration is off
        new_times = []
        for order in plans:
            t, mids = 0.0, []
            for tr in order:
                mids.append(t + tr["duration"] / 2); t += tr["duration"]
            new_times.append(mids)
        if new_times == slot_times: break
        slot_times = new_times
    for order in plans:
        t = 0.0
        for tr in order:
            tr["start_ms"] = t; t += tr["duration"]
    return plans


def report(plans, name):
    for i, order in enumerate(plans, 1):
        dur = sum(t["duration"] for t in order) / 3.6e6
        err = sum(abs(t["valence"] - t["target"]) for t in order) / max(len(order), 1)
        print(f"\nPlaylist {i}: {len(order)} tracks, {dur:.2f} h, mean |valence - target| = {err:.3f}")
        path = os.path.join(HERE, f"plan_{i}.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["pos", "start_hh:mm", "valence", "target", "title", "artist", "uri"])
            for k, t in enumerate(order, 1):
                s = int(t["start_ms"] // 60000)
                w.writerow([k, f"{s//60}:{s%60:02d}", f"{t['valence']:.3f}", f"{t['target']:.3f}",
                            t["title"], t["artist"], t["uri"]])
        print(f"  wrote {path}")
    try:
        chart(plans, name)
    except Exception as e:
        print("  (chart skipped:", e, ")")


def chart(plans, name):
    """Small multiples, one per playlist. Colors: default validated palette slots 1-2
    (blue = track valence, orange = target sine); text/grid use ink tokens."""
    import matplotlib; matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    SURF, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
    S1, S2 = "#2a78d6", "#eb6834"
    n = len(plans)
    fig, axes = plt.subplots(n, 1, figsize=(10, 2.8 * n + 0.8), squeeze=False)
    fig.patch.set_facecolor(SURF)
    for ax, order, i in zip(axes[:, 0], plans, range(1, n + 1)):
        ax.set_facecolor(SURF)
        xs = [(t["start_ms"] + t["duration"] / 2) / 3.6e6 for t in order]
        ax.plot(xs, [t["target"] for t in order], color=S2, lw=2, solid_joinstyle="round", label="target sine")
        ax.plot(xs, [t["valence"] for t in order], color=S1, lw=2, marker="o", ms=5,
                markeredgecolor=SURF, markeredgewidth=1, label="track valence")
        yv, yt = order[-1]["valence"], order[-1]["target"]
        if abs(yv - yt) < 0.07:  # keep end labels from colliding
            yv, yt = (yv - 0.035, yt + 0.035) if yv <= yt else (yv + 0.035, yt - 0.035)
        ax.text(xs[-1] + 0.05, yv, "track valence", color=INK2, fontsize=8, va="center")
        ax.text(xs[-1] + 0.05, yt, "target sine", color=INK2, fontsize=8, va="center")
        ax.set_ylim(0, 1); ax.set_xlim(0, xs[-1] + 0.6)
        ax.set_ylabel("valence", color=INK2, fontsize=9)
        ax.set_title(f"Playlist {i}  ·  {len(order)} tracks, {sum(t['duration'] for t in order)/3.6e6:.1f} h",
                     loc="left", fontsize=10, color=INK)
        for sp in ("top", "right"): ax.spines[sp].set_visible(False)
        for sp in ("left", "bottom"): ax.spines[sp].set_color(AXIS)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(axis="y", color=GRID, lw=0.8); ax.set_axisbelow(True)
    axes[-1, 0].set_xlabel("hours into playlist", color=INK2, fontsize=9)
    leg = axes[0, 0].legend(frameon=False, loc="upper right", fontsize=8)
    for txt in leg.get_texts(): txt.set_color(INK2)
    fig.suptitle(f"{name}: valence over time", x=0.01, ha="left", fontsize=11, color=INK)
    fig.tight_layout()
    out = os.path.join(HERE, "valence_plan.png"); fig.savefig(out, dpi=150, facecolor=SURF); print(f"  wrote {out}")


# ---------- 5. create ----------
def create(token, plans, src_name):
    H = {"Authorization": "Bearer " + token}
    me = http("https://api.spotify.com/v1/me", headers=H)["id"]
    for i, order in enumerate(plans, 1):
        pl = http("https://api.spotify.com/v1/me/playlists", headers=H,
                  data={"name": f"{src_name} — sine {i}/{len(plans)}", "public": False,
                        "description": "valence traces a sine wave over the playlist"})
        uris = [t["uri"] for t in order]
        for k in range(0, len(uris), 100):
            http(f"https://api.spotify.com/v1/playlists/{pl['id']}/items", headers=H, data={"uris": uris[k:k + 100]})
        print(f"created: {pl['external_urls']['spotify']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--client-id", default=os.environ.get("SPOTIFY_CLIENT_ID") or CONFIG.get("client_id"))
    ap.add_argument("--playlist", default=SRC_PLAYLIST, help="Spotify playlist id or URL")
    ap.add_argument("--playlists", type=int, default=2)
    ap.add_argument("--hours-each", type=float, default=3.0)
    ap.add_argument("--cycles", type=float, default=1.0, help="sine cycles per playlist")
    ap.add_argument("--phase", type=float, default=0.0, help="0 = start mid rising; 0.75 = start at trough; 0.25 = start at peak")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan-only", action="store_true", help="use cached tracks/features, no Spotify calls")
    a = ap.parse_args()
    a.playlist = a.playlist.rstrip("/").split("/")[-1].split("?")[0].split(":")[-1] if a.playlist else ""
    if not a.playlist and not a.plan_only:
        sys.exit("Need --playlist (or playlist_id in config.json)")

    if a.plan_only:
        d = json.load(open(TRACKS_F)); tracks, name = d["tracks"], d["name"]
        feats = json.load(open(FEATS_F))
    else:
        token = auth(a.client_id)
        if os.path.exists(TRACKS_F):
            d = json.load(open(TRACKS_F)); tracks, name = d["tracks"], d["name"]
            print(f"using cached {len(tracks)} tracks (delete {TRACKS_F} to refetch)")
        else:
            tracks = fetch_playlist(token, a.playlist); name = json.load(open(TRACKS_F))["name"]
        feats = fetch_features(tracks)
    plans = plan(tracks, feats, a.playlists, a.hours_each, a.cycles, a.phase, a.seed)
    report(plans, name)
    if a.dry_run or a.plan_only:
        print("\nDry run: nothing written to Spotify."); return
    create(token, plans, name)


if __name__ == "__main__":
    main()
