# AstroMesh

AstroMesh is an agent-driven orbital safety simulation. Its agents detect close approaches, plan satellite maneuvers, independently verify safety constraints, and execute approved thruster burns. A live 3D scene and event stream make each decision and handoff visible.

The system is designed around three recursive-agent ideas: define verifiable completion, share useful memory across agents, and promote a better playbook only when it improves on held-out scenarios.

## Orbital operations

- **Interactive 3D constellation:** Explore an animated orbital scene containing 16 satellites and 64 debris objects. Rotate and zoom the view, inspect spacecraft, and follow orbital tracks, approaching debris, closest-approach markers, maneuver vectors, and thruster activity.
- **First-person satellite view:** Select a craft and lock the camera behind it, aligned with its velocity, to see objects approaching its path from the spacecraft's perspective.
- **Repeatable multi-threat flight:** Start an isolated test flight in which one test spacecraft encounters eight scenarios, each with three concurrent debris threats. The simulation displays detections, the planned compound Δv, thruster burn, changed path, and the verified miss distance for each object.
- **Live fleet operations:** The live simulation tracks spacecraft telemetry and close approaches. Proposed maneuvers are checked against current state before they are committed; the supervisor can replan or stop for human review when its retry budget is exhausted.

## Agent decision flow

AstroMesh exposes the collaboration between agents in a synchronized command and data stream:

1. **Root Architect** establishes the mission contract and maneuver constraints.
2. **Mission Supervisor** monitors telemetry, raises collision alerts, coordinates replanning, and handles escalation.
3. **Shared Orbital Memory** supplies relevant observations and prior verified outcomes to the agents and records new results.
4. **Satellite Worker** evaluates the encounter and proposes a compound trajectory change and thruster burn.
5. **Adversarial Verifier** independently checks predicted separation from every threat, fuel, burn duration, and configured safety limits.
6. **Thruster Controller** applies only an approved maneuver to the simulation.
7. **Playbook Evaluator** scores candidate policies against training and held-out cases and reports whether a candidate qualifies for promotion.

Telemetry handoffs, decisions, verifier results, approvals, burns, replans, and mission outcomes appear in the live stream. The event log lets a reviewer follow why a maneuver was taken, rather than seeing only a final score.

## Done Means Done

Missions use explicit completion conditions and safety invariants. A maneuver is considered complete only after the verifier checks the simulated result. If the worker's plan fails verification, the supervisor can request another plan within a bounded retry budget; a repeated failure or exhausted budget is surfaced for human review. The UI reports verified missions, replans, stopped loops, and escalations.

## Shared Brain

Agents read and update a persistent, conflict-aware orbital memory. It stores observations, decisions, and verified outcomes, deduplicates repeated telemetry, and resolves competing updates. The interface reports memory activity and inference cache usage when the provider returns those measurements.

## Version N+1

The Playbook Evaluator selects a candidate using training scenarios, then compares it with the active playbook on a separate set of 48 held-out scenarios. Promotion is gated on measured improvement and retained safety checks. The benchmark view shows both scores, pass counts, score change, and the promotion decision. These are results on synthetic scenarios, not a real-world collision probability.

## Live observability

The browser receives state updates over a WebSocket and can recover with REST snapshots. The scene, agent stream, event log, fleet telemetry, and metrics update from the same simulation state. Observability includes:

- collision survival and verified avoidance results;
- agent latency, inference provider status, and rate-limit or fallback state;
- mission completion, replans, loop stops, and escalations;
- shared-memory records, deduplicated observations, and cached tokens;
- playbook version, benchmark scores, and candidate promotion status.

Provider and inference metrics reflect observed calls and provider-reported usage; local fallback work is identified separately.

## Inference and simulation views

When configured, AstroMesh routes agent reasoning through Crusoe Managed Inference using its OpenAI-compatible API and the NVIDIA Nemotron model route. Without a live key, the application can use its deterministic local planner. NemoClaw is an optional OpenShell sandbox boundary for controlling the agent service's access to inference and telemetry; it is not itself the model or inference endpoint.

The browser scene is rendered with Three.js. An optional NVIDIA Isaac Sim viewer reads AstroMesh state and presents the same satellite and debris telemetry in a USD scene. The simulation remains the source of state for both visualizations.

## Project structure

- `astromesh/sim/` — deterministic orbital-plane dynamics and closest-approach checks.
- `astromesh/agents/` — mission architect, satellite worker, verifier, supervisor, and playbook evaluator.
- `astromesh/memory/` — persistent shared orbital memory.
- `astromesh/server/` — simulation orchestration, inference calls, REST snapshots, and WebSocket telemetry.
- `frontend/` — live operations interface and interactive Three.js orbital scene.
- `isaac_sim/` — Isaac Sim viewer for the AstroMesh telemetry feed.

## Simulation scope

AstroMesh is a demonstration, not flight software or an operational collision-avoidance service. Its orbital physics use a simplified two-dimensional model with exact closest-approach calculations for linear relative motion. Test-flight success rates describe only the fixed synthetic cases included in the project.
