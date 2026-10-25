"""Deterministic trace simulator. Same JSON schema as a real GPS source.
Usage: python collector/gps_simulator.py --scenario normal|deviation|stop|gap
"""
import argparse, math, random, time
from datetime import datetime, timezone
import requests

K = 111320.0


def along(route, step_m):
    """Points every step_m metres along the route polyline."""
    pts = []
    for (a, b), (c, d) in zip(route, route[1:]):
        dx = (d - b) * K * math.cos(math.radians(a))
        dy = (c - a) * K
        n = max(1, int(math.hypot(dx, dy) / step_m))
        pts += [(a + (c - a) * i / n, b + (d - b) * i / n) for i in range(n)]
    pts.append(route[-1])
    return pts


def build(scenario, route, speed_kmh):
    pts = along(route, speed_kmh / 3.6)          # 1 sample per second
    rng = random.Random(42)                      # fixed seed -> repeatable
    n = lambda: rng.gauss(0, 0.00002)            # ~2 m GPS noise
    out = []                                     # (lat, lon, speed, extra_sleep_s)
    cut = len(pts) * 3 // 10
    for i, (la, lo) in enumerate(pts):
        if scenario == "stop" and i == cut:
            out += [(la + n(), lo + n(), rng.uniform(0, 1), 0) for _ in range(25)]
        if scenario == "gap" and i == cut:
            out.append((la + n(), lo + n(), speed_kmh, 20))   # 20 s > stale timeout
        if scenario == "deviation" and cut <= i < cut + 12:
            la += 0.0012                                       # ~130 m off route
        out.append((la + n(), lo + n(), speed_kmh + rng.uniform(-2, 2), 0))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="normal", choices=["normal", "deviation", "stop", "gap"])
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--vehicle", default="VH001")
    ap.add_argument("--speed", type=float, default=36)
    ap.add_argument("--interval", type=float, default=1.0)
    a = ap.parse_args()

    route = requests.get(f"{a.api}/api/config", timeout=5).json()["route"]
    r = requests.get(f"{a.api}/api/vehicles/{a.vehicle}/latest", timeout=5)
    seq = r.json()["sequence"] + 1 if r.ok else 0

    for la, lo, sp, extra in build(a.scenario, route, a.speed):
        time.sleep(extra)
        payload = dict(vehicle_id=a.vehicle,
                       timestamp_utc=datetime.now(timezone.utc).isoformat(),
                       latitude=la, longitude=lo, speed_kmh=round(max(sp, 0), 1),
                       sequence=seq, source="simulator")
        res = requests.post(f"{a.api}/api/positions", json=payload, timeout=5)
        print(seq, res.status_code, f"{la:.5f},{lo:.5f}", payload["speed_kmh"])
        seq += 1
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
