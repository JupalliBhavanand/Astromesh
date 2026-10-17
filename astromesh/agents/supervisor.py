from __future__ import annotations

import copy
import math
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from astromesh.agents.architect import ArchitectAgent
from astromesh.agents.evaluator import EvaluatorAgent
from astromesh.agents.satellite import ManeuverPlan, SatelliteAgent
from astromesh.agents.verifier import VerifierAgent
from astromesh.memory.shared_brain import OrbitalMemoryGraph
from astromesh.sim.physics import Body, apply_burn, closest_approach


@dataclass
class MissionResult:
    task_id: str
    status: str
    events: list[dict]
    candidate_satellite: Body | None
    attempts: int
    contract: dict


class MissionSupervisor:
    """Own one goal from immutable contract through verified completion or human escalation."""

    def __init__(self, architect: ArchitectAgent, worker: SatelliteAgent,
                 verifier: VerifierAgent, memory: OrbitalMemoryGraph,
                 evaluator: EvaluatorAgent) -> None:
        self.architect, self.worker, self.verifier = architect, worker, verifier
        self.memory, self.evaluator = memory, evaluator
        self.stats = {"missions_started": 0, "missions_completed": 0, "attempts": 0,
                      "replans": 0, "loop_detections": 0, "human_escalations": 0}

    @staticmethod
    def _signature(plan: ManeuverPlan) -> tuple:
        return (round(plan.dvx, 5), round(plan.dvy, 5), round(plan.duration_sec, 2))

    @staticmethod
    def _break_cycle(plan: ManeuverPlan, seen: set[tuple]) -> ManeuverPlan | None:
        for degrees in (22.5, -22.5, 45, -45, 90):
            angle = math.radians(degrees)
            dvx = plan.dvx * math.cos(angle) - plan.dvy * math.sin(angle)
            dvy = plan.dvx * math.sin(angle) + plan.dvy * math.cos(angle)
            alternative = plan.model_copy(update={"dvx": dvx, "dvy": dvy,
                                                  "rationale": f"Supervisor changed repeated vector by {degrees:+g}°"})
            if MissionSupervisor._signature(alternative) not in seen:
                return alternative
        return None

    async def execute(self, satellite: Body, debris: Body | list[Body], sim_time: float,
                      on_stage: Callable[[dict], None] | None = None) -> MissionResult:
        self.stats["missions_started"] += 1
        threats = debris if isinstance(debris, list) else [debris]
        if not threats:
            raise ValueError("A collision-avoidance mission requires at least one threat.")
        primary = threats[0]
        task_id = f"avoid_{satellite.id.lower()}_{primary.id.lower()}_{uuid4().hex[:8]}"
        threat_distances = {threat.id: closest_approach(satellite, threat) for threat in threats}
        predicted_distance = min(threat_distances.values())

        def emit(agent: str, stage: str, message: str, shared: str = "") -> None:
            if on_stage:
                recipient = {
                    "mission_supervisor": "Root Architect · Worker · Shared Memory",
                    "root_architect": "Satellite Worker · Adversarial Verifier",
                    "shared_memory": "Satellite Worker",
                    "satellite_worker": "Adversarial Verifier",
                    "adversarial_verifier": "Mission Supervisor",
                    "thruster_controller": "Flight Dynamics · Shared Memory",
                    "playbook_evaluator": "Mission Supervisor · Shared Memory",
                }.get(agent, "Mission Mesh")
                on_stage({"task_id": task_id, "agent": agent, "stage": stage,
                          "message": message, "shared": shared,
                          "packet": {"from": agent, "to": recipient, "payload": shared} if shared else None,
                          "time": round(sim_time, 1)})

        emit("mission_supervisor", "COLLISION DETECTED",
             f"{satellite.id} has {len(threats)} approaching objects; nearest predicted pass is {predicted_distance:.2f} km.",
             f"Telemetry → Root Architect, Satellite Worker, and shared orbital memory · {', '.join(t.id for t in threats)}")
        emit("root_architect", "DEFINITION OF DONE",
             "Building an immutable maneuver contract before any thruster command.",
             f"Threat set: {satellite.id} / {', '.join(t.id for t in threats)} · nearest predicted separation: {predicted_distance:.2f} km")
        contract = await self.architect.contract(satellite.id, primary.id, [threat.id for threat in threats])
        self.memory.remember_fact(f"safety:{satellite.id}",
                                  f"Minimum separation {contract.min_debris_distance_km:.1f} km; "
                                  f"fuel cap {contract.max_fuel_budget_delta:.1f}; "
                                  f"maximum {contract.max_replans} replans",
                                  "Root Architect contract", 1.0)
        self.memory.flush()
        emit("root_architect", "CONTRACT PUBLISHED",
             f"Minimum separation {contract.min_debris_distance_km:.1f} km · fuel cap {contract.max_fuel_budget_delta:.1f} · "
             f"burn ≤ {contract.max_thrust_duration_sec:.0f}s · up to {contract.max_replans} replans · "
             f"{len(threats)} concurrent threat paths.",
             f"Contract + threat set [{', '.join(t.id for t in threats)}] → Satellite Worker + Adversarial Verifier")
        memory_context = self.memory.prompt_context(satellite.x_km, satellite.y_km)
        context_preview = " · ".join(line.strip() for line in memory_context.splitlines() if line.strip())[:230]
        emit("shared_memory", "CONTEXT RETRIEVED",
             f"Read {len(threats)} threat tracks, {len(self.memory.decisions)} prior outcomes, and {len(self.memory.facts)} curated safety facts.",
             f"Shared threat packet [{', '.join(t.id for t in threats)}] + worker context: {context_preview}")
        seen: set[tuple] = set()
        feedback = "No prior attempt."
        result_events: list[dict] = []
        for attempt in range(contract.max_replans + 1):
            playbook = self.evaluator.active
            emit("satellite_worker", "PLAN THRUSTER BURN",
                 f"Analyzing relative motion and selecting a burn · attempt {attempt + 1}.",
                 f"Inputs: state + contract + shared memory + verifier feedback. {feedback}")
            plan = await self.worker.plan(satellite, primary, playbook, attempt, feedback,
                                          memory_context=memory_context, threats=threats)
            emit("satellite_worker", "BURN PROPOSED",
                 f"Δv=({plan.dvx:+.5f}, {plan.dvy:+.5f}) km/s for {plan.duration_sec:.0f}s · {plan.rationale}",
                 "Plan + predicted trajectory → Adversarial Verifier")
            signature = self._signature(plan)
            if signature in seen:
                self.stats["loop_detections"] += 1
                alternative = self._break_cycle(plan, seen)
                if alternative is None:
                    break
                plan = alternative
                signature = self._signature(plan)
            seen.add(signature)
            candidate = copy.deepcopy(satellite)
            fuel_before = candidate.fuel
            fuel_cost = apply_burn(candidate, plan.dvx, plan.dvy, plan.duration_sec)
            verification = self.verifier.verify(contract, candidate, threats, fuel_before, plan.duration_sec)
            checks = ", ".join(f"{name}={'PASS' if ok else 'FAIL'}" for name, ok in verification.checks.items())
            emit("adversarial_verifier", "INDEPENDENT CHECK",
                 f"Nearest separation {verification.min_distance_km:.2f} km across {len(threats)} objects · fuel {verification.fuel_delta:.3f} · {checks}.",
                 f"Verifier result → Mission Supervisor: {'APPROVED' if verification.approved else 'REJECTED'}")
            self.stats["attempts"] += 1
            if attempt:
                self.stats["replans"] += 1
            event = {
                "time": round(sim_time, 1), "type": "approved" if verification.approved else "rejected",
                "task_id": task_id, "attempt": attempt + 1, "satellite": satellite.id, "debris": primary.id,
                "threat_ids": [threat.id for threat in threats],
                "threat_distances_km": verification.threat_distances,
                "message": verification.reason, "distance_km": verification.min_distance_km,
                "fuel_delta": round(fuel_cost, 4), "dvx": round(plan.dvx, 5), "dvy": round(plan.dvy, 5),
                "duration_sec": plan.duration_sec,
                "rationale": plan.rationale, "checks": verification.checks,
                "playbook_version": playbook.version, "source": "cycle-breaker" if plan.rationale.startswith("Supervisor") else "worker",
            }
            result_events.append(event)
            self.memory.remember_decision(event)
            fact_value = "approved" if verification.approved else f"rejected ({verification.reason})"
            self.memory.remember_fact(f"maneuver:{satellite.id}:{','.join(t.id for t in threats)}", fact_value,
                                      f"Adversarial Verifier · {task_id}",
                                      0.98 if verification.approved else 0.72)
            if verification.approved:
                emit("mission_supervisor", "SNAPSHOT VERIFIED / LIVE CHECK PENDING",
                     "Contract passed on the plan snapshot; the live-state verifier must approve before actuator control.",
                     "Approved plan + Δv + burn duration → live-state verifier · outcome → shared memory")
                return MissionResult(task_id, "done", result_events, candidate, attempt + 1,
                                     contract.model_dump())
            feedback = (f"Attempt {attempt + 1} rejected by independent verifier: {verification.reason}. "
                        f"Closest approaches={verification.threat_distances}; fuel cost={fuel_cost:.3f}. "
                        f"Rejected vector signature={signature}. Produce a materially different safe vector.")
            emit("mission_supervisor", "REPLAN REQUESTED", feedback,
                 "Verifier failure + measured closest approach → Satellite Worker")
            if attempt < contract.max_replans:
                continue

        self.stats["human_escalations"] += 1
        emit("mission_supervisor", "SAFE STOP / HUMAN REVIEW",
             "No burn passed the contract within the replan budget; autonomous control stopped safely.",
             "Failure evidence + attempts → operator and shared memory")
        escalation = {"time": round(sim_time, 1), "type": "needs_human", "task_id": task_id,
                      "satellite": satellite.id, "debris": primary.id,
                      "threat_ids": [threat.id for threat in threats],
                      "message": "Supervisor stopped after the replan budget or a repeated-plan loop. Human review required.",
                      "attempts": len(result_events), "contract": contract.model_dump()}
        result_events.append(escalation)
        self.memory.remember_decision(escalation)
        return MissionResult(task_id, "needs_human", result_events, None, len(result_events) - 1,
                             contract.model_dump())

    def confirm_live_commit(self) -> None:
        self.stats["missions_completed"] += 1
