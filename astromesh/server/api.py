from __future__ import annotations

import asyncio
import copy
import hashlib
import math
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from astromesh.agents.architect import ArchitectAgent, ManeuverContract
from astromesh.agents.evaluator import EvaluatorAgent
from astromesh.agents.satellite import SatelliteAgent
from astromesh.agents.supervisor import MissionSupervisor
from astromesh.agents.verifier import VerifierAgent
from astromesh.config import get_settings
from astromesh.memory.shared_brain import OrbitalMemoryGraph
from astromesh.sim.environment import SpaceEnvironment
from astromesh.sim.physics import Body, apply_burn, closest_approach, predict_encounters, propagate

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
settings = get_settings()
world = SpaceEnvironment(settings.random_seed)
memory = OrbitalMemoryGraph()
architect = ArchitectAgent(settings, memory)
worker = SatelliteAgent(settings, memory)
verifier = VerifierAgent()
evaluator = EvaluatorAgent()
supervisor = MissionSupervisor(architect, worker, verifier, memory, evaluator)
test_supervisor = MissionSupervisor(architect, worker, verifier, memory, evaluator)
events: deque[dict] = deque(maxlen=150)
outcome_batch: list[dict] = []
pending_humans: dict[str, dict] = {}
human_retries: deque[tuple[str, list[str]]] = deque()
metrics = {
    "token_total": 0, "cached_tokens": 0, "latency_ms": None, "maneuvers": 0,
    "approved": 0, "rejected_attempts": 0, "stale_rejections": 0,
    "playbook_version": 1, "playbook_score": 0.0, "playbook_delta": 0.0,
    "promotions": 0, "provider": settings.llm_provider, "model": settings.llm_model,
    "mode": "CRUSOE KEY REQUIRED · add CRUSOE_API_KEY to .env" if not settings.inference_api_key else "CRUSOE READY",
    "runtime_mode": settings.runtime_mode,
}
lock = asyncio.Lock()
last_maneuver_time = -999.0
last_memory_flush_time = 0.0
stale_counts: dict[tuple[str, str], int] = {}
mission_in_flight = False
test_cases_running = False
test_overlay: dict | None = None
test_case_report: dict = {"running": False, "total": 0, "passed": 0, "cases": []}
mission_flow: deque[dict] = deque(maxlen=80)
mission_stage: dict | None = None


def record_mission_stage(entry: dict) -> None:
    global mission_stage
    flow_event = {**entry, "time": entry.get("time", round(world.time_sec, 1))}
    if flow_event.get("shared") and not flow_event.get("packet"):
        agent = flow_event.get("agent", "mission_mesh")
        recipient = {
            "thruster_controller": "Flight Dynamics · Shared Memory",
            "adversarial_verifier": "Mission Supervisor",
            "playbook_evaluator": "Mission Supervisor · Shared Memory",
            "mission_supervisor": "Mission Mesh",
        }.get(agent, "Mission Mesh")
        flow_event["packet"] = {"from": agent, "to": recipient, "payload": flow_event["shared"]}
    mission_stage = flow_event
    mission_flow.appendleft(flow_event)


def state() -> dict:
    alive = sum(s.alive for s in world.satellites)
    snapshot = world.snapshot()
    snapshot["events"] = list(events)
    snapshot["metrics"] = {
        **metrics,
        "inference_status": ("key_missing" if not settings.inference_api_key else
                             "error" if any(a.inference_status == "error" for a in (architect, worker)) else
                             "rate_limited" if any(a.inference_status == "rate_limited" for a in (architect, worker)) else
                             "degraded" if any(a.inference_status == "degraded" for a in (architect, worker)) else
                             "connected" if any(a.successful_requests for a in (architect, worker)) else
                             "ready"),
        "inference_requests": sum(a.request_count for a in (architect, worker)),
        "inference_successes": sum(a.successful_requests for a in (architect, worker)),
        "inference_last_success_at": max((a.last_success_at for a in (architect, worker)
                                           if a.last_success_at is not None), default=None),
        "latency_ms": (round(sum(a.total_latency_ms for a in (architect, worker)) /
                              max(sum(a.request_count for a in (architect, worker)), 1), 1)
                       if sum(a.request_count for a in (architect, worker)) else None),
        "inference_error": next((a.last_error for a in (worker, architect) if a.last_error), None),
        "survival_rate": round(alive / max(len(world.satellites), 1) * 100),
        "fuel_remaining": round(sum(s.fuel for s in world.satellites) / max(len(world.satellites), 1), 1),
        "avoidance_rate": round(metrics["approved"] / max(metrics["maneuvers"], 1) * 100),
        "satellite_count": len(world.satellites), "debris_count": len(world.debris),
    }
    snapshot["memory"] = memory.metrics()
    snapshot["supervisor"] = {**supervisor.stats, "pending_human_reviews": len(pending_humans)}
    snapshot["playbook"] = evaluator.snapshot()
    snapshot["pending_human_reviews"] = list(pending_humans.values())
    snapshot["test_cases"] = copy.deepcopy(test_case_report)
    snapshot["test_overlay"] = copy.deepcopy(test_overlay)
    snapshot["mission_flow"] = list(mission_flow)[:24]
    snapshot["mission_stage"] = copy.deepcopy(mission_stage)
    return snapshot


