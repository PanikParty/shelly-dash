#!/usr/bin/env python3
"""Shelly collector for battery sensors (H&T Gen3 etc.).

Battery Shellies deep-sleep and only wake to report, so polling on a fixed
interval is useless — the device is almost never there. Instead this collector
runs a continuous mDNS watch (`avahi-browse -p -r _shelly._tcp`): every wake
is announced on the LAN, and the device is polled immediately while awake.
Devices that stay reachable (USB-powered) are re-polled on a short interval.

Store: SQLite, one row per device per minute-bucket (INSERT OR IGNORE dedupe —
multiple mDNS events fire within one wake window).
"""

import json
import re
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

sys.path.insert(0, "/root/shelly-temps")   # Duncan's shelly_cloud.py client
import shelly_cloud

DB = "/opt/shelly-dash/data/shelly.db"
SERVICE = "_shelly._tcp"
CREDS = "/root/shelly-temps/.shelly_creds.json"
CLOUD_POLL_S = 60          # one device/list call per cycle — far under the 1 req/s limit
CLOUD_MAX_AGE_S = 7*86400  # ignore cached snapshots older than this
MAC_RE = re.compile(r"([0-9a-fA-F]{12})$")
MIN_TRIGGER_GAP = 20     # s; ignore mDNS re-triggers of the same device inside this
AWAKE_REPOLL_S = 60      # s; re-poll devices that stayed reachable (powered units)
AWAKE_GRACE_S = 900      # device counts as awake while last_ok is within this
INFO_REFRESH_S = 3600    # s between GetDeviceInfo refreshes per device
HTTP_TIMEOUT = 3         # short: fail fast, retry while the wake window is open
POLL_RETRIES = 3         # attempts per sighting
POLL_RETRY_GAP = 1.5     # s between attempts

_lock = threading.Lock()
_last_trigger = {}       # device_id -> ts of last mDNS-triggered poll
_last_ok = {}            # device_id -> ts of last successful poll
_last_info = {}          # device_id -> ts of last GetDeviceInfo fetch
_ip = {}                 # device_id -> last known IPv4


def canon_id(name):
    """Canonical device id = bare MAC (12 hex). mDNS service names carry a
    model prefix that varies by generation (shellyhtg3-…, shellyplusht-…)
    while the Shelly Cloud identifies devices by bare MAC — the MAC is the
    only key both worlds agree on."""
    m = MAC_RE.search(name or "")
    return m.group(1).lower() if m else (name or "").lower()


