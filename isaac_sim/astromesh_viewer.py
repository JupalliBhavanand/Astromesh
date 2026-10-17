"""Run inside Isaac Sim's Script Editor to visualize AstroMesh telemetry.

The FastAPI app owns the orbital simulation. This viewer polls its read-only state
endpoint off the Kit thread and updates the USD scene on each Kit update tick.
"""

from __future__ import annotations

import json
import math
import threading
import time
from urllib.request import urlopen

import omni.kit.app
import omni.ui as ui
import omni.usd
from pxr import Gf, Sdf, UsdGeom, Vt


TELEMETRY_URL = "http://127.0.0.1:8000/api/state"
POLL_INTERVAL_SEC = 0.25
WORLD = "/World/AstroMesh"


class AstroMeshIsaacViewer:
    def __init__(self):
        self.stage = omni.usd.get_context().get_stage()
        self._lock = threading.Lock()
        self._latest = None
        self._last_poll = 0.0
        self._running = True
        self._bodies = {}
        self._vectors = {}
        self._colors = {}
        self._radii = {}

        self._build_scene()
        self._build_panel()
        self._poll_thread = threading.Thread(target=self._poll_forever, daemon=True)
        self._poll_thread.start()
        self._update_subscription = omni.kit.app.get_app().get_update_event_stream().create_subscription_to_pop(
            self._on_update, name="AstroMesh telemetry to USD")
        self._set_camera()

    @staticmethod
    def _set_color(prim, rgb):
        UsdGeom.Gprim(prim).CreateDisplayColorAttr().Set([Gf.Vec3f(*rgb)])

    def _build_scene(self):
        UsdGeom.Xform.Define(self.stage, WORLD)
        # A stylized Earth provides scale and orientation; object positions are a 2D
        # orbital-plane projection mapped into the XZ plane (coordinates in km).
        earth = UsdGeom.Sphere.Define(self.stage, WORLD + "/Earth")
        earth.CreateRadiusAttr(7.0)
        self._set_color(earth.GetPrim(), (0.07, 0.24, 0.43))

        # Reference rings are decorative only; AstroMesh telemetry remains authoritative.
        for radius in (12.0, 22.0, 32.0):
            path = f"{WORLD}/ReferenceOrbit_{int(radius)}km"
            curve = UsdGeom.BasisCurves.Define(self.stage, path)
            points = [Gf.Vec3f(radius * math.cos(i * math.tau / 128), 0.0,
                                radius * math.sin(i * math.tau / 128)) for i in range(128)]
            curve.CreateTypeAttr(UsdGeom.Tokens.linear)
            curve.CreateCurveVertexCountsAttr([len(points)])
            curve.CreatePointsAttr(Vt.Vec3fArray(points))
            curve.CreateWidthsAttr(Vt.FloatArray([0.035]))
            curve.CreateWrapAttr(UsdGeom.Tokens.periodic)
            self._set_color(curve.GetPrim(), (0.12, 0.21, 0.30))

        for entity_id, color, radius in (
            ("satellite", (0.28, 0.86, 0.94), 0.72),
            ("debris", (0.96, 0.61, 0.25), 0.38),
        ):
            self._colors[entity_id] = color
            self._radii[entity_id] = radius

        # An XYZ axis marker reinforces that the 2D orbital plane is shown in XZ.
        axes = UsdGeom.BasisCurves.Define(self.stage, WORLD + "/PlaneAxes")
        axes.CreateTypeAttr(UsdGeom.Tokens.linear)
        axes.CreateCurveVertexCountsAttr([2, 2])
        axes.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(-38, 0, 0), Gf.Vec3f(38, 0, 0),
                                             Gf.Vec3f(0, 0, -38), Gf.Vec3f(0, 0, 38)]))
        axes.CreateWidthsAttr(Vt.FloatArray([0.025, 0.025]))
        self._set_color(axes.GetPrim(), (0.22, 0.34, 0.46))

    def _build_panel(self):
        self._window = ui.Window("AstroMesh · Mission Telemetry", width=330, height=245)
        with self._window.frame:
            with ui.VStack(spacing=8, style={"margin": 12}):
                ui.Label("ASTROMESH / ORBITAL OPERATIONS", style={"font_size": 14, "color": 0xFFDBE8F6})
                self._status_label = ui.Label("Connecting to AstroMesh API…")
                self._clock_label = ui.Label("Simulation time: —")
                self._fleet_label = ui.Label("Fleet: —")
                self._metrics_label = ui.Label("Avoidance: —   Survival: —")
                self._events_label = ui.Label("Latest verified event: —", word_wrap=True)
                ui.Spacer(height=4)
                ui.Label("3D VIEW · positions use the orbital XZ plane · units: km",
                         style={"font_size": 10, "color": 0xFF92A8BE}, word_wrap=True)

    def _poll_forever(self):
        while self._running:
            try:
                with urlopen(TELEMETRY_URL, timeout=2.0) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                with self._lock:
                    self._latest = payload
                    self._last_poll = time.time()
            except Exception as exc:
                with self._lock:
                    self._poll_error = str(exc)
            time.sleep(POLL_INTERVAL_SEC)

    @staticmethod
    def _set_position(prim, x, z):
        UsdGeom.Xformable(prim).ClearXformOpOrder()
        UsdGeom.Xformable(prim).AddTranslateOp().Set(Gf.Vec3d(float(x), 0.0, float(z)))

    def _ensure_body(self, entity_id, kind):
        if entity_id in self._bodies:
            return self._bodies[entity_id]
        path = f"{WORLD}/{kind.title()}_{entity_id.replace('-', '_')}"
        sphere = UsdGeom.Sphere.Define(self.stage, path)
        sphere.CreateRadiusAttr(self._radii[kind])
        self._set_color(sphere.GetPrim(), self._colors[kind])
        self._bodies[entity_id] = sphere.GetPrim()
        return sphere.GetPrim()

    def _ensure_vector(self, entity_id):
        if entity_id not in self._vectors:
            curve = UsdGeom.BasisCurves.Define(self.stage, f"{WORLD}/Maneuver_{entity_id.replace('-', '_')}")
            curve.CreateTypeAttr(UsdGeom.Tokens.linear)
            curve.CreateCurveVertexCountsAttr([2])
            curve.CreatePointsAttr(Vt.Vec3fArray([Gf.Vec3f(0, 0, 0), Gf.Vec3f(0, 0, 0)]))
            curve.CreateWidthsAttr(Vt.FloatArray([0.11]))
            self._set_color(curve.GetPrim(), (0.24, 0.94, 0.61))
            self._vectors[entity_id] = curve
        return self._vectors[entity_id]

    def _on_update(self, _event):
        with self._lock:
            payload = self._latest
            poll_error = getattr(self, "_poll_error", "")
            last_poll = self._last_poll
        if not payload:
            if poll_error:
                self._status_label.text = f"API unavailable: {poll_error[:90]}"
            return

        for body in payload.get("satellites", []):
            prim = self._ensure_body(body["id"], "satellite")
            self._set_position(prim, body["x"], body["y"])
        for body in payload.get("debris", []):
            prim = self._ensure_body(body["id"], "debris")
            self._set_position(prim, body["x"], body["y"])

        for event in payload.get("events", [])[:3]:
            if event.get("type") != "approved":
                continue
            satellite = next((item for item in payload.get("satellites", [])
                              if item["id"] == event.get("satellite")), None)
            if satellite:
                curve = self._ensure_vector(satellite["id"])
                scale = 400.0
                points = [Gf.Vec3f(satellite["x"], 0, satellite["y"]),
                          Gf.Vec3f(satellite["x"] + event.get("dvx", satellite["vx"]) * scale, 0,
                                   satellite["y"] + event.get("dvy", satellite["vy"]) * scale)]
                curve.GetPointsAttr().Set(Vt.Vec3fArray(points))

        metrics = payload.get("metrics", {})
        age = time.time() - last_poll
        self._status_label.text = "Connected · telemetry live" if age < 2 else "Telemetry stream delayed"
        self._clock_label.text = f"Simulation time: T+ {payload.get('time_sec', 0):.1f}s   ·   Epoch {payload.get('epoch', 1)}"
        self._fleet_label.text = f"Fleet: {len(payload.get('satellites', []))} satellites   ·   {len(payload.get('debris', []))} debris objects"
        self._metrics_label.text = (f"Avoidance: {metrics.get('avoidance_rate', 0)}%   ·   "
                                    f"Survival: {metrics.get('survival_rate', 0)}%   ·   "
                                    f"Playbook v{metrics.get('version', 1)}")
        events = payload.get("events", [])
        if events:
            event = events[0]
            self._events_label.text = (f"Latest event: {event['satellite']} / {event['debris']} · "
                                       f"{event['type'].upper()} · {event['distance_km']:.1f} km")

    def _set_camera(self):
        try:
            from isaacsim.core.rendering_manager import ViewportManager
            camera = ViewportManager.get_camera()
            ViewportManager.set_camera_view(camera, eye=[0, -88, 62], target=[0, 0, 0])
        except Exception as exc:
            print(f"AstroMesh: kept the default viewport camera ({exc})")

    def close(self):
        self._running = False
        self._update_subscription = None
        if self._window:
            self._window.destroy()


try:
    astromesh_viewer.close()
except Exception:
    pass
astromesh_viewer = AstroMeshIsaacViewer()
print(f"AstroMesh Isaac viewer connected to {TELEMETRY_URL}")