class HumanDecision(BaseModel):
    action: Literal["retry", "dismiss"]
    note: str = Field(default="", max_length=500)


class InferenceProbeResult(BaseModel):
    status: Literal["ok"]
    message: str


def _next_threat() -> tuple[Body, list[Body]] | None:
    if human_retries:
        satellite_id, debris_ids = human_retries.popleft()
        satellite = next((s for s in world.satellites if s.id == satellite_id and s.alive), None)
        threats = [d for d in world.debris if d.id in set(debris_ids)]
        if satellite and threats:
            return copy.deepcopy(satellite), copy.deepcopy(threats)
    if world.time_sec - last_maneuver_time < 4:
        return None
    candidates: list[tuple[float, Body, list[Body]]] = []
    for sat in world.satellites:
        if not sat.alive:
            continue
        contacts = predict_encounters(sat, world.debris, horizon_sec=300, threshold_km=10.0)
        if contacts and min(item["distance_km"] for item in contacts) < settings.minimum_debris_distance_km:
            candidates.append((min(item["distance_km"] for item in contacts), sat,
                               [item["debris"] for item in contacts]))
    if not candidates:
        return None
    _, satellite, threats = min(candidates, key=lambda x: x[0])
    return copy.deepcopy(satellite), copy.deepcopy(threats)


async def physics_loop() -> None:
    """Keep the environment advancing while long-running inference is in flight."""
    global last_memory_flush_time
    while True:
        await asyncio.sleep(settings.tick_interval_sec)
        async with lock:
            world.tick(settings.tick_interval_sec)
            for body in world.satellites + world.debris:
                memory.observe(body.id, body.x_km, body.y_km, body.vx_km_s, body.vy_km_s,
                               0.99 if body.kind == "satellite" else 0.9)
            if world.time_sec - last_memory_flush_time >= 10:
                memory.flush()
                last_memory_flush_time = world.time_sec


def apply_benchmark_report(report: dict, sim_time: float) -> dict:
    metrics["playbook_version"] = report["version"]
    metrics["playbook_score"] = report["candidate_score"] if report["promoted"] else report["baseline_score"]
    metrics["playbook_delta"] = report["delta"]
    metrics["promotions"] += int(report["promoted"])
    memory.remember_fact("active_playbook", str(evaluator.snapshot()["active"]), "Held-out benchmark", 1.0)
    event = {"time": round(sim_time, 1), "type": "playbook", "message": report["note"],
             "task_id": f"evaluation-{evaluator.evaluations}",
             "baseline_score": report["baseline_score"], "candidate_score": report["candidate_score"],
             "delta": report["delta"], "promoted": report["promoted"],
             "version": report["version"], "holdout_cases": report["holdout_cases"],
             "holdout_passed_baseline": report["holdout_passed_baseline"],
             "holdout_passed_candidate": report["holdout_passed_candidate"]}
    events.appendleft(event)
    memory.remember_decision(event)
    record_mission_stage({"time": round(sim_time, 1), "agent": "playbook_evaluator",
                          "stage": "HELD-OUT CANDIDATE REVIEW",
                          "message": (f"Candidate score {report['candidate_score']:.1f} vs baseline "
                                     f"{report['baseline_score']:.1f} · {report['holdout_passed_candidate']}/"
                                     f"{report['holdout_cases']} unseen cases."),
                          "shared": f"Decision: {'promoted' if report['promoted'] else 'kept in shadow'} · "
                                    f"delta {report['delta']:+.1f} → active playbook and shared memory"})
    return event


