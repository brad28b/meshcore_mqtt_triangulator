# meshcore_mqtt_triangulator

Passively triangulate the location of any [MeshCore](https://meshcore.dev/) repeater (or other advertised node) by listening to an MQTT broker that aggregates packet observations from multiple radios.

You don't need a fancy radio setup, you don't need to be the operator of any of the publishing nodes, and you don't need a calibrated signal-strength model. You just need read access to a broker where MeshCore radios publish what they hear.

The more radios that publish to your broker — and the more of them have a correctly-set GPS position — the more reliable the triangulation will be.

## How it works

When a MeshCore node sends an advert, the packet propagates through the mesh as a flood. Every relay that re-broadcasts it tags it with a one-byte hash of its own pubkey, building up a `path` that records which relays the packet went through. Radios that hear the advert publish what they received to MQTT, including the path and their own pubkey.

If you can correlate:

- **What an advert's path looked like** when it reached each receiving radio (relay sequence)
- **Where each relay in the path is** (their own GPS, learned from their adverts)
- **Where each receiving radio is** (their GPS, learned from their adverts or from operator metadata)

…you can constrain the source's location: for every observed reception, the source must be within RF range of the *first* relay in the path (the one that heard it directly), and the chain back to the receiver must be physically plausible.

Combine many such constraints from many receivers across many relay paths, take the weighted geometric median, and you get an estimate that's typically accurate to within ~1–10 km in dense mesh areas, much wider in sparse rural areas.

## Setup

```bash
git clone https://github.com/brad28b/meshcore_mqtt_triangulator.git
cd meshcore_mqtt_triangulator
pip install -r requirements.txt
cp config.example.ini config.ini
$EDITOR config.ini   # set your broker host, auth, topic patterns
```

## Usage

There are three small CLI tools. They all read `config.ini` from the current directory.

### 1. Run the collector

```bash
./collector.py
```

Connects to your broker, subscribes to the configured status + packets topics, and persists every observation to a SQLite database. Reconnects automatically on broker drops. Run it indefinitely — accuracy improves as the dataset grows.

In production, run it under systemd, supervisord, or a Docker restart policy so it survives reboots.

### 2. List triangulation candidates

```bash
./targets.py
```
<img width="1303" height="296" alt="Screenshot 2026-05-05 144103" src="https://github.com/user-attachments/assets/d8388ad2-5e13-40af-9a7f-ee382e408299" />



Reports every source pubkey for which there's enough data in the database to attempt triangulation, grouped into quality tiers based on how many distinct first-hop relays heard the source. Tier 1 (≥4 distinct first-hop relays) is the most reliable.

### 3. Locate a specific target

```bash
./locate.py --target <pubkey-prefix>
```

Run the chain-walk + weighted-geometric-median algorithm against the stored data and print an estimated GPS coordinate plus a Google Maps link. If the target self-advertises GPS, the error from the actual location is shown too — useful for sanity-checking accuracy on your specific broker / region.

You can also validate the algorithm across all GPS-known targets at once:

```bash
./locate.py --validate
```

## Reliability depends on your data

This tool is **only as good as the data your broker has**:

- **More publishing radios = better.** Each radio that publishes its receptions adds an independent constraint on every advert it hears. With one publisher you have a single anchor and accuracy degrades quickly outside its RF range. With dozens of publishers spread across a city or region, accuracy is much sharper.
- **Publishers must have correct GPS.** A publishing radio with no GPS, or with a wrong GPS, contributes observations the algorithm can't anchor (or anchors at the wrong place). Operators of publishing radios should set their `adv_lat` / `adv_lon` and enable location sharing in their advert.
- **More repeaters with GPS in the path = better disambiguation.** The chain-walk algorithm needs to map each one-byte path hash to a known repeater. The more repeaters in the system that have published their GPS, the fewer ambiguous chains we have to throw away.
- **More time = better.** Repeaters typically advertise every 12 hours. A first-hop relay that the source has been heard through many times gives a much stronger signal than one heard once. Run the collector for at least 24 hours before expecting good results, and ideally at least a week.
- **Targets near the edge of relay coverage are harder.** A repeater on a remote mountain with only one or two nearby neighbours has structurally less position information than one in dense urban relay coverage. Expect 20–50 km errors on edge-of-coverage targets even with lots of data.

## Algorithm

The locator implements **multi-anchor chain-walk + weighted geometric median**:

1. For every observation of the target, anchor at the receiving radio's known GPS and walk the path right-to-left (receiver → last relay → ... → first relay). At each hop, look up that relay's GPS. Reject the chain if any consecutive pair is more than `max_rf_km` apart (the path is then physically implausible).
2. Aggregate the surviving "first-hop relays" by pubkey — each distinct first-hop relay is a constraint that the source is within RF range of *that* relay.
3. Compute the weighted geometric median (Weiszfeld iteration) over all surviving first-hop relays plus all receivers that heard the source 0-hop direct. Weight is `log(n + 1)` where `n` is the number of independent paths confirming each constraint.

Geometric median (rather than centroid) is robust to the few wide-outlier candidates that pass the chain-walk filter.

## Terrain-aware mode (optional)

When you provide a Copernicus GLO-30 DEM via `[terrain] dem_dir` in `config.ini`, the locator switches on **terrain-aware triangulation** — using line-of-sight tests and an empirically calibrated SNR→distance table to tighten the answer where the data permits.

### How it improves accuracy

For each direct (0-hop) observation of a target, the locator tests whether terrain blocks the chord between the receiver and the candidate position, with [Earth-curvature correction at k=4/3 effective radius](https://www.itu.int/rec/R-REC-P.530/en) and a 60% first-Fresnel-zone clearance criterion. Direct observations classified as line-of-sight get a **much tighter Gaussian σ** (down to 1 km when SNR ≥ 10 dB) because we know empirically that high-SNR LoS receptions occur at very short range — pulling the answer toward the true source much harder than a plain disk constraint can.

When ≥ 3 strong-SNR LoS direct receptions exist for the same target, the locator promotes them to **ring constraints** (target on a circle of empirical radius around each receiver) and trilaterates via L-BFGS-B. This is the highest-confidence regime.

### Validation

Tested against 701 GPS-known multi-observer targets in a real-world MQTT-broker dataset (~660 K observations, 7 days, 84 publishers across SE Australia):

| Metric | Baseline | Terrain | Δ |
|---|---:|---:|---:|
| **Median error** | 8.46 km | **7.57 km** | **−0.89 km (−10.5%)** |
| Mean error | 19.23 km | 18.95 km | −0.28 km |
| ≤ 1 km accurate | 114 | **123** | +9 |
| ≤ 5 km accurate | 283 | **297** | +14 |
| ≤ 10 km accurate | 382 | **393** | +11 |

The improvement is concentrated in targets where the data has actual line-of-sight signal — the locator dispatches into one of three profiles per target:

| Profile | When | n | Median error |
|---|---|---:|---:|
| **A** — no direct receptions | Source only heard via multi-hop chains | 507 | 9.27 km (= baseline; LoS at first-hop relays isn't a useful distance signal) |
| **B** — direct receptions present | < 3 strong-SNR LoS direct receptions | 187 | **5.87 km** |
| **C** — ring trilateration | ≥ 3 strong-SNR LoS direct receptions | 7 | **0.70 km** |

Terrain mode is a **strict superset of baseline** — Profile A targets fall through to the same algorithm baseline uses (different per-σ formulation, identical fixed-point), so enabling terrain mode never hurts targets where it has no useful signal.

### Setting it up

```bash
# 1. Install rasterio (numpy + scipy already in baseline requirements)
pip install rasterio

# 2. Download Copernicus GLO-30 tiles for your region
./download_dem.py --auto              # uses bbox of your collected GPS, +50km buffer
# OR
./download_dem.py --bbox -43 138 -27 154   # manual: south west north east

# 3. Set dem_dir in config.ini
$EDITOR config.ini   # under [terrain], set dem_dir = ./dem

# 4. Locate as usual; terrain mode activates automatically
./locate.py --validate
./locate.py --target <pubkey-prefix>
```

DEM tiles are 1°×1°, ~25 MB each, hosted anonymously on AWS Open Data — no account or API key required. A typical regional bbox is 100–300 tiles, ≈ 3–8 GB on disk. Ocean-only squares are simply not in the bucket and skip silently.

You can disable terrain mode at any time with `--no-terrain` on the command line, or by clearing `dem_dir` in the config.

## Configuration reference

See `config.example.ini` for all options with inline comments. The interesting knobs are in the `[locator]` section:

- `max_rf_km` — default 35. Tighter values reject more spurious chains. Empirically 35 is the sweet spot in mixed urban/rural coverage; some networks may want 25 or 50.
- `days_lookback` — default 14. The locator uses only observations within this window when triangulating.
- `min_observers` — default 2. Targets seen by fewer than this many distinct receivers are skipped entirely.

## Limitations

- **One-byte path hash collisions.** When two repeaters share the same first byte of their pubkey, chain reconstruction has to disambiguate by distance — usually the right answer, but occasionally wrong. Most production meshes that use 2-byte path hashes don't have this problem.
- **Stale GPS.** If a publishing radio moves and doesn't re-advertise, your stored GPS for that radio drifts from reality. The collector overwrites GPS on every status-topic update, so as long as publishers keep advertising you're fine.
- **DEM resolution.** Terrain mode uses Copernicus GLO-30 (≈ 30 m horizontal, ~3 m vertical RMSE). Isolated narrow peaks may be under-represented by ~10% — this slightly over-credits chord clearance over true narrow ridges. For higher fidelity you could substitute a regional LiDAR DEM (1–5 m), but Copernicus is good enough for the link distances LoRa typically operates over.
- **Antenna heights.** Without per-node antenna metadata in the DB, the LoS engine assumes both ends use `default_antenna_m` (default 5 m AGL). Real repeater masts are often higher. The σ values are calibrated against the dataset that produced them, so this assumption is baked in — overriding the default may help if your network is dominated by tall sites.

## License

MIT — see `LICENSE`.
