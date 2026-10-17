# AstroMesh

AstroMesh is an async orbital-debris mitigation demo with a live browser visualization, contract-based maneuver verification, shared orbital memory, and an evaluator that versions successful playbooks.

## Crusoe and Nemotron

- The default model route is **NVIDIA Nemotron 3 Super 120B through Crusoe Managed Inference**, using Crusoe's OpenAI-compatible `https://api.inference.crusoecloud.com/v1` endpoint. The account catalog ID is `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B`.
- Put a Crusoe Intelligence Foundry key in `CRUSOE_API_KEY` (or the process environment). For existing setups, when Crusoe is the selected provider the app also accepts `ASTROMESH_LLM_API_KEY`. With neither value configured, the demo uses the deterministic local planner and makes no inference requests.
- NVIDIA API Catalog can still be selected by setting `ASTROMESH_LLM_PROVIDER=nvidia`, its endpoint and model, then `NVIDIA_API_KEY`.
- **NemoClaw** is the optional deployment boundary for running the AstroMesh agent service in an OpenShell sandbox with controlled egress. It is not a Python model SDK or an inference endpoint. Follow [NVIDIA's NemoClaw overview](https://docs.nvidia.com/nemoclaw/latest/about/overview.html) and current onboarding for your host, then run this service inside the configured sandbox and allow only the required inference and telemetry destinations. Set `ASTROMESH_RUNTIME_MODE=nemoclaw` only when it is actually running in that sandbox. The app runs locally without NemoClaw for development.

The authoritative setup is Crusoe's [Serverless Inference quickstart](https://docs.cloud.crusoe.ai/quickstart/getting-started-with-serverless-inference/index.html) and [available-model catalog](https://docs.cloud.crusoe.ai/serverless-inference/available-models/index.html). Check model access and rate limits in your account before a live demo.

## Run locally

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
# Edit .env and add CRUSOE_API_KEY (or ASTROMESH_LLM_API_KEY) for Crusoe.
uvicorn astromesh.server.api:app --reload --reload-dir astromesh
```

For live development, run `astromesh` or use the command above. Backend edits restart the API automatically, and frontend edits refresh open dashboard tabs automatically. A running server launched before live reload was enabled must be restarted once to pick up the reloader.

Open <http://127.0.0.1:8000>. The landing view is the interactive 3D orbital scene and agent mesh; supporting metrics and hackathon evidence sit below it. The seeded fleet contains 16 satellites and 64 debris objects. WebSocket telemetry is available at `/ws/telemetry`; REST snapshots are available at `/api/state`, `/api/memory`, and `/api/benchmark`.

## Demo the three tracks

1. **Done Means Done:** press **START MULTI-THREAT FLIGHT** in the scene. One repeatable test spacecraft faces eight encounters with three simultaneous debris threats. The worker optimizes one compound burn; the independent verifier checks separation from every object, fuel, and duration before the thruster controller fires. The 3D view shows the inbound paths, impact markers, thruster vector, changing trajectory, and live agent data packets. Use the satellite selector and **FIRST-PERSON** toggle to lock the camera behind an active craft along its velocity vector. The test flight is isolated from the live fleet. On live close approaches, plans are rechecked against current telemetry before a burn commits; exhausted retries stop for human review.
2. **Shared Brain:** the team retrieves nearby telemetry and prior verified outcomes through a durable, conflict-aware memory file at `data/orbital_memory.json`. Agents share a stable system prefix for Crusoe's cache-aware routing. The UI reports actual provider cached-token counts when returned, along with memory reads, writes, conflicts, and deduplicated observations.
3. **Version N+1:** after three mission outcomes, AstroMesh selects a candidate using training scenarios and compares it with the active playbook on 48 separate held-out scenarios. It promotes only when the candidate beats the parent and preserves the safety checks. Click **RUN 48-CASE HELD-OUT EVAL** to run and show the comparison on demand. Reports include both scores, pass counts, delta, and promotion decision.

For the walkthrough, show one verified approach, deliberately demonstrate a rejected/replanned attempt if one occurs, inspect shared-memory metrics, then run the held-out comparison. The benchmark is deterministic and auditable; do not present a candidate score as a real-world collision rate.

## Show the simulation in NVIDIA Isaac Sim

AstroMesh remains the source of simulation state. The browser renders an interactive Three.js 3D scene with Earth, orbital reference rings, animated satellites and debris, inbound collision paths, impact markers, thruster plumes, verified burn vectors, and hover details. Drag to orbit the camera and scroll to zoom. It loads the pinned Three.js release from jsDelivr, so the browser needs access to that CDN. The seeded demo constellation has 16 satellites and 64 debris objects; Isaac Sim receives the same expanded telemetry.

1. Start AstroMesh as above and leave it running at `http://127.0.0.1:8000`.
2. Launch Isaac Sim and open **Window → Script Editor**.
3. Open and run [`isaac_sim/astromesh_viewer.py`](isaac_sim/astromesh_viewer.py) in the Script Editor. It connects to `/api/state` and updates the USD scene while Isaac Sim runs. The viewer expects Isaac Sim's bundled `pxr`, `omni`, and `isaacsim` Python modules, so run the script inside Isaac Sim rather than a system Python interpreter.

To use another AstroMesh host, edit `TELEMETRY_URL` at the top of the viewer script. It uses Isaac Sim's USD geometry APIs and the current `ViewportManager` camera API. See NVIDIA's [OpenUSD fundamentals](https://docs.isaacsim.omniverse.nvidia.com/latest/omniverse_usd/open_usd.html) and [viewport rendering manager API](https://docs.isaacsim.omniverse.nvidia.com/latest/py/source/extensions/isaacsim.core.rendering_manager/docs/api.html).

## Project map

`sim/physics.py` contains deterministic orbital-plane dynamics and collision checks. `agents/` contains the architect, worker, independent verifier, persistent mission supervisor, and held-out playbook evaluator. `memory/shared_brain.py` persists observations, verified outcomes, and conflict-aware facts. `server/api.py` runs physics concurrently with async inference, revalidates stale plans before commit, and serves WebSocket telemetry. `frontend/` serves the interactive Three.js dashboard from FastAPI.

The simulator is illustrative and uses a simplified 2D model with exact closest-approach calculations for linear relative motion; it is not flight software, a high-fidelity propagator, or an operational collision-avoidance service. The scenario lab's success rate describes only its fixed synthetic cases.