async def agent_loop() -> None:
    """Plan outside the world lock, then revalidate against live telemetry before commit."""
    global last_maneuver_time, mission_in_flight
    while True:
        await asyncio.sleep(min(0.25, settings.tick_interval_sec))
        async with lock:
            selected = None if test_cases_running else _next_threat()
            sim_time = world.time_sec
            if selected is not None:
                last_maneuver_time = sim_time
                mission_in_flight = True
        if selected is None:
            continue

        proposed_satellite, proposed_threats = selected
        proposed_debris = proposed_threats[0]
        try:
            mission = await supervisor.execute(proposed_satellite, proposed_threats, sim_time,
                                               on_stage=record_mission_stage)
        except Exception as exc:
            mission = None
            task_id = f"supervisor-error-{int(sim_time * 10)}"
            failure = {"time": round(sim_time, 1), "type": "needs_human", "task_id": task_id,
                       "satellite": proposed_satellite.id, "debris": proposed_debris.id,
                       "threat_ids": [threat.id for threat in proposed_threats],
                       "message": f"Agent pipeline failed closed ({type(exc).__name__}); operator review required."}
            result_events = [failure]
            memory.remember_decision(failure)
        else:
            result_events = [dict(event) for event in mission.events]

        async with lock:
            if mission is None:
                pending_humans[task_id] = {"task_id": task_id, "satellite": proposed_satellite.id,
                                           "debris": proposed_debris.id,
                                           "threat_ids": [threat.id for threat in proposed_threats],
                                           "message": result_events[0]["message"],
                                           "attempts": 0}
            elif mission.status == "done" and mission.candidate_satellite is not None:
                # The simulation continued while the model planned. Reapply its delta-v to the
                # latest state and rerun the independent verifier before changing the world.
                live_satellite = next((s for s in world.satellites if s.id == proposed_satellite.id), None)
                live_threats = [next((d for d in world.debris if d.id == threat.id), None)
                                for threat in proposed_threats]
                if (live_satellite is not None and all(live_threats) and live_satellite.alive):
                    live_threats = [threat for threat in live_threats if threat is not None]
                    dvx = mission.candidate_satellite.vx_km_s - proposed_satellite.vx_km_s
                    dvy = mission.candidate_satellite.vy_km_s - proposed_satellite.vy_km_s
                    duration = result_events[-1]["duration_sec"]
                    candidate = copy.deepcopy(live_satellite)
                    fuel_before = candidate.fuel
                    fuel_cost = apply_burn(candidate, dvx, dvy, duration)
                    contract = ManeuverContract.model_validate(mission.contract)
                    live_check = verifier.verify(contract, candidate, live_threats, fuel_before, duration)
                    live_source = "live-state-verifier"
                    if not live_check.approved:
                        # Model latency can make a verified snapshot stale. Replan
                        # immediately from current telemetry, then run the same hard gate.
                        record_mission_stage({"task_id": mission.task_id, "agent": "satellite_worker",
                                              "stage": "LIVE-STATE REPLAN",
                                              "message": f"Snapshot burn became stale ({live_check.reason}); computing a fast local plan from current telemetry.",
                                              "shared": "Latest satellite + all threat states → local safety optimizer → independent verifier"})
                        live_plan = worker.local_plan(live_satellite, live_threats[0], evaluator.active,
                                                      mission.attempts, live_threats)
                        live_plan = worker._safety_shield(live_satellite, live_threats[0],
                                                          live_plan, threats=live_threats)
                        dvx, dvy, duration = live_plan.dvx, live_plan.dvy, live_plan.duration_sec
                        candidate = copy.deepcopy(live_satellite)
                        fuel_before = candidate.fuel
                        fuel_cost = apply_burn(candidate, dvx, dvy, duration)
                        live_check = verifier.verify(contract, candidate, live_threats, fuel_before, duration)
                        live_source = "live-state-replan"
                    plan_event = result_events[-1]
                    plan_event["type"] = "plan_verified"
                    plan_event["message"] = ("Snapshot plan passed; current-state replan also passed verification."
                                              if live_source == "live-state-replan" and live_check.approved else
                                              "Snapshot verification passed; checking the live state before commit.")
                    result_events.append({"time": round(world.time_sec, 1),
                                          "type": "approved" if live_check.approved else "rejected",
                                          "task_id": mission.task_id, "attempt": mission.attempts,
                                          "satellite": live_satellite.id, "debris": proposed_debris.id,
                                          "threat_ids": [threat.id for threat in live_threats],
                                          "threat_distances_km": live_check.threat_distances,
                                          "message": ("Live-state invariants passed; maneuver committed" if live_check.approved
                                                      else f"Live-state recheck rejected stale plan: {live_check.reason}"),
                                          "distance_km": live_check.min_distance_km,
                                          "fuel_delta": round(fuel_cost, 4), "dvx": round(dvx, 5), "dvy": round(dvy, 5),
                                          "checks": live_check.checks, "playbook_version": evaluator.active.version,
                                          "source": live_source})
                    if live_check.approved:
                        live_satellite.vx_km_s, live_satellite.vy_km_s = candidate.vx_km_s, candidate.vy_km_s
                        live_satellite.fuel = candidate.fuel
                        supervisor.confirm_live_commit()
                        memory.remember_decision(result_events[-1])
                        memory.remember_fact(f"maneuver:{live_satellite.id}:{','.join(t.id for t in live_threats)}", "committed",
                                             f"Live-state verifier · {mission.task_id}", 1.0)
                        record_mission_stage({"task_id": mission.task_id, "agent": "thruster_controller",
                                              "stage": "THRUSTERS FIRED / PATH CHANGED",
                                              "message": f"{live_satellite.id} executed compound Δv=({dvx:+.5f}, {dvy:+.5f}) km/s; live-state check cleared {len(live_threats)} threats.",
                                              "shared": f"New velocity + remaining fuel broadcast to flight dynamics and shared memory · {live_satellite.fuel:.1f}% fuel"})
                        outcome_batch.append({"task_id": mission.task_id, "status": "done",
                                              "attempts": mission.attempts,
                                              "separation_km": live_check.min_distance_km,
                                              "failed_checks": [name for name, passed in live_check.checks.items() if not passed],
                                              "fuel_delta": round(fuel_cost, 4),
                                              "playbook_version": evaluator.active.version})
                        stale_counts.pop((live_satellite.id, proposed_debris.id), None)
                    else:
                        record_mission_stage({"task_id": mission.task_id, "agent": "adversarial_verifier",
                                              "stage": "LIVE-STATE CHECK REJECTED",
                                              "message": f"Thrusters stayed off: current telemetry no longer satisfies the contract ({live_check.reason}).",
                                              "shared": "Rejection + latest state → Mission Supervisor for replan or safe stop"})
                        metrics["stale_rejections"] += 1
                        key = (live_satellite.id, proposed_debris.id)
                        stale_counts[key] = stale_counts.get(key, 0) + 1
                        if stale_counts[key] >= 2:
                            supervisor.stats["human_escalations"] += 1
                            escalation = {"time": round(world.time_sec, 1), "type": "needs_human",
                                          "task_id": mission.task_id, "satellite": live_satellite.id,
                                          "debris": proposed_debris.id,
                                          "message": "Two consecutive plans went stale before commit; operator review required.",
                                          "attempts": mission.attempts}
                            result_events.append(escalation)
                            pending_humans[mission.task_id] = escalation
                        outcome_batch.append({"task_id": mission.task_id, "status": "stale",
                                              "attempts": mission.attempts,
                                              "separation_km": live_check.min_distance_km,
                                              "failed_checks": [name for name, passed in live_check.checks.items() if not passed],
                                              "fuel_delta": round(fuel_cost, 4),
                                              "playbook_version": evaluator.active.version})
                else:
                    record_mission_stage({"task_id": mission.task_id, "agent": "mission_supervisor",
                                          "stage": "SAFE STOP / TARGET CHANGED",
                                          "message": "Satellite or debris changed state before the live commit; no burn was issued.",
                                          "shared": "State mismatch → mission log and shared memory"})
                    result_events.append({"time": round(world.time_sec, 1), "type": "rejected",
                                          "task_id": mission.task_id, "satellite": proposed_satellite.id,
                                          "debris": proposed_debris.id,
                                          "message": "Satellite or debris changed state before live commit."})
            elif mission is not None:
                pending = {"task_id": mission.task_id, "satellite": proposed_satellite.id,
                           "debris": proposed_debris.id,
                           "threat_ids": [threat.id for threat in proposed_threats],
                           "message": result_events[-1]["message"],
                           "attempts": mission.attempts}
                pending_humans[mission.task_id] = pending
                outcome_batch.append({"task_id": mission.task_id, "status": mission.status,
                                      "attempts": mission.attempts})

            for event in result_events:
                events.appendleft(event)
            metrics["maneuvers"] = supervisor.stats["missions_started"]
            metrics["approved"] = supervisor.stats["missions_completed"]
            metrics["rejected_attempts"] = supervisor.stats["attempts"] - metrics["approved"]
            metrics["token_total"] = architect.total_tokens + worker.total_tokens
            metrics["cached_tokens"] = architect.cached_tokens + worker.cached_tokens
            request_count = sum(agent.request_count for agent in (architect, worker))
            metrics["latency_ms"] = (round(sum(agent.total_latency_ms for agent in (architect, worker)) /
                                             request_count, 1) if request_count else None)
            if not settings.inference_api_key:
                metrics["mode"] = "CRUSOE KEY REQUIRED · add CRUSOE_API_KEY to .env"
            elif any(agent.inference_status == "rate_limited" for agent in (architect, worker)):
                limited = next((agent.last_error for agent in (worker, architect)
                                if agent.inference_status == "rate_limited" and agent.last_error),
                               "provider asked the agent mesh to back off")
                metrics["mode"] = f"CRUSOE RATE LIMITED · {limited}"
            elif any(agent.inference_status == "degraded" for agent in (architect, worker)):
                slow = next((agent.last_error for agent in (worker, architect)
                             if agent.inference_status == "degraded" and agent.last_error),
                            "inference timed out; local safety planner remains active")
                metrics["mode"] = f"CRUSOE DEGRADED · {slow}"
            elif any(agent.inference_status == "error" for agent in (architect, worker)):
                failed = next((agent.last_error for agent in (worker, architect) if agent.last_error), "request failed")
                metrics["mode"] = f"CRUSOE ERROR · {failed}"
            elif any(agent.successful_requests for agent in (architect, worker)):
                metrics["mode"] = "CRUSOE CONNECTED · NEMOTRON ACTIVE"
            else:
                metrics["mode"] = "CRUSOE READY · WAITING FOR THREAT"
            if len(outcome_batch) >= 3:
                apply_benchmark_report(evaluator.evaluate(outcome_batch[-3:]), world.time_sec)
                outcome_batch.clear()
            memory.flush()
            mission_in_flight = False


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(physics_loop()), asyncio.create_task(agent_loop())]
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    await architect.close()
    await worker.close()
    memory.flush()


