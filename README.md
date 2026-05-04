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
git clone <this repo>
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

## Configuration reference

See `config.example.ini` for all options with inline comments. The interesting knobs are in the `[locator]` section:

- `max_rf_km` — default 35. Tighter values reject more spurious chains. Empirically 35 is the sweet spot in mixed urban/rural coverage; some networks may want 25 or 50.
- `days_lookback` — default 14. The locator uses only observations within this window when triangulating.
- `min_observers` — default 2. Targets seen by fewer than this many distinct receivers are skipped entirely.

## Limitations

- **One-byte path hash collisions.** When two repeaters share the same first byte of their pubkey, chain reconstruction has to disambiguate by distance — usually the right answer, but occasionally wrong. Most production meshes that use 2-byte path hashes don't have this problem.
- **Stale GPS.** If a publishing radio moves and doesn't re-advertise, your stored GPS for that radio drifts from reality. The collector overwrites GPS on every status-topic update, so as long as publishers keep advertising you're fine.
- **No SNR/RSSI calibration.** The algorithm currently treats every first-hop reception as a uniform "within RF range" constraint, ignoring signal strength. Adding an SNR-to-distance model would tighten estimates further but requires per-broker calibration.

## License

MIT — see `LICENSE`.
