from pydantic import BaseModel, Field

from astromesh.agents.base_agent import BaseAgent
from astromesh.config import Settings
from astromesh.memory.shared_brain import OrbitalMemoryGraph


class ManeuverContract(BaseModel):
    task_id: str
    satellite_id: str
    debris_id: str
    threat_ids: list[str] = Field(default_factory=list)
    min_debris_distance_km: float = Field(ge=0)
    max_fuel_budget_delta: float = Field(ge=0)
    target_coverage_restored: bool = True
    max_thrust_duration_sec: float = Field(gt=0, le=120)
    max_replans: int = Field(default=3, ge=0, le=5)
    acceptance_checks: list[str] = Field(default_factory=lambda: [
        "Every predicted concurrent debris approach is at least the contracted minimum distance.",
        "Burn fuel delta is within the contracted budget.",
        "Burn duration stays within the contracted limit.",
        "The maneuver plan is independently verified before state is committed.",
    ])


class ArchitectAgent(BaseAgent):
    def __init__(self, settings: Settings, memory: OrbitalMemoryGraph) -> None:
        super().__init__("architect", settings, memory)

    async def contract(self, satellite_id: str, debris_id: str,
                       threat_ids: list[str] | None = None) -> ManeuverContract:
        threat_ids = list(dict.fromkeys(threat_ids or [debris_id]))
        fallback = ManeuverContract(task_id=f"maneuver_{satellite_id.lower()}_{debris_id.lower()}",
                                    satellite_id=satellite_id, debris_id=debris_id,
                                    threat_ids=threat_ids,
                                    min_debris_distance_km=self.settings.minimum_debris_distance_km,
                                    max_fuel_budget_delta=3.5, max_thrust_duration_sec=120,
                                    max_replans=3)
        proposed = await self.structured_call("Create an explicit definition of done for the requested close approach. "
                                              "Keep supplied satellite and threat IDs, all numeric safety thresholds, and max_replans unchanged. "
                                              "Add checkable acceptance checks; do not loosen any invariant. "
                                              f"Return this schema: {fallback.model_dump_json()}", ManeuverContract, fallback)
        # The model may add helpful checks, but the root architect cannot weaken the hard envelope.
        return proposed.model_copy(update={
            "task_id": fallback.task_id, "satellite_id": fallback.satellite_id,
            "debris_id": fallback.debris_id,
            "threat_ids": fallback.threat_ids,
            "min_debris_distance_km": fallback.min_debris_distance_km,
            "max_fuel_budget_delta": fallback.max_fuel_budget_delta,
            "target_coverage_restored": fallback.target_coverage_restored,
            "max_thrust_duration_sec": fallback.max_thrust_duration_sec,
            "max_replans": fallback.max_replans,
        })
