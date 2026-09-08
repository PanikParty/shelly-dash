#!/usr/bin/env python3
"""Shelly climate dashboard server: query API + rename + ML export + UI.

  /api/overview          devices + latest sample + 6h sparklines (one call
                         paints the whole card grid)
  /api/samples           rows for one device in [from,to], ascending
  /api/stats             per-field min/max/avg/n for one device + range
  /api/devices/{id}/name POST {"name": ...} — persist a friendly label
  /api/export.csv        ML-ready CSV (pandas.read_csv the URL)
  /api/weather           Open-Meteo model (past+forecast) + local sensor overlay
  /                      the dashboard UI (web/index.html, no CDN)
Run: uvicorn server:app --host 0.0.0.0 --port 8088
"""

import csv
import io
import json
import ssl
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

DB = "/opt/shelly-dash/data/shelly.db"
WEB = Path("/opt/shelly-dash/web")
FIELDS = ("t_c", "rh", "bat_pct", "rssi")
SPARK_WINDOW_S = 6 * 3600

# ---- weather (Open-Meteo, free, no key) --------------------------------
W_LAT, W_LON = 32.791, -117.194          # property (Bay Park, San Diego)
W_TZ = "America/Los_Angeles"
W_URL = ("https://api.open-meteo.com/v1/forecast"
         "?latitude={lat}&longitude={lon}"
         "&hourly=temperature_2m,relative_humidity_2m"
         "&temperature_unit=fahrenheit"
         "&timezone={tz}&past_days={pd}&forecast_days={fd}")
W_MAX_AGE_S = 15 * 60                    # server-side cache for API friendliness
try:
    import certifi
    _CA = certifi.where()
except Exception:
    _CA = None

_weather_cache = {"ts": 0, "pd": 0, "fd": 0, "data": None}


def _fetch_weather(past_days: int, forecast_days: int) -> dict:
    """Hourly model temp (°F) + RH from now-past_days to now+forecast_days,
    as [{ts, t_f, rh}] epoch-ascending. Raises on network/parse failure."""
    url = W_URL.format(lat=W_LAT, lon=W_LON, tz=W_TZ,
                       pd=past_days, fd=forecast_days)
    ctx = ssl.create_default_context()
    if _CA:
        ctx.load_verify_locations(_CA)
    req = urllib.request.Request(url, headers={"User-Agent": "shelly-dash/1.0"})
    with urllib.request.urlopen(req, timeout=25, context=ctx) as r:
        j = json.loads(r.read().decode())
    h = j["hourly"]
    tz = ZoneInfo(W_TZ)
    points = []
    for iso, tf, rh in zip(h["time"], h["temperature_2m"], h["relative_humidity_2m"]):
        ts = int(datetime.fromisoformat(iso).replace(tzinfo=tz).timestamp())
        if tf is None or rh is None:
            continue
        points.append({"ts": ts, "t_f": tf, "rh": rh})
    points.sort(key=lambda p: p["ts"])
    return {"source": "Open-Meteo.com", "tz": W_TZ,
            "lat": W_LAT, "lon": W_LON, "generated": int(time.time()),
            "points": points}


def get_weather(past_days: int = 14, forecast_days: int = 16) -> dict:
    """TTL-cached model weather; on a fresh fetch failure serves the last
    good payload if still young, otherwise lets the error propagate."""
    now = int(time.time())
    c = _weather_cache
    if (c["data"] and now - c["ts"] < W_MAX_AGE_S
            and c["pd"] == past_days and c["fd"] == forecast_days):
        return c["data"]
    fresh = _fetch_weather(past_days, forecast_days)
    c.update(ts=now, pd=past_days, fd=forecast_days, data=fresh)
    return fresh

