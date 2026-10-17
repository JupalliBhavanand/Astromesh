from __future__ import annotations

import math
import random
from dataclasses import asdict, dataclass

from astromesh.sim.physics import Body, apply_burn, closest_approach


@dataclass(frozen=True)
class TrajectoryPlaybook:
    version: int = 1
    radial_dv_km_s: float = 0.003
    tangential_mix: float = 0.0
    burn_duration_sec: float = 60.0


class EvaluatorAgent:
    """Promote a mutated playbook only after it beats its parent on unseen fixed scenarios."""

    TRAIN_SEEDS = tuple(range(1200, 1224))
    HOLDOUT_SEEDS = tuple(range(91200, 91248))
    CANDIDATE_DV = (0.0045, 0.006, 0.008, 0.010, 0.012)
    CANDIDATE_TANGENTIAL_MIX = (-0.25, 0.0, 0.25)

    def __init__(self) -> None:
        self.active = TrajectoryPlaybook()
        self.last_report = {"version": 1, "baseline_score": 0.0, "candidate_score": 0.0,
                            "delta": 0.0, "promoted": False, "holdout_cases": len(self.HOLDOUT_SEEDS),
                            "note": "Awaiting the first batch of mission outcomes"}
        self.history: list[dict] = []
        self.evaluations = 0

    @staticmethod
    def _score(policy: TrajectoryPlaybook, seeds: tuple[int, ...]) -> tuple[float, int, float]:
        passed = 0
        distances = []
        safe_contracts = True
        for seed in seeds:
            rng = random.Random(seed)
            angle = rng.uniform(0, math.tau)
            gap = rng.uniform(6.2, 11.5)
            approach_speed = rng.uniform(0.012, 0.055)
            tangent = rng.uniform(-0.008, 0.008)
            dx, dy = gap * math.cos(angle), gap * math.sin(angle)
            satellite = Body(f"T-{seed}", 0, 0, 0, 0, "satellite")
            debris = Body(f"D-{seed}", dx, dy,
                          -approach_speed * math.cos(angle) - tangent * math.sin(angle),
                          -approach_speed * math.sin(angle) + tangent * math.cos(angle), "debris")
            rx, ry = -dx / gap, -dy / gap
            tx, ty = -ry, rx
            vx = rx + policy.tangential_mix * tx
            vy = ry + policy.tangential_mix * ty
            norm = math.hypot(vx, vy)
            dvx, dvy = policy.radial_dv_km_s * vx / norm, policy.radial_dv_km_s * vy / norm
            initial_fuel = satellite.fuel
            fuel_delta = apply_burn(satellite, dvx, dvy, policy.burn_duration_sec)
            min_distance = closest_approach(satellite, debris, horizon_sec=180)
            distances.append(min_distance)
            safe = (min_distance >= 5.0 and fuel_delta <= 3.5 and policy.burn_duration_sec <= 120
                    and initial_fuel - satellite.fuel <= 3.5)
            passed += int(safe)
            safe_contracts &= safe
        survival = passed / len(seeds)
        separation_quality = sum(min(distance / 12.0, 1.0) for distance in distances) / len(distances)
        return 100 * (0.8 * survival + 0.2 * separation_quality), passed, sum(distances) / len(distances)

    def evaluate(self, actual_outcomes: list[dict] | None = None) -> dict:
        """Use mission failures to steer training search; promote only on untouched holdout seeds."""
        parent = self.active
        parent_train = self._score(parent, self.TRAIN_SEEDS)[0]
        outcomes = [item for item in (actual_outcomes or []) if item.get("status") in {"done", "stale"}]
        failed_checks = [check for item in outcomes for check in item.get("failed_checks", [])]
        separation_failures = sum(check.startswith("separation:") for check in failed_checks)
        fuel_failures = sum(check == "fuel_budget" for check in failed_checks)
        # Real verifier failures influence which policy variants enter the training
        # race. The independent fixed holdout still decides whether anything ships.
        candidates = [TrajectoryPlaybook(version=parent.version + 1, radial_dv_km_s=dv,
                                         tangential_mix=mix,
                                         burn_duration_sec=parent.burn_duration_sec)
                      for dv in self.CANDIDATE_DV for mix in self.CANDIDATE_TANGENTIAL_MIX
                      if (dv, mix) != (parent.radial_dv_km_s, parent.tangential_mix)]

        def training_score(policy: TrajectoryPlaybook) -> float:
            score = self._score(policy, self.TRAIN_SEEDS)[0]
            if separation_failures:
                score += 1.5 * max(0.0, policy.radial_dv_km_s - parent.radial_dv_km_s) / 0.012
                score += min(abs(policy.tangential_mix), 0.25) * 2
            if fuel_failures:
                score += 1.5 * max(0.0, parent.radial_dv_km_s - policy.radial_dv_km_s) / 0.012
            return score

        candidate = max(candidates, key=training_score)
        candidate_train = self._score(candidate, self.TRAIN_SEEDS)[0]
        parent_holdout, parent_passed, parent_distance = self._score(parent, self.HOLDOUT_SEEDS)
        candidate_holdout, candidate_passed, candidate_distance = self._score(candidate, self.HOLDOUT_SEEDS)
        delta = candidate_holdout - parent_holdout
        promoted = delta >= 0.5 and candidate_passed >= parent_passed
        if promoted:
            self.active = candidate
        self.evaluations += 1
        report = {
            "version": self.active.version,
            "baseline_version": parent.version,
            "baseline_score": round(parent_holdout, 2),
            "candidate_score": round(candidate_holdout, 2),
            "training_baseline": round(parent_train, 2),
            "training_candidate": round(candidate_train, 2),
            "delta": round(delta, 2),
            "promoted": promoted,
            "holdout_cases": len(self.HOLDOUT_SEEDS),
            "holdout_passed_baseline": parent_passed,
            "holdout_passed_candidate": candidate_passed,
            "mean_separation_baseline_km": round(parent_distance, 2),
            "mean_separation_candidate_km": round(candidate_distance, 2),
            "active_radial_dv_km_s": self.active.radial_dv_km_s,
            "note": "Version N+1 promoted on held-out scenarios" if promoted else
                    "Candidate retained as shadow; active playbook unchanged",
            "real_missions_in_batch": len(actual_outcomes or []),
            "real_feedback": {"outcomes_used": len(outcomes),
                              "separation_failures": separation_failures,
                              "fuel_failures": fuel_failures,
                              "training_search_guided": bool(failed_checks)},
        }
        self.last_report = report
        self.history.append(report)
        self.history = self.history[-20:]
        return report

    def snapshot(self) -> dict:
        return {"active": asdict(self.active), "last_report": self.last_report,
                "history": self.history, "evaluations": self.evaluations}
