# shelly-dash

Property climate dashboard for battery-powered **Shelly H&T** (Gen2/3)
sensors. The dashboard shows a live card grid, a maximized two-lane
time-series view (Temp / Humidity) with hover crosshair + data tooltips,
and a blueprint heatmap of the whole property.

**The hard problem this solves:** battery Shellies deep-sleep and only wake
for a few seconds to report. Polling on a fixed interval misses almost
every wake. Instead the collector runs a continuous **mDNS watch** — every
wake is announced on the LAN (`_shelly._tcp`) and the device is polled
*immediately while it is awake* — backed by a once-a-minute **Shelly Cloud
snapshot poll** so pushes that arrive while nobody is watching still land in
the database.

| File | Purpose |
|---|---|
| `collector.py` | mDNS wake-watch + awake-window polling + cloud snapshot loop → SQLite |
| `server.py` | FastAPI: overview/samples/stats/CSV API over the store, serves the UI |
| `requirements.txt` | fastapi, uvicorn, certifi |
| `web/index.html` | the dashboard — dependency-free canvas, no build step |
| `web/map.html`, `web/heatmap.html`, `web/blueprint.svg` | property blueprint heatmap (`/map`; `heatmap.html` is a static duplicate of `map.html`) |
| `units/shelly-collector.service` | systemd unit for the collector |
| `units/shelly-dash.service` | systemd unit for the webapp |

## Architecture

Two services; both are required.

```
Shelly devices (battery + powered)
   │  wake announced via mDNS (_shelly._tcp)
   ▼
collector.py ── avahi-browse watch: poll device within its wake window
   │          ── awake-repoll loop: re-poll powered units every 60 s
   │          ── cloud loop: 1×/min Shelly Cloud snapshot poll
   ▼
SQLite  /opt/shelly-dash/data/shelly.db
   │        samples(device_id, ts minute-bucket, t_c, rh, bat_pct, rssi)
   │        devices(device_id, name, ip, hostname, app, model, gen, fw)
   ▼
server.py ── FastAPI :8088, reads only, never writes
   ▼
web/       ── dashboard + heatmap, plain canvas JS, no CDN/build
```

Key behaviors:

- **Storage is deduped by the minute**: one row per device per
  minute-bucket (`INSERT OR IGNORE`), because multiple mDNS events fire
  within a single wake window. The collector never overwrites history.
- **Cloud snapshots can't corrupt the series**: a cloud row is stored only
  when its `sys.unixtime` (the moment the *device* took the reading) is
  newer than the device's latest local row, so ancient cached pushes never
  leak in.
- **mDNS sighting alone registers the device** — if the poll loses the
  sleep race, the sensor still appears on the dashboard (as stale) instead
  of vanishing.
- **Battery H&T status blobs are range-checked** (`-40..85 °C`,
  `0..100 %RH`); garbage reads are stored as NULL, never plotted.
- **Live values are server-aligned**: the UI computes freshness from the
  server clock, not the browser clock.

## Dashboard

- Card grid: latest Temp/Humidity, 30-minute deltas, 6 h sparklines, badge
  (live / idle / stale) from last-report age, battery %, RSSI.
- **Maximize a card** (⤢): two full-size lanes (Temp °C, Humidity %RH) with
  range buttons 1H / 6H / 24H / 7D / ALL, min/max/avg stats, and
  **hover crosshair + tooltip** showing the nearest real sample for each
  metric (each metric resolves independently — a sensor that drops one
  reading doesn't blank the other lane).
- Double-click a sensor name to rename it (persisted to the DB, survives
  collector restarts).
- **Weather & forecast panel** (footer): Open-Meteo model for the property —
  observed hourly history (solid, filled) back to 14 days, dashed 16-day
  forecast, with the **Shed / any outdoor sensor measured trace overlaid** as
  dots so local reality is compared against the model on the same axis.
  Ranges 48H / 1W / 2W / 4W / ALL, °F ⇄ °C toggle, sensor dropdown (any
  device — indoor vs outdoor spread), hover crosshair + tooltip. The weather
  model is fetched by the server (`/api/weather`) and TTL-cached 15 min, so
  the page never hammers the upstream API.
- Pause/resume live refresh, refresh interval select (10 s – 5 m),
  blueprint link (`/map`).
- No auth — keep on a trusted LAN or bind to one interface.

## Install (Debian/Fedora host)

```sh
# deps — mDNS watch needs avahi
sudo dnf install -y avahi avahi-tools   # or: apt install avahi-daemon avahi-utils
sudo systemctl enable --now avahi-daemon

# app
sudo mkdir -p /opt/shelly-dash/data
sudo cp -r collector.py server.py requirements.txt units web /opt/shelly-dash/
sudo python3 -m venv /opt/shelly-dash/.venv
sudo /opt/shelly-dash/.venv/bin/pip install -r /opt/shelly-dash/requirements.txt

# Shelly Cloud client dependency (cloud fallback loop)
#   collector.py imports shelly_cloud from /root/shelly-temps and reads the
#   cloud auth key at /root/shelly-temps/.shelly_creds.json
#   {"auth_key": "...", "server_host": "shelly-XX.cloud.shelly.cloud"}
#   Get the auth_key from the Shelly app: Settings → Authorization → Cloud key.
#   mDNS wake capture works without this; the cloud loop just adds coverage.

sudo cp units/shelly-dash.service units/shelly-collector.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now shelly-collector shelly-dash
```

Both units run as root and log to journald. The webapp must never run alone
for long — it serves a dashboard over whatever the collector has written.

## Updating from GitHub

```sh
cd /opt/shelly-dash && git pull origin main
sudo systemctl restart shelly-collector shelly-dash
curl -s http://localhost:8088/api/overview | python3 -m json.tool | head
```

## API

| Endpoint | Returns |
|---|---|
| `/api/overview` | devices + latest sample + 6 h sparklines — one call paints the grid |
| `/api/samples?device=&from=&to=` | raw rows for one device in range, ascending |
| `/api/stats?device=&from=&to=` | per-field min / max / avg / n for one device |
| `/api/devices/{id}/name` | `POST {"name": "…"}` — persist a friendly label |
| `/api/export.csv?device=&from=&to=` | ML-ready CSV (`pandas.read_csv` the URL) |
| `/api/weather?device=&past_days=&forecast_days=` | Open-Meteo model past+forecast (temp °F, RH) + measured overlay for one device; server TTL-cached 15 min |
| `/` | the dashboard UI (no CDN) |
| `/map` | property blueprint heatmap |

## Data

- SQLite at `/opt/shelly-dash/data/shelly.db` (WAL mode).
- Sparklines are the last 6 h from `/api/overview`; the maximized lanes
  fetch `/api/samples` per selected range.
- The collector re-auths to the Shelly Cloud on any error and ignores cloud
  cached snapshots older than 7 days.
