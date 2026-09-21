#!/usr/bin/env python3
"""Estimate Spotify-style audio features (valence, energy, ...) for tracks that have none,
from their 30-second previews, using models fitted on the tracks that do have values.

  python estimate_features.py cache/playlist_<id>.json      # fill gaps for that item
  python estimate_features.py --report                      # cross-validation report only

Previews come from Spotify's public embed pages. Descriptors are extracted with librosa and
cached per track in previews/. Estimated entries are written to features.json with
"estimated": true so the UI can flag them.
"""
import json, os, re, sys, time, urllib.request, warnings
import numpy as np
warnings.filterwarnings("ignore")

HERE = os.path.dirname(os.path.abspath(__file__))
PREV = os.path.join(HERE, "previews"); os.makedirs(PREV, exist_ok=True)
URLS_F = os.path.join(PREV, "urls.json")
FEATS_F = os.path.join(HERE, "features.json")
TARGETS = ["valence", "energy", "danceability", "acousticness", "instrumentalness", "loudness", "speechiness", "tempo"]
BOUNDS = {"loudness": (-60, 0), "tempo": (40, 250)}  # others clip to [0, 1]


def _get(url, binary=False):
    r = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    b = urllib.request.urlopen(r, timeout=30).read()
    return b if binary else b.decode()


def preview_url(tid, urls):
    if tid in urls: return urls[tid]
    try:
        h = _get(f"https://open.spotify.com/embed/track/{tid}")
        ent = json.loads(re.search(r'__NEXT_DATA__" type="application/json">(.*?)</script>', h, re.S).group(1))
        urls[tid] = (ent["props"]["pageProps"]["state"]["data"]["entity"].get("audioPreview") or {}).get("url")
    except Exception:
        urls[tid] = None
    json.dump(urls, open(URLS_F, "w")); time.sleep(0.2)
    return urls[tid]


def descriptors(tid, urls):
    """~100 numbers describing the clip; cached."""
    cf = os.path.join(PREV, f"{tid}.desc.json")
    if os.path.exists(cf): return json.load(open(cf))
    url = preview_url(tid, urls)
    if not url: return None
    mp3 = os.path.join(PREV, f"{tid}.mp3")
    if not os.path.exists(mp3): open(mp3, "wb").write(_get(url, binary=True))
    import librosa
    y, sr = librosa.load(mp3, sr=22050, mono=True)
    if len(y) < sr: return None
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=512))
    mel = librosa.feature.melspectrogram(S=S ** 2, sr=sr)
    mfcc = librosa.feature.mfcc(S=librosa.power_to_db(mel), n_mfcc=20)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    onset = librosa.onset.onset_strength(S=librosa.power_to_db(mel), sr=sr)
    tempo = float(np.atleast_1d(librosa.feature.tempo(onset_envelope=onset, sr=sr))[0])
    y_h, y_p = librosa.effects.hpss(y)
    rms = librosa.feature.rms(S=S)[0]
    # key / mode via Krumhansl profiles on mean chroma
    maj = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
    mnr = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
    cm = chroma.mean(axis=1)
    cmaj = max(np.corrcoef(np.roll(maj, k), cm)[0, 1] for k in range(12))
    cmin = max(np.corrcoef(np.roll(mnr, k), cm)[0, 1] for k in range(12))
    f = {
        "tempo": tempo, "onset_mean": onset.mean(), "onset_std": onset.std(),
        "rms_mean": rms.mean(), "rms_std": rms.std(), "rms_db": 20 * np.log10(rms.mean() + 1e-9),
        "dyn_range": 20 * np.log10((np.percentile(rms, 95) + 1e-9) / (np.percentile(rms, 5) + 1e-9)),
        "centroid": librosa.feature.spectral_centroid(S=S, sr=sr).mean(), "centroid_std": librosa.feature.spectral_centroid(S=S, sr=sr).std(),
        "bandwidth": librosa.feature.spectral_bandwidth(S=S, sr=sr).mean(),
        "rolloff": librosa.feature.spectral_rolloff(S=S, sr=sr).mean(),
        "flatness": librosa.feature.spectral_flatness(S=S).mean(),
        "zcr": librosa.feature.zero_crossing_rate(y).mean(),
        "harm_ratio": float(np.sum(y_h ** 2) / (np.sum(y_h ** 2) + np.sum(y_p ** 2) + 1e-9)),
        "major_corr": cmaj, "minor_corr": cmin, "majorness": cmaj - cmin, "chroma_std": chroma.std(),
        "chroma_entropy": float(-(cm / cm.sum() * np.log(cm / cm.sum() + 1e-9)).sum()),
    }
    for i, v in enumerate(mfcc.mean(axis=1)): f[f"mfcc{i}_m"] = v
    for i, v in enumerate(mfcc.std(axis=1)): f[f"mfcc{i}_s"] = v
    for i, v in enumerate(librosa.feature.spectral_contrast(S=S, sr=sr).mean(axis=1)): f[f"contrast{i}"] = v
    for i, v in enumerate(librosa.feature.tonnetz(y=y_h, sr=sr).mean(axis=1)): f[f"tonnetz{i}"] = v
    for i, v in enumerate(np.sort(cm)[::-1]): f[f"chroma_rank{i}"] = v
    f = {k: float(v) for k, v in f.items()}
    json.dump(f, open(cf, "w"))
    return f


