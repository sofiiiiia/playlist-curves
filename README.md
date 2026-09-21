# Playlist curves

Reorder a Spotify playlist so its audio features follow a shape over time. Draw a sine
wave for valence, a slow rise in tempo, a hill in energy, then export the result as CSV,
Spotify URIs, or brand-new playlists on your account.

![The curve editor](docs/editor.png)

Two ways to use it:

- **Web editor** (`server.py` + `index.html`). One panel per feature, each with a
  draggable target curve. Every drag re-solves the track order live.
- **CLI** (`spotify_sine.py`). Valence-only sine wave, produces CSVs and a chart.

![Valence over time for two playlists](docs/valence_plan.png)

## How it works

1. Pulls every track in the source playlist through the Spotify Web API (PKCE login,
   no client secret).
2. Looks up audio features (valence, tempo, energy, danceability, acousticness,
   instrumentalness, loudness, speechiness) from [ReccoBeats](https://reccobeats.com),
   because Spotify's own audio-features endpoint returns 403 for apps created after
   November 2024. Tracks it can't match by ID are retried by title search.
3. Splits the playlist into N playlists of H hours. Each playlist gets a set of time
   slots, and every slot has a target value per enabled feature read off your curve.
4. Solves the slot-to-track assignment exactly (Hungarian algorithm in the browser,
   a sorted-matching dynamic program in the CLI), minimizing the weighted distance
   between each track's features and its slot's targets. Slot times are then refined
   from real track durations and the solve is repeated.
5. Exports.

## Setup

You need Python 3.10+ and a free Spotify developer app.

1. Go to <https://developer.spotify.com/dashboard>, create an app, set the redirect URI
   to exactly `http://127.0.0.1:8888/callback`, tick Web API, and copy the Client ID.
2. Copy `config.example.json` to `config.json` and fill in the Client ID and your
   playlist URL or ID.
3. Optional, for the CLI chart only: `pip install -r requirements.txt`.

## Web editor

```
python3 server.py
```

Opens <http://127.0.0.1:8765>. The first run pops a browser tab to log into Spotify;
the token is saved to `token.json` and refreshed automatically afterwards.

- Tick a feature chip to add its panel. Drag the handles to shape the curve, or pick a
  preset (sine, two cycles, hill, valley, rise, fall, flat).
- The weight slider decides how much each feature counts when several are on.
- Dots are the actual tracks. Hover for a crosshair and the track playing at that moment.
- Set the number of playlists and hours each. Tracks that don't fit, or have no feature
  data, are listed under Unplaced.
- Export: **Download CSVs**, **Copy URIs** (paste into a playlist in the Spotify desktop
  app), or **Create on Spotify** (private playlists on your account).

## CLI

```
python3 spotify_sine.py --dry-run            # plan + chart, nothing written
python3 spotify_sine.py                      # also creates the playlists
python3 spotify_sine.py --playlists 2 --hours-each 6 --cycles 1 --phase 0.75
```

`--cycles` sets sine cycles per playlist, `--phase 0.75` starts at the trough,
`--phase 0.25` at the peak, `--seed` reshuffles near-tie tracks, `--plan-only` re-plans
from cache with no network.

## Files

| File | Purpose |
|---|---|
| `index.html` | the editor, vanilla JS and SVG, no build step |
| `server.py` | serves the page, the cached data, and the create endpoint |
| `spotify_sine.py` | Spotify auth, fetch, ReccoBeats lookup, CLI planner, chart |
| `config.example.json` | template for `config.json` |

`config.json`, `token.json`, and the cached `playlist_tracks.json` / `features.json` are
git-ignored.
