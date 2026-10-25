import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.detection import CONFIG, ROUTE, Detector

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "vehicle_tracker.db"
detectors: dict[str, Detector] = {}   # in-memory detector state per vehicle

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vehicle_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL, received_utc TEXT NOT NULL,
  latitude REAL NOT NULL, longitude REAL NOT NULL, speed_kmh REAL NOT NULL,
  sequence INTEGER NOT NULL, source TEXT NOT NULL,
  UNIQUE(vehicle_id, sequence));
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  vehicle_id TEXT NOT NULL, type TEXT NOT NULL,
  start_utc TEXT NOT NULL, trigger_utc TEXT NOT NULL,
  observed REAL NOT NULL, threshold REAL NOT NULL, closed_utc TEXT);
"""


def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


@asynccontextmanager
async def lifespan(app):
    with connect() as con:
        con.executescript(SCHEMA)
    yield


app = FastAPI(title="Vehicle Tracker API", lifespan=lifespan)
# Dev only: lets the dashboard open from a file or another port.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class Position(BaseModel):
    vehicle_id: str = Field(min_length=1, max_length=40)
    timestamp_utc: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    speed_kmh: float = Field(ge=0, le=400)
    sequence: int = Field(ge=0)
    source: str = "gps"


@app.post("/api/positions")
def add_position(p: Position):
    now = datetime.now(timezone.utc)
    ts = p.timestamp_utc if p.timestamp_utc.tzinfo else p.timestamp_utc.replace(tzinfo=timezone.utc)
    with connect() as con:
        last = con.execute("SELECT MAX(sequence) m FROM positions WHERE vehicle_id=?",
                           (p.vehicle_id,)).fetchone()["m"]
        try:
            con.execute(
                "INSERT INTO positions (vehicle_id,timestamp_utc,received_utc,latitude,"
                "longitude,speed_kmh,sequence,source) VALUES (?,?,?,?,?,?,?,?)",
                (p.vehicle_id, ts.isoformat(), now.isoformat(), p.latitude,
                 p.longitude, p.speed_kmh, p.sequence, p.source))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Duplicate vehicle sequence")
        if last is not None and p.sequence < last:   # stored, but not fed to detectors
            return {"accepted": True, "detection": "skipped_out_of_order"}
        det = detectors.setdefault(p.vehicle_id, Detector())
        for e in det.process(ts, p.latitude, p.longitude, p.speed_kmh):
            if e[0] == "open":
                _, typ, start, trig, obs, thr = e
                con.execute("INSERT INTO alerts (vehicle_id,type,start_utc,trigger_utc,observed,"
                            "threshold) VALUES (?,?,?,?,?,?)",
                            (p.vehicle_id, typ, start.isoformat(), trig.isoformat(), obs, thr))
            else:
                con.execute("UPDATE alerts SET closed_utc=? WHERE vehicle_id=? AND type=? "
                            "AND closed_utc IS NULL", (e[2].isoformat(), p.vehicle_id, e[1]))
    return {"accepted": True, "received_utc": now.isoformat()}


def _latest(con, vid):
    return con.execute("SELECT * FROM positions WHERE vehicle_id=? "
                       "ORDER BY sequence DESC LIMIT 1", (vid,)).fetchone()


@app.get("/api/vehicles/{vid}/latest")
def latest(vid: str):
    with connect() as con:
        row = _latest(con, vid)
    if row is None:
        raise HTTPException(404, "No position recorded")
    return dict(row)


@app.get("/api/vehicles/{vid}/status")
def status(vid: str):
    with connect() as con:
        row = _latest(con, vid)
    if row is None:
        return {"state": "no_data"}
    now = datetime.now(timezone.utc)
    age = (now - datetime.fromisoformat(row["received_utc"])).total_seconds()
    lat = (datetime.fromisoformat(row["received_utc"])
           - datetime.fromisoformat(row["timestamp_utc"])).total_seconds()
    det = detectors.get(vid)
    return {"state": "stale" if age > CONFIG["stale_s"] else "live",
            "age_s": round(age, 1), "latency_s": round(lat, 2),
            "latest": dict(row), "detector": det.info() if det else None}


@app.get("/api/vehicles/{vid}/history")
def history(vid: str, limit: int = 1000):
    with connect() as con:
        rows = con.execute("SELECT * FROM positions WHERE vehicle_id=? "
                           "ORDER BY sequence DESC LIMIT ?", (vid, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


@app.get("/api/alerts")
def alerts(vehicle_id: str | None = None, limit: int = 50):
    q, args = "SELECT * FROM alerts", []
    if vehicle_id:
        q += " WHERE vehicle_id=?"
        args.append(vehicle_id)
    with connect() as con:
        rows = con.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/config")
def config():
    return {"route": ROUTE, "thresholds": CONFIG}


@app.post("/api/reset")
def reset():
    with connect() as con:
        con.executescript("DELETE FROM positions; DELETE FROM alerts;")
    detectors.clear()
    return {"reset": True}


# Serve the dashboard last so /api routes take priority.
app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
