"""Detection rules: route deviation (with hysteresis) and unexpected stop."""
import math

# Demo settings. Tune from your own measured traces and report them.
CONFIG = dict(
    corridor_m=50,        # deviation entry threshold
    recovery_m=30,        # must be < corridor_m (hysteresis)
    persist_samples=3,    # consecutive samples outside corridor
    low_speed_kmh=2.0,
    stop_radius_m=15,
    stop_duration_s=15,   # DEMO value; use ~60 s in field tests
    stale_s=15,
)

# Illustrative route (lat, lon). Replace with your real planned route.
ROUTE = [(13.0827, 80.2707), (13.0845, 80.2740), (13.0870, 80.2765), (13.0900, 80.2780)]
PLANNED_STOPS = []  # (lat, lon, radius_m) -> stops inside are not alerted

K = 111320.0  # metres per degree of latitude


def _xy(lat, lon, lat0):
    return lon * K * math.cos(math.radians(lat0)), lat * K


def dist_m(a, b):
    ax, ay = _xy(a[0], a[1], a[0])
    bx, by = _xy(b[0], b[1], a[0])
    return math.hypot(ax - bx, ay - by)


def dist_to_route(lat, lon):
    px, py = _xy(lat, lon, lat)
    best = 1e12
    for (a, b), (c, d) in zip(ROUTE, ROUTE[1:]):
        ax, ay = _xy(a, b, lat)
        bx, by = _xy(c, d, lat)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0 if L2 == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
        best = min(best, math.hypot(px - ax - t * dx, py - ay - t * dy))
    return best


class Detector:
    """Per-vehicle state. Uses device event time for all durations."""

    def __init__(self):
        self.outside = 0
        self.dev = False
        self.dev_start = None
        self.anchor = None
        self.stop_start = None
        self.stop = False
        self.last_dist = None
        self.stationary_s = 0.0

    def info(self):
        return dict(dist_m=self.last_dist, outside=self.outside,
                    stationary_s=round(self.stationary_s, 1),
                    deviating=self.dev, stopped=self.stop)

    def process(self, t, lat, lon, speed, cfg=CONFIG):
        """Returns events: ('open', type, start, trigger, observed, threshold)
        or ('close', type, time)."""
        ev = []

        # --- route deviation ---
        d = dist_to_route(lat, lon)
        self.last_dist = round(d, 1)
        if d > cfg["corridor_m"]:
            if self.outside == 0:
                self.dev_start = t
            self.outside += 1
        else:
            self.outside = 0
        if self.outside >= cfg["persist_samples"] and not self.dev:
            self.dev = True
            ev.append(("open", "deviation", self.dev_start, t, round(d, 1), cfg["corridor_m"]))
        elif self.dev and d < cfg["recovery_m"]:
            self.dev = False
            ev.append(("close", "deviation", t))

        # --- unexpected stop ---
        slow = speed < cfg["low_speed_kmh"]
        if slow and self.anchor is None:
            self.anchor, self.stop_start = (lat, lon), t
        if self.anchor is not None:
            if slow and dist_m(self.anchor, (lat, lon)) < cfg["stop_radius_m"]:
                self.stationary_s = (t - self.stop_start).total_seconds()
                planned = any(dist_m((a, b), (lat, lon)) < r for a, b, r in PLANNED_STOPS)
                if self.stationary_s >= cfg["stop_duration_s"] and not self.stop and not planned:
                    self.stop = True
                    ev.append(("open", "stop", self.stop_start, t,
                               round(self.stationary_s, 1), cfg["stop_duration_s"]))
            else:
                if self.stop:
                    ev.append(("close", "stop", t))
                self.stop, self.anchor, self.stop_start, self.stationary_s = False, None, None, 0.0
        return ev
