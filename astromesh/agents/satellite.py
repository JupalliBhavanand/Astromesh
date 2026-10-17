import copy
import math

from pydantic import BaseModel, Field

from astromesh.agents.base_agent import BaseAgent
from astromesh.config import Settings
from astromesh.agents.evaluator import TrajectoryPlaybook
from astromesh.memory.shared_brain import OrbitalMemoryGraph
from astromesh.sim.physics import Body, apply_burn, closest_approach


class ManeuverPlan(BaseModel):
    dvx: float = Field(ge=-0.05, le=0.05)
    dvy: float = Field(ge=-0.05, le=0.05)
    duration_sec: float = Field(gt=0, le=120)
    rationale: str


class SatelliteAgent(BaseAgent):
    def __init__(self, settings: Settings, memory: OrbitalMemoryGraph) -> None:
        super().__init__("satellite_worker", settings, memory)

    async def plan(self, satellite: Body, debris: Body, playbook: TrajectoryPlaybook | None = None,
                   attempt: int = 0, feedback: str = "", memory_context: str | None = None,
                   threats: list[Body] | None = None) -> ManeuverPlan:
        playbook = playbook or TrajectoryPlaybook()
        threats = threats or [debris]
        fallback = self.local_plan(satellite, debris, playbook, attempt, threats)
        contacts = "; ".join(f"{threat.id}: {closest_approach(satellite, threat):.2f} km"
                              for threat in threats)
        prompt = (f"Plan one compound collision-avoidance burn for {satellite.id} against all concurrent contacts: "
                  f"{contacts}. The burn must preserve the minimum separation for every object. "
                  f"This is plan attempt {attempt + 1}. {feedback} "
                  "Do not repeat a rejected vector. Prioritize the verifier feedback and preserve all contract limits. "
                  f"Active playbook: v{playbook.version}, radial delta-v {playbook.radial_dv_km_s:.4f} km/s. "
                  "Return bounded dvx, dvy in km/s, duration_sec, and a concise rationale.")
        plan = await self.structured_call(prompt, ManeuverPlan, fallback,
                                          memory_context=memory_context if memory_context is not None else
                                          self.memory.prompt_context(satellite.x_km, satellite.y_km))
        return self._safety_shield(satellite, debris, plan, threats=threats)

    @staticmethod
    def local_plan(satellite: Body, debris: Body, playbook: TrajectoryPlaybook | None = None,
                   attempt: int = 0, threats: list[Body] | None = None) -> ManeuverPlan:
        """Return an immediate, verifier-compatible plan without waiting for inference."""
        playbook = playbook or TrajectoryPlaybook()
        threats = threats or [debris]
        # Replans rotate the burn vector so a repeated rejected plan cannot loop unchanged.
        # Combine the repulsion directions from all concurrent contacts. The
        # deterministic shield below optimizes and verifies the final vector.
        aggregate_x = aggregate_y = 0.0
        for threat in threats:
            tx, ty = satellite.x_km - threat.x_km, satellite.y_km - threat.y_km
            distance = max(math.hypot(tx, ty), 0.001)
            weight = 1.0 / (closest_approach(satellite, threat) + 1.0)
            aggregate_x += tx / distance * weight
            aggregate_y += ty / distance * weight
        aggregate_norm = max(math.hypot(aggregate_x, aggregate_y), 0.001)
        radial_x, radial_y = aggregate_x / aggregate_norm, aggregate_y / aggregate_norm
        tangent_x, tangent_y = -radial_y, radial_x
        offsets = (0.0, 0.45, -0.45, 0.9)
        offset = offsets[min(attempt, len(offsets) - 1)]
        vx, vy = radial_x + playbook.tangential_mix * tangent_x + offset * tangent_x, \
            radial_y + playbook.tangential_mix * tangent_y + offset * tangent_y
        direction_norm = max((vx * vx + vy * vy) ** 0.5, 0.001)
        fallback = ManeuverPlan(dvx=playbook.radial_dv_km_s * vx / direction_norm,
                                dvy=playbook.radial_dv_km_s * vy / direction_norm,
                                duration_sec=playbook.burn_duration_sec,
                                rationale=f"Replan {attempt + 1}: separation vector guided by active playbook v{playbook.version}")
        return fallback

    @staticmethod
    def _safety_shield(satellite: Body, debris: Body, proposed: ManeuverPlan,
                       threats: list[Body] | None = None) -> ManeuverPlan:
        """Choose one low-impulse burn that clears every contact in the physics model."""
        threats = threats or [debris]
        minimum = 5.5  # margin for telemetry rounding and live-state drift
        candidates = [proposed]
        for magnitude_step in range(1, 48):
            magnitude = magnitude_step * 0.0015
            for direction_step in range(72):
                angle = math.tau * direction_step / 72
                dvx, dvy = magnitude * math.cos(angle), magnitude * math.sin(angle)
                if abs(dvx) > 0.05 or abs(dvy) > 0.05:
                    continue
                candidates.append(ManeuverPlan(
                    dvx=dvx, dvy=dvy,
                    duration_sec=60.0,
                    rationale="Physics safety shield selected a minimum-impulse thruster vector"))

        safe: list[tuple[float, float, ManeuverPlan]] = []
        baseline = min(closest_approach(satellite, threat) for threat in threats)
        best: tuple[float, ManeuverPlan] | None = None
        for plan in candidates:
            candidate = copy.deepcopy(satellite)
            cost = apply_burn(candidate, plan.dvx, plan.dvy, plan.duration_sec)
            separations = [closest_approach(candidate, threat) for threat in threats]
            minimum_separation = min(separations)
            if best is None or minimum_separation > best[0]:
                best = (minimum_separation, plan)
            if all(separation >= minimum for separation in separations) and cost <= 3.5:
                safe.append((math.hypot(plan.dvx, plan.dvy), -minimum_separation, plan))
        if safe:
            # Lowest fuel/impulse wins; among ties choose more separation.
            selected = min(safe, key=lambda item: (round(item[0], 8), item[1]))[2]
            return selected
        return best[1] if best is not None and best[0] > baseline else proposed
