from astromesh.agents.architect import ManeuverContract
from astromesh.sim.physics import Body, closest_approach


class Verification:
    def __init__(self, approved: bool, min_distance_km: float, fuel_delta: float, reason: str,
                 checks: dict[str, bool], threat_distances: dict[str, float] | None = None) -> None:
        self.approved, self.min_distance_km, self.fuel_delta, self.reason = approved, min_distance_km, fuel_delta, reason
        self.checks = checks
        self.threat_distances = threat_distances or {}

    def model_dump(self) -> dict:
        return {"approved": self.approved, "min_distance_km": round(self.min_distance_km, 2),
                "fuel_delta": round(self.fuel_delta, 3), "reason": self.reason, "checks": self.checks,
                "threat_distances_km": {key: round(value, 2) for key, value in self.threat_distances.items()}}


class VerifierAgent:
    """Independent deterministic safety gate; model output cannot override physics checks."""
    @staticmethod
    def verify(contract: ManeuverContract, satellite: Body, debris: Body | list[Body],
               original_fuel: float, duration_sec: float) -> Verification:
        threats = debris if isinstance(debris, list) else [debris]
        threat_distances = {threat.id: closest_approach(satellite, threat) for threat in threats}
        separation = min(threat_distances.values(), default=float("inf"))
        fuel_delta = original_fuel - satellite.fuel
        checks = {f"separation:{threat_id}": distance >= contract.min_debris_distance_km
                  for threat_id, distance in threat_distances.items()}
        checks.update({"fuel_budget": fuel_delta <= contract.max_fuel_budget_delta,
                  "thrust_duration": duration_sec <= contract.max_thrust_duration_sec,
                  "coverage_floor": not contract.target_coverage_restored or satellite.coverage >= 0.15})
        passed = all(checks.values())
        failures = ", ".join(name for name, ok in checks.items() if not ok)
        return Verification(passed, separation, fuel_delta,
                            "All invariant checks passed" if passed else f"Contract invariant violated: {failures}",
                            checks, threat_distances)
