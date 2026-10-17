from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Body:
    id: str
    x_km: float
    y_km: float
    vx_km_s: float
    vy_km_s: float
    kind: str
    fuel: float = 100.0
    coverage: float = 0.0
    alive: bool = True


def distance(a: Body, b: Body) -> float:
    return math.hypot(a.x_km - b.x_km, a.y_km - b.y_km)


def propagate(body: Body, dt: float, decay: float = 0.00002) -> None:
    """Advance a simple orbital-plane body with slow inward drift."""
    if not body.alive:
        return
    body.x_km += body.vx_km_s * dt
    body.y_km += body.vy_km_s * dt
    body.x_km *= 1.0 - decay * dt
    body.y_km *= 1.0 - decay * dt


def apply_burn(body: Body, dvx: float, dvy: float, duration_sec: float) -> float:
    cost = math.hypot(dvx, dvy) * max(duration_sec, 1.0) * 0.02
    body.vx_km_s += dvx
    body.vy_km_s += dvy
    body.fuel = max(0.0, body.fuel - cost)
    return cost


def closest_approach(a: Body, b: Body, horizon_sec: float = 300.0) -> float:
    """Return the exact closest distance under linear relative motion in the horizon."""
    dvx, dvy = a.vx_km_s - b.vx_km_s, a.vy_km_s - b.vy_km_s
    dx, dy = a.x_km - b.x_km, a.y_km - b.y_km
    speed_sq = dvx * dvx + dvy * dvy
    if speed_sq <= 1e-18:
        return math.hypot(dx, dy)
    closest_time = max(0.0, min(horizon_sec, -(dx * dvx + dy * dvy) / speed_sq))
    return math.hypot(dx + dvx * closest_time, dy + dvy * closest_time)


def closest_approach_time(a: Body, b: Body, horizon_sec: float = 300.0) -> float:
    """Time in seconds to the linear-motion closest approach within the horizon."""
    dvx, dvy = a.vx_km_s - b.vx_km_s, a.vy_km_s - b.vy_km_s
    dx, dy = a.x_km - b.x_km, a.y_km - b.y_km
    speed_sq = dvx * dvx + dvy * dvy
    if speed_sq <= 1e-18:
        return 0.0
    return max(0.0, min(horizon_sec, -(dx * dvx + dy * dvy) / speed_sq))


def predict_encounters(satellite: Body, debris: list[Body], horizon_sec: float = 300.0,
                       threshold_km: float = 10.0) -> list[dict]:
    """Return approaching debris contacts sorted by closest-approach time."""
    encounters = []
    for object_ in debris:
        dx, dy = object_.x_km - satellite.x_km, object_.y_km - satellite.y_km
        rvx, rvy = object_.vx_km_s - satellite.vx_km_s, object_.vy_km_s - satellite.vy_km_s
        closing = dx * rvx + dy * rvy < 0
        distance_km = closest_approach(satellite, object_, horizon_sec)
        if closing and distance_km < threshold_km:
            encounters.append({"debris": object_, "distance_km": distance_km,
                               "time_sec": closest_approach_time(satellite, object_, horizon_sec)})
    return sorted(encounters, key=lambda item: (item["time_sec"], item["distance_km"]))