def log(msg):
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def db():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db():
    con = db()
    con.executescript("""
    CREATE TABLE IF NOT EXISTS samples(
      device_id TEXT NOT NULL,
      ts        INTEGER NOT NULL,          -- epoch, floored to the minute
      t_c       REAL,
      rh        REAL,
      bat_pct   REAL,
      rssi      INTEGER,
      PRIMARY KEY(device_id, ts)
    );
    CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
    CREATE TABLE IF NOT EXISTS devices(
      device_id  TEXT PRIMARY KEY,
      name       TEXT,                     -- user label (editable via API)
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


def jget(url, timeout=HTTP_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def upsert_device(con, device_id, ip=None, hostname=None, app=None,
                  model=None, gen=None, fw=None, name=None, touch=True):
    now = int(time.time())
    con.execute("""
      INSERT INTO devices(device_id, name, ip, hostname, app, model, gen, fw,
                          first_seen, last_seen)
      VALUES(?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(device_id) DO UPDATE SET
        name      = COALESCE(name, excluded.name),
        ip        = COALESCE(excluded.ip, ip),
        hostname  = COALESCE(excluded.hostname, hostname),
        app       = COALESCE(excluded.app, app),
        model     = COALESCE(excluded.model, model),
        gen       = COALESCE(excluded.gen, gen),
        fw        = COALESCE(excluded.fw, fw),
        last_seen = CASE WHEN ? THEN excluded.last_seen ELSE last_seen END
    """, (device_id, name, ip, hostname, app, model, gen, fw,
          now, now if touch else None, 1 if touch else 0))


def parse_status(st):
    """Pull the reading out of a Gen2/3 Shelly.GetStatus blob, defensively."""
    def g(*keys):
        cur = st
        for k in keys:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(k)
        return cur
    t_c = g("temperature:0", "tC")
    rh = g("humidity:0", "rh")
    bat = g("devicepower:0", "battery", "percent")
    rssi = g("wifi", "rssi")
    ts = g("sys", "unixtime")
    # Range-check like the huzzah firmware does: store NULLs, not garbage.
    if not (isinstance(t_c, (int, float)) and -40 <= t_c <= 85):
        t_c = None
    if not (isinstance(rh, (int, float)) and 0 <= rh <= 100):
        rh = None
    if not (isinstance(bat, (int, float)) and 0 <= bat <= 100):
        bat = None
    if not (isinstance(ts, (int, float)) and ts > 1_600_000_000):
        ts = None
    return t_c, rh, bat, rssi, ts


def poll_device(device_id, ip, hostname=None, txt=None):
    """One awake-window poll: status + (occasionally) device info.

    Battery Shellies are awake for seconds at a time, and the mDNS sighting
    that triggered us may already be stale (avahi replays its cache on
    start). Fail fast and retry a few times to catch a device at the edge of
    its window, then give up until the next sighting."""
    now = int(time.time())
    st = None
    for attempt in range(POLL_RETRIES):
        try:
            st = jget(f"http://{ip}/rpc/Shelly.GetStatus")
            break
        except Exception as e:
            if attempt == POLL_RETRIES - 1:
                log(f"{device_id} @{ip}: GetStatus failed x{POLL_RETRIES}: {e}")
                return
            time.sleep(POLL_RETRY_GAP)
    t_c, rh, bat, rssi, sample_ts = parse_status(st)
    sample_ts = sample_ts or now
    with _lock:
        _last_ok[device_id] = now

    app = model = fw = None
    gen = None
    if txt:
        m = dict(re.findall(r'(\w+)=([^\s"]+)', txt))
        app, fw = m.get("app"), m.get("ver")
        try:
            gen = int(m.get("gen", "")) or None
        except ValueError:
            gen = None
    with _lock:
        need_info = now - _last_info.get(device_id, 0) > INFO_REFRESH_S
    if need_info:
        try:
            info = jget(f"http://{ip}/rpc/Shelly.GetDeviceInfo")
            app = app or info.get("app")
            model = info.get("model")
            fw = fw or info.get("fw_id")
            gen = gen or info.get("gen")
            with _lock:
                _last_info[device_id] = now
        except Exception as e:
            log(f"{device_id} @{ip}: GetDeviceInfo failed: {e}")

    bucket = sample_ts - (sample_ts % 60)
    con = db()
    try:
        cur = con.execute(
            "INSERT OR IGNORE INTO samples(device_id, ts, t_c, rh, bat_pct, rssi)"
            " VALUES(?,?,?,?,?,?)", (device_id, bucket, t_c, rh, bat, rssi))
        upsert_device(con, device_id, ip=ip, hostname=hostname,
                      app=app, model=model, gen=gen, fw=fw)
        con.commit()
        if cur.rowcount:
            log(f"{device_id} @{ip}: {t_c}°C {rh}%RH bat={bat} rssi={rssi} -> stored")
        else:
            log(f"{device_id} @{ip}: {t_c}°C {rh}%RH (dup minute, skipped)")
    finally:
        con.close()


def sighting(fields):
    """Handle one resolved avahi-browse -p line ('=' record, IPv4 only)."""
    if fields[0] != "=" or fields[2] != "IPv4":
        return
    name, hostname, ip, port = fields[3], fields[6], fields[7], fields[8]
    txt = fields[9] if len(fields) > 9 else ""
    if not ip:
        return
    device_id = canon_id(name)
    now = time.time()
    with _lock:
        if now - _last_trigger.get(device_id, 0) < MIN_TRIGGER_GAP:
            return
        _last_trigger[device_id] = now
        _ip[device_id] = ip
    log(f"{device_id} awake: {hostname} {ip}:{port} {txt}")
    # Register the device even if the poll below loses the sleep race — an
    # mDNS sighting is proof the sensor exists, and the dashboard should list
    # it (with "never reported" freshness) rather than pretend it isn't there.
    m = dict(re.findall(r'(\w+)=([^\s"]+)', txt))
    try:
        gen = int(m.get("gen", "")) or None
    except ValueError:
        gen = None
    con = db()
    try:
        upsert_device(con, device_id, ip=ip, hostname=hostname,
                      app=m.get("app"), fw=m.get("ver"), gen=gen)
        con.commit()
    finally:
        con.close()
    threading.Thread(target=poll_device, args=(device_id, ip, hostname, txt),
                     daemon=True).start()


def mdns_watch():
    """Run avahi-browse forever; restart it if it exits."""
    while True:
        log(f"starting avahi-browse watch on {SERVICE}")
        try:
            proc = subprocess.Popen(
                ["avahi-browse", "-p", "-r", SERVICE],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    sighting(line.split(";"))
                except Exception as e:
                    log(f"sighting parse error: {e} ({line[:120]})")
            proc.wait()
            log(f"avahi-browse exited rc={proc.returncode}; restarting in 5 s")
        except FileNotFoundError:
            log("avahi-browse not found; install avahi-tools. Retrying in 30 s")
            time.sleep(30)
        except Exception as e:
            log(f"watch error: {e}; restarting in 5 s")
        time.sleep(5)


def awake_repoll():
    """Re-poll devices that stay reachable (USB-powered units never sleep)."""
    while True:
        time.sleep(AWAKE_REPOLL_S)
        now = time.time()
        with _lock:
            targets = [(d, _ip[d]) for d, t in _last_ok.items()
                       if now - t < AWAKE_GRACE_S and d in _ip]
        for device_id, ip in targets:
            threading.Thread(target=poll_device, args=(device_id, ip),
                             daemon=True).start()


def cloud_loop():
    """Once a minute: one device/list call → names + cached status snapshots.

    The cloud's per-device status is a cache of the device's last push. Its
    sys.unixtime is the moment the DEVICE took the reading, so storing by
    that timestamp (INSERT OR IGNORE, and only when newer than our latest
    row) lets fresh pushes flow in without ever corrupting the series with
    ancient cache. Battery units push on threshold drift or the 2 h floor —
    Duncan's reconfigure.py cron keeps cloud push enabled — while the mDNS
    watch keeps winning the race for truly live wake-window readings."""
    creds = None
    while True:
        try:
            if creds is None:
                with open(CREDS) as f:
                    c = json.load(f)
                creds = shelly_cloud.auth_with_key(c["auth_key"], c["server_host"])
            key, host = creds
            resp = shelly_cloud.list_devices_key(host, key)
            devs = (resp.get("data") or {}).get("devices") or {}
            now = int(time.time())
            con = db()
            stored = 0
            try:
                for mac, d in devs.items():
                    did = mac.lower()
                    name = d.get("name")
                    try:
                        gen = int(d.get("gen")) if d.get("gen") is not None else None
                    except (TypeError, ValueError):
                        gen = None
                    app = d.get("type")            # model code, e.g. S3SN-0U12A
                    st = (d.get("ss") or {}).get("status") or {}
                    ip = (st.get("wifi") or {}).get("sta_ip")
                    snap_ts = (st.get("sys") or {}).get("unixtime")
                    if (isinstance(snap_ts, (int, float))
                            and snap_ts > now - CLOUD_MAX_AGE_S):
                        last = con.execute(
                            "SELECT MAX(ts) FROM samples WHERE device_id=?",
                            (did,)).fetchone()[0] or 0
                        if snap_ts > last:
                            t_c, rh, bat, rssi, _ = parse_status(st)
                            b = int(snap_ts)
                            cur = con.execute(
                                "INSERT OR IGNORE INTO samples"
                                "(device_id, ts, t_c, rh, bat_pct, rssi)"
                                " VALUES(?,?,?,?,?,?)",
                                (did, b - (b % 60), t_c, rh, bat, rssi))
                            if cur.rowcount:
                                stored += 1
                                upsert_device(con, did, ip=ip, name=name,
                                              app=app, gen=gen, touch=True)
                                continue
                    # name/ip refresh only — never fake freshness on last_seen
                    upsert_device(con, did, ip=ip, name=name, app=app,
                                  gen=gen, touch=False)
                con.commit()
            finally:
                con.close()
            log(f"cloud: {len(devs)} devices, {stored} fresh snapshot(s) stored")
        except Exception as e:
            log(f"cloud: {e}")
            creds = None               # re-auth next cycle
        time.sleep(CLOUD_POLL_S)


def main():
    init_db()
    threading.Thread(target=awake_repoll, daemon=True).start()
    threading.Thread(target=cloud_loop, daemon=True).start()
    mdns_watch()  # blocks forever


if __name__ == "__main__":
    main()
