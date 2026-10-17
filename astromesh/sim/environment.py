from __future__ import annotations

import math
import random

from astromesh.sim.physics import Body, distance, propagate


class SpaceEnvironment:
    def __init__(self, seed: int = 17) -> None:
        rng = random.Random(seed)
        self.time_sec = 0.0
        self.epoch = 1
        self.satellites = [
            Body("S-01", -24, 9, 0.012, 0.030, "satellite", coverage=0.72),
            Body("S-04", 18, -16, -0.022, 0.014, "satellite", coverage=0.42),
            Body("S-07", 3, 25, -0.026, -0.006, "satellite", coverage=0.68),
        ]
        next_satellite_number = 1
        while len(self.satellites) < 16:
            angle = rng.uniform(0, math.tau)
            radius = rng.uniform(17, 34)
            x, y = radius * math.cos(angle), radius * math.sin(angle)
            if any(math.hypot(x - body.x_km, y - body.y_km) < 5.0 for body in self.satellites):
                continue
            speed = rng.uniform(0.018, 0.033)
            existing_ids = {body.id for body in self.satellites}
            while f"S-{next_satellite_number:02d}" in existing_ids:
                next_satellite_number += 1
            self.satellites.append(Body(f"S-{next_satellite_number:02d}", x, y,
                                        -math.sin(angle) * speed, math.cos(angle) * speed,
                                        "satellite", coverage=rng.uniform(0.4, 0.85)))
            next_satellite_number += 1
        # Start with a safe, observable constellation. The previous seed placed
        # multiple debris objects 1–3 km from satellites at T+0, before the
        # agents could possibly detect or avoid them.
        self.debris: list[Body] = []
        next_debris_number = 1
        while len(self.debris) < 64:
            angle = rng.uniform(0, math.tau)
            radius = rng.uniform(11, 39)
            x, y = radius * math.cos(angle), radius * math.sin(angle)
            if any(math.hypot(x - body.x_km, y - body.y_km) < 1.8 for body in self.debris):
                continue
            if any(math.hypot(x - body.x_km, y - body.y_km) < 8.0 for body in self.satellites):
                continue
            speed = rng.uniform(0.008, 0.026)
            existing_ids = {body.id for body in self.debris}
            while f"D-{next_debris_number:02d}" in existing_ids:
                next_debris_number += 1
            self.debris.append(Body(f"D-{next_debris_number:02d}", x, y,
                                    -math.sin(angle) * speed + rng.uniform(-0.005, 0.005),
                                    math.cos(angle) * speed + rng.uniform(-0.005, 0.005), "debris"))
            next_debris_number += 1

    def tick(self, dt: float) -> None:
        self.time_sec += dt
        for body in self.satellites + self.debris:
            propagate(body, dt)
        for satellite in self.satellites:
            if satellite.alive and any(distance(satellite, debris) < 0.35 for debris in self.debris):
                satellite.alive = False
        if int(self.time_sec // 30) + 1 > self.epoch:
            self.epoch += 1
        for satellite in self.satellites:
            satellite.coverage = max(0.0, min(1.0, 0.55 + 0.35 * math.sin(self.time_sec / 18 + satellite.x_km / 20)))

    def snapshot(self) -> dict:
        def encode(body: Body) -> dict:
            return {"id": body.id, "x": round(body.x_km, 3), "y": round(body.y_km, 3),
                    "vx": round(body.vx_km_s, 4), "vy": round(body.vy_km_s, 4),
                    "fuel": round(body.fuel, 1), "coverage": round(body.coverage, 2), "alive": body.alive}
        return {"time_sec": round(self.time_sec, 1), "epoch": self.epoch,
                "satellites": [encode(x) for x in self.satellites],
                "debris": [encode(x) for x in self.debris]}