app = FastAPI(title="AstroMesh · Recursive Agent Control", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def disable_dashboard_caching(request, call_next):
    """Keep the dashboard and live state fresh without requiring a hard refresh."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(("/assets/", "/api/")):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount("/assets", StaticFiles(directory=FRONTEND), name="assets")


@app.get("/")
async def dashboard():
    return FileResponse(FRONTEND / "index.html")


@app.get("/api/build")
async def get_build():
    """Expose a small source fingerprint so an open dev dashboard reloads on edits."""
    digest = hashlib.sha256()
    for name in ("index.html", "app.js", "scene3d.js", "styles.css", "scene3d.css"):
        path = FRONTEND / name
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return {"build": digest.hexdigest()[:16]}


@app.get("/api/state")
async def get_state():
    async with lock:
        return state()


@app.get("/api/benchmark")
async def get_benchmark():
    return evaluator.snapshot()


@app.post("/api/inference/probe")
async def probe_inference():
    """Make one tiny schema-checked call so operators can verify live Crusoe routing."""
    if not settings.inference_api_key:
        raise HTTPException(status_code=503, detail="Crusoe API key is not configured")
    if mission_in_flight:
        raise HTTPException(status_code=409, detail="Wait for the active avoidance mission before probing inference")
    previous_successes = worker.successful_requests
    await worker.structured_call(
        "Connectivity check: return status=ok and message='Nemotron is reachable'. Do not use tools or discuss telemetry.",
        InferenceProbeResult,
        InferenceProbeResult(status="ok", message="local fallback; provider was not reached"),
        memory_context="This is a provider connectivity probe. No mission data is included.",
    )
    if worker.successful_requests == previous_successes:
        status_code = 429 if worker.inference_status == "rate_limited" else 502
        raise HTTPException(status_code=status_code, detail=worker.last_error or "Provider did not return a valid response")
    return {"ok": True, "provider": settings.llm_provider, "model": settings.llm_model,
            "latency_ms": worker.last_latency_ms, "successes": worker.successful_requests,
            "last_success_at": worker.last_success_at,
            "message": "Live response parsed against the probe schema"}


@app.post("/api/benchmark/run")
async def run_benchmark():
    if mission_in_flight:
        raise HTTPException(status_code=409, detail="Wait for the current mission to finish before evaluating.")
    async with lock:
        report = evaluator.evaluate(outcome_batch[-3:])
        apply_benchmark_report(report, world.time_sec)
        outcome_batch.clear()
        return report


def _scenario_body_snapshot(body: Body) -> dict:
    return {"id": body.id, "x": round(body.x_km, 3), "y": round(body.y_km, 3),
            "vx": round(body.vx_km_s, 4), "vy": round(body.vy_km_s, 4),
            "fuel": round(body.fuel, 1), "coverage": round(body.coverage, 2), "alive": body.alive}


def _approach_view(satellite: Body, debris: Body) -> dict:
    rx, ry = satellite.x_km - debris.x_km, satellite.y_km - debris.y_km
    vx, vy = satellite.vx_km_s - debris.vx_km_s, satellite.vy_km_s - debris.vy_km_s
    speed_sq = vx * vx + vy * vy
    closest_time = 0.0 if speed_sq <= 1e-18 else max(0.0, min(300.0, -(rx * vx + ry * vy) / speed_sq))
    steps = 18
    path = [{"x": round(debris.x_km + debris.vx_km_s * closest_time * i / steps, 3),
             "y": round(debris.y_km + debris.vy_km_s * closest_time * i / steps, 3)}
            for i in range(steps + 1)]
    sx, sy = satellite.x_km + satellite.vx_km_s * closest_time, satellite.y_km + satellite.vy_km_s * closest_time
    dx, dy = debris.x_km + debris.vx_km_s * closest_time, debris.y_km + debris.vy_km_s * closest_time
    return {"debris_id": debris.id, "path": path, "impact_x": round((sx + dx) / 2, 3), "impact_y": round((sy + dy) / 2, 3),
            "closest_in_sec": round(closest_time, 1), "predicted_distance_km": round(closest_approach(satellite, debris), 2)}


@app.post("/api/test-cases/run")
async def run_collision_test_cases():
    """Run a deterministic collision suite through the same agents and independent verifier."""
    global test_cases_running, test_overlay, test_case_report
    if mission_in_flight or test_cases_running:
        raise HTTPException(status_code=409, detail="A mission or collision test run is already in progress.")

    test_cases_running = True
    cases: list[dict] = []
    # Start outside the rendered Earth at a representative orbital radius so the demo craft is visible.
    test_satellite = Body("TEST-SIM-01", 24.0, 0.0, 0.0, 0.02, "satellite", coverage=0.8)
    test_case_report = {"running": True, "total": 8, "passed": 0, "cases": []}
    try:
        for index in range(8):
            satellite = test_satellite
            threats = []
            for threat_index in range(3):
                angle = index * (math.tau / 8) + 0.17 + (threat_index - 1) * 0.19
                radius = 12.0 + threat_index * 2.4
                speed = 0.050 + index * 0.002 + threat_index * 0.003
                # Three independently approaching objects cross the same craft together.
                miss_distance = 0.05 + index * 0.02 + threat_index * 0.025
                ux, uy = math.cos(angle), math.sin(angle)
                px, py = -uy, ux
                cross_speed = speed * miss_distance / radius
                threats.append(Body(f"TEST-D{index + 1:02d}-{threat_index + 1}",
                                    satellite.x_km + radius * ux, satellite.y_km + radius * uy,
                                    satellite.vx_km_s - speed * ux + cross_speed * px,
                                    satellite.vy_km_s - speed * uy + cross_speed * py, "debris"))
            debris = threats[0]
            threat_distances_before = {threat.id: closest_approach(satellite, threat) for threat in threats}
            predicted_before = min(threat_distances_before.values())
            async with lock:
                test_overlay = {"satellite": _scenario_body_snapshot(satellite),
                                "debris": _scenario_body_snapshot(debris), "phase": "DETECTION",
                                "threats": [_scenario_body_snapshot(threat) for threat in threats],
                                "case": index + 1, "total": 8,
                                "approaches": [_approach_view(satellite, threat) for threat in threats],
                                "approach": _approach_view(satellite, debris)}
                test_case_report = {"running": True, "total": 8, "passed": len([c for c in cases if c["passed"]]),
                                    "cases": list(cases), "active_case": index + 1}

            await asyncio.sleep(0.55)  # allow at least one telemetry frame to render the inbound threat
            mission = await test_supervisor.execute(copy.deepcopy(satellite), copy.deepcopy(threats), float(index),
                                                    on_stage=lambda update, case=index + 1:
                                                    record_mission_stage({**update, "case": case}))
            passed = False
            action = None
            predicted_after = predicted_before
            if mission.status == "done" and mission.candidate_satellite is not None:
                dvx = mission.candidate_satellite.vx_km_s - satellite.vx_km_s
                dvy = mission.candidate_satellite.vy_km_s - satellite.vy_km_s
                duration = mission.events[-1]["duration_sec"]
                candidate = copy.deepcopy(satellite)
                fuel_before = candidate.fuel
                fuel_cost = apply_burn(candidate, dvx, dvy, duration)
                contract = ManeuverContract.model_validate(mission.contract)
                verification = verifier.verify(contract, candidate, threats, fuel_before, duration)
                predicted_after = verification.min_distance_km
                passed = verification.approved and all(distance < 0.35 for distance in threat_distances_before.values())
                record_mission_stage({"task_id": mission.task_id, "case": index + 1,
                                      "agent": "adversarial_verifier", "stage": "TEST-FLIGHT FINAL CHECK",
                                      "message": f"Rechecked the applied thruster vector against the contract · {verification.reason}.",
                                      "shared": f"Checks: {verification.checks}"})
                action = {"dvx": round(dvx, 5), "dvy": round(dvy, 5), "duration_sec": duration,
                          "fuel_delta": round(fuel_cost, 4), "checks": verification.checks,
                          "threat_distances_km": verification.threat_distances}
                # Project the whole threat cluster after the compound burn.
                propagate(candidate, 120.0)
                projected_threats = [copy.deepcopy(threat) for threat in threats]
                for projected in projected_threats:
                    propagate(projected, 120.0)
                projected_debris = projected_threats[0]
                event = {"time": round(world.time_sec, 1), "type": "test_case", "source": "collision-suite",
                         "task_id": mission.task_id, "case": index + 1, "satellite": satellite.id,
                         "debris": debris.id, "threat_ids": [threat.id for threat in threats],
                         "threat_distances_km": verification.threat_distances, "passed": passed,
                          "message": "Three simultaneous collisions detected; one compound burn passed every safety check" if passed
                                    else "Compound burn failed the independent safety gate",
                         "distance_before_km": round(predicted_before, 2),
                         "distance_km": round(predicted_after, 2), **action}
                async with lock:
                    test_overlay = {"satellite": _scenario_body_snapshot(candidate),
                                    "debris": _scenario_body_snapshot(projected_debris),
                                    "threats": [_scenario_body_snapshot(threat) for threat in projected_threats],
                                    "phase": "PATH AVOIDED" if passed else "BURN REJECTED",
                                    "case": index + 1, "total": 8, "event": event,
                                    "approaches": [_approach_view(candidate, threat) for threat in projected_threats],
                                    "approach": _approach_view(candidate, projected_debris)}
                test_satellite = candidate
                record_mission_stage({"task_id": mission.task_id, "case": index + 1,
                                      "agent": "thruster_controller", "stage": "THRUSTERS FIRED / PATH CHANGED",
                                      "message": f"{satellite.id} changed course after case {index + 1}; predicted separation is now {predicted_after:.2f} km.",
                                      "shared": f"Updated velocity + fuel state broadcast to shared memory; sequencing next debris encounter."})
            else:
                event = {"time": round(world.time_sec, 1), "type": "test_case", "source": "collision-suite",
                         "task_id": mission.task_id, "case": index + 1, "satellite": satellite.id,
                         "debris": debris.id, "threat_ids": [threat.id for threat in threats],
                         "passed": False, "message": "Agent stopped safely; no compound burn passed verification",
                         "distance_before_km": round(predicted_before, 2), "distance_km": round(predicted_after, 2)}
                async with lock:
                    test_overlay = {"satellite": _scenario_body_snapshot(satellite),
                                    "debris": _scenario_body_snapshot(debris), "phase": "BURN REJECTED",
                                    "threats": [_scenario_body_snapshot(threat) for threat in threats],
                                    "case": index + 1, "total": 8,
                                    "approaches": [_approach_view(satellite, threat) for threat in threats],
                                    "approach": _approach_view(satellite, debris)}

            case_result = {"case": index + 1, "detected": predicted_before < 0.35, "passed": passed,
                           "distance_before_km": round(predicted_before, 2),
                           "distance_after_km": round(predicted_after, 2), "satellite": satellite.id,
                           "debris": debris.id, "threat_ids": [threat.id for threat in threats],
                           "threat_count": len(threats), "threat_distances_before_km": threat_distances_before,
                           "action": action, "message": event["message"]}
            cases.append(case_result)
            events.appendleft(event)
            memory.remember_decision(event)
            async with lock:
                test_case_report = {"running": True, "total": 8,
                                    "passed": sum(int(c["passed"]) for c in cases),
                                    "cases": list(cases), "active_case": index + 1}
            await asyncio.sleep(0.55)  # show the burn and updated flight path before the next threat

        test_case_report = {"running": False, "total": 8,
                            "passed": sum(int(c["passed"]) for c in cases), "cases": cases}
        record_mission_stage({"agent": "mission_supervisor", "stage": "FLIGHT SEQUENCE COMPLETE",
                              "message": f"Autonomous test flight completed {test_case_report['passed']}/8 compound multi-threat responses.",
                              "shared": "Eight missions × three simultaneous contacts → shared telemetry → plan → verify all paths → compound burn."})
        return test_case_report
    except Exception as exc:
        test_case_report = {"running": False, "total": 8,
                            "passed": sum(int(c["passed"]) for c in cases), "cases": cases,
                            "error": f"Collision test run stopped ({type(exc).__name__})."}
        raise
    finally:
        test_cases_running = False


@app.get("/api/memory")
async def get_memory():
    async with lock:
        return {"metrics": memory.metrics(), "facts": memory.facts,
                "recent_decisions": list(memory.decisions)[-20:]}


@app.post("/api/missions/{task_id}/decision")
async def human_decision(task_id: str, decision: HumanDecision):
    async with lock:
        pending = pending_humans.get(task_id)
        if not pending:
            raise HTTPException(status_code=404, detail="No pending human review for this mission")
        if decision.action == "retry":
            human_retries.append((pending["satellite"], pending.get("threat_ids", [pending["debris"]])))
        pending_humans.pop(task_id, None)
        resolution = {**pending, "time": round(world.time_sec, 1), "type": "human_decision",
                      "action": decision.action,
                      "message": decision.note or f"Operator chose {decision.action}"}
        events.appendleft(resolution)
        memory.remember_decision(resolution)
        return {"ok": True, "queued": decision.action == "retry"}


@app.websocket("/ws/telemetry")
async def telemetry(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            async with lock:
                payload = state()
            await websocket.send_json(payload)
            await asyncio.sleep(settings.tick_interval_sec)
    except WebSocketDisconnect:
        return