def build_models(known, urls, report=False):
    """known: list of (tid, feats dict with real values). Returns {target: fitted pipeline}."""
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.linear_model import RidgeCV
    from sklearn.svm import SVR
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.model_selection import cross_val_predict, KFold
    rows, ys = [], {t: [] for t in TARGETS}
    for i, (tid, fe) in enumerate(known):
        d = descriptors(tid, urls)
        if d is None: continue
        rows.append(d)
        for t in TARGETS: ys[t].append(fe[t])
        if report and i % 25 == 24: print(f"  descriptors {i + 1}/{len(known)}", file=sys.stderr)
    keys = sorted(rows[0]); X = np.array([[r[k] for k in keys] for r in rows])
    models, kf = {}, KFold(5, shuffle=True, random_state=0)
    for t in TARGETS:
        y = np.array(ys[t])
        cands = {"ridge": make_pipeline(StandardScaler(), RidgeCV(alphas=np.logspace(-2, 3, 20))),
                 "svr": make_pipeline(StandardScaler(), SVR(C=1.0, epsilon=0.05)),
                 "gbr": GradientBoostingRegressor(n_estimators=300, max_depth=2, learning_rate=0.03, subsample=0.8, random_state=0)}
        if t == "tempo":  # the beat tracker beats any model; snap octave errors toward the known median
            med = float(np.median(y)); tl = X[:, keys.index("tempo")]
            snap = lambda v: min((v / 2, v, v * 2), key=lambda c: abs(c - med))
            if report: print(f"{t:17s} direct beat tracker, octave-snapped: MAE {np.mean([abs(snap(v) - yy) for v, yy in zip(tl, y)]):.1f} BPM")
            models[t] = (("tempo", med), keys); continue
        best = None
        for name, m in cands.items():
            pred = cross_val_predict(m, X, y, cv=kf)
            mae = np.mean(np.abs(pred - y)); r2 = 1 - np.sum((pred - y) ** 2) / np.sum((y - y.mean()) ** 2)
            if best is None or mae < best[1]: best = (name, mae, r2, m)
        if report:
            base = np.mean(np.abs(y - np.median(y)))
            print(f"{t:17s} best={best[0]:5s}  CV MAE {best[1]:.3f}  (guess-the-median MAE {base:.3f})  R² {best[2]:.2f}")
        models[t] = (best[3].fit(X, y), keys)
    return models


def estimate(track_ids, known, urls):
    models = build_models(known, urls)
    out = {}
    for tid in track_ids:
        d = descriptors(tid, urls)
        if d is None: out[tid] = None; continue
        est = {"estimated": True, "href": f"https://open.spotify.com/track/{tid}"}
        for t, (m, keys) in models.items():
            if isinstance(m, tuple):  # direct tempo with octave snap
                med, v = m[1], d["tempo"]; v = min((v / 2, v, v * 2), key=lambda c: abs(c - med))
            else:
                v = float(m.predict(np.array([[d[k] for k in keys]]))[0])
            lo, hi = BOUNDS.get(t, (0, 1)); est[t] = round(min(hi, max(lo, v)), 3)
        out[tid] = est
    return out


def main():
    urls = json.load(open(URLS_F)) if os.path.exists(URLS_F) else {}
    feats = json.load(open(FEATS_F)) if os.path.exists(FEATS_F) else {}
    known = [(tid, f) for tid, f in feats.items() if f and not f.get("estimated")]
    if "--report" in sys.argv:
        build_models(known, urls, report=True); return
    item = json.load(open(sys.argv[1]))
    missing = [t for t in item["tracks"] if not feats.get(t["id"])]
    if not missing: print("nothing to estimate"); return
    print(f"estimating {len(missing)} tracks from {len(known)} known ones")
    est = estimate([t["id"] for t in missing], known, urls)
    for t in missing:
        if est.get(t["id"]):
            feats[t["id"]] = est[t["id"]]
            print(f"  {t['title'][:40]:40s} valence≈{est[t['id']]['valence']:.2f} energy≈{est[t['id']]['energy']:.2f} tempo≈{est[t['id']]['tempo']:.0f}")
        else:
            print(f"  {t['title'][:40]:40s} no preview available")
    json.dump(feats, open(FEATS_F, "w"))


if __name__ == "__main__":
    main()