app = FastAPI(title="shelly-dash")


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    """Idempotent schema — the collector owns writes, but the API must answer
    (empty) queries before the first sample ever lands."""
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS samples(
      device_id TEXT NOT NULL,
      ts        INTEGER NOT NULL,
      t_c       REAL,
      rh        REAL,
      bat_pct   REAL,
      rssi      INTEGER,
      PRIMARY KEY(device_id, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
    CREATE TABLE IF NOT EXISTS devices(
      device_id  TEXT PRIMARY KEY,
      name       TEXT,
      ip         TEXT,
      hostname   TEXT,
      app        TEXT,
      model      TEXT,
      gen        INTEGER,
      fw         TEXT,
      first_seen INTEGER,
      last_seen  INTEGER
    );""")
    con.commit()
    con.close()


init_db()


@app.get("/api/overview")
def overview():
    now = int(time.time())
    con = db()
    devs = con.execute("SELECT * FROM devices ORDER BY COALESCE(name, device_id)").fetchall()
    out = []
    for d in devs:
        did = d["device_id"]
        latest = con.execute(
            "SELECT ts, t_c, rh, bat_pct, rssi FROM samples"
            " WHERE device_id=? ORDER BY ts DESC LIMIT 1", (did,)).fetchone()
        spark_rows = con.execute(
            "SELECT ts, t_c, rh FROM samples WHERE device_id=? AND ts>=?"
            " ORDER BY ts", (did, now - SPARK_WINDOW_S)).fetchall()
        count = con.execute(
            "SELECT COUNT(*) c, MIN(ts) lo FROM samples WHERE device_id=?",
            (did,)).fetchone()
        out.append({
            "device_id": did,
            "name": d["name"],
            "ip": d["ip"],
            "app": d["app"],
            "model": d["model"],
            "gen": d["gen"],
            "fw": d["fw"],
            "first_seen": d["first_seen"],
            "last_seen": d["last_seen"],
            "latest": dict(latest) if latest else None,
            "spark": {
                "t_c": [[r["ts"], r["t_c"]] for r in spark_rows if r["t_c"] is not None],
                "rh": [[r["ts"], r["rh"]] for r in spark_rows if r["rh"] is not None],
            },
            "count": count["c"],
            "first_ts": count["lo"],
        })
    con.close()
    return {"now": now, "devices": out}


@app.get("/api/samples")
def samples(device: str,
            frm: int = Query(0, alias="from"),
            to: int = Query(2**31, alias="to"),
            limit: int = Query(20000, le=200000)):
    con = db()
    rows = con.execute(
        f"""SELECT ts, {','.join(FIELDS)} FROM samples
            WHERE device_id=? AND ts BETWEEN ? AND ? ORDER BY ts LIMIT ?""",
        (device, frm, to, limit)).fetchall()
    con.close()
    return {"samples": [dict(r) for r in rows]}


@app.get("/api/stats")
def stats(device: str,
          frm: int = Query(0, alias="from"),
          to: int = Query(2**31, alias="to")):
    con = db()
    out = {}
    for f in FIELDS:
        r = con.execute(
            f"""SELECT MIN({f}) lo, MAX({f}) hi, AVG({f}) avg, COUNT({f}) n
                FROM samples WHERE device_id=? AND ts BETWEEN ? AND ?
                AND {f} IS NOT NULL""",
            (device, frm, to)).fetchone()
        out[f] = {"min": r["lo"], "max": r["hi"], "avg": r["avg"], "n": r["n"]}
    con.close()
    return out


@app.post("/api/devices/{device_id}/name")
async def rename(device_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "json body required"}, status_code=400)
    name = (body.get("name") or "").strip()[:64] or None
    con = db()
    cur = con.execute("UPDATE devices SET name=? WHERE device_id=?", (name, device_id))
    con.commit()
    con.close()
    if not cur.rowcount:
        return JSONResponse({"error": "unknown device"}, status_code=404)
    return {"device_id": device_id, "name": name}


@app.get("/api/weather")
def weather(device: str = Query(None),
            past_days: int = Query(14, ge=1, le=92),
            forecast_days: int = Query(16, ge=1, le=16)):
    """Outdoor weather context for the dashboard footer chart.

    - model: Open-Meteo hourly temp (°F) + RH, past_days before now and
      forecast_days after, server-side TTL-cached (15 min).
    - local:  Shelly samples for one sensor over the same window (t_c °C),
      so measured reality can be overlaid against the model.
    """
    try:
        w = get_weather(past_days, forecast_days)
        err = None
    except Exception as e:                      # network down / API hiccup
        w = None
        err = f"weather fetch failed: {e}"
    con = db()
    now = int(time.time())
    frm = now - past_days * 86400
    to = now + forecast_days * 86400
    local = None
    if device:
        row = con.execute("SELECT device_id, name FROM devices WHERE device_id=?",
                          (device,)).fetchone()
        if row:
            pts = con.execute(
                "SELECT ts, t_c, rh FROM samples WHERE device_id=? AND ts BETWEEN ? AND ?"
                " ORDER BY ts", (device, frm, to)).fetchall()
            local = {
                "device_id": row["device_id"],
                "name": row["name"],
                "points": [{"ts": r["ts"], "t_c": r["t_c"], "rh": r["rh"]}
                           for r in pts],
            }
    # outdoor candidates for the overlay dropdown: sensors whose name hints
    # outside the conditioned envelope
    out = []
    for r in con.execute("SELECT device_id, name FROM devices ORDER BY COALESCE(name, device_id)"):
        nm = (r["name"] or "").lower()
        if any(k in nm for k in ("shed", "garage", "backyard", "outdoor", "patio")):
            out.append({"device_id": r["device_id"], "name": r["name"]})
    con.close()
    return {"now": now,
            "model": w,
            "error": err,
            "local": local,
            "outdoor_candidates": out}


@app.get("/api/export.csv", response_class=PlainTextResponse)
def export_csv(device: str = Query(None),
               frm: int = Query(0, alias="from"),
               to: int = Query(2**31, alias="to")):
    con = db()
    if device:
        rows = con.execute(
            f"""SELECT device_id, ts, {','.join(FIELDS)} FROM samples
                WHERE device_id=? AND ts BETWEEN ? AND ? ORDER BY ts""",
            (device, frm, to)).fetchall()
    else:
        rows = con.execute(
            f"""SELECT device_id, ts, {','.join(FIELDS)} FROM samples
                WHERE ts BETWEEN ? AND ? ORDER BY device_id, ts""",
            (frm, to)).fetchall()
    con.close()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["device_id", "ts", *FIELDS])
    for r in rows:
        w.writerow([r[k] for k in ("device_id", "ts", *FIELDS)])
    return PlainTextResponse(
        buf.getvalue(),
        headers={"Content-Disposition": "attachment; filename=shelly-samples.csv"})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/map")
def map_view():
    return FileResponse(WEB / "map.html")


app.mount("/", StaticFiles(directory=WEB), name="static")
