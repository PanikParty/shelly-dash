#!/usr/bin/env python3
"""Shelly climate dashboard server: query API + rename + ML export + UI.

  /api/overview          devices + latest sample + 6h sparklines (one call
                         paints the whole card grid)
  /api/samples           rows for one device in [from,to], ascending
  /api/stats             per-field min/max/avg/n for one device + range
  /api/devices/{id}/name POST {"name": ...} — persist a friendly label
  /api/export.csv        ML-ready CSV (pandas.read_csv the URL)
  /                      the dashboard UI (web/index.html, no CDN)
Run: uvicorn server:app --host 0.0.0.0 --port 8088
"""

import csv
import io
import json
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

DB = "/opt/shelly-dash/data/shelly.db"
WEB = Path("/opt/shelly-dash/web")
FIELDS = ("t_c", "rh", "bat_pct", "rssi")
SPARK_WINDOW_S = 6 * 3600

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
