import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const canvas = document.getElementById('orbit-canvas');
const wrapper = document.querySelector('.canvas-wrap');
const scene = new THREE.Scene();
scene.fog = new THREE.FogExp2(0x07101b, 0.0032);
const camera = new THREE.PerspectiveCamera(43, 1, 0.1, 500);
camera.position.set(0, 43, 72);
const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true, powerPreference: 'high-performance' });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.setClearColor(0x07101b, 0);
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.toneMapping = THREE.ACESFilmicToneMapping;
renderer.toneMappingExposure = 1.18;

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.06;
controls.minDistance = 24;
controls.maxDistance = 115;
controls.maxPolarAngle = Math.PI * 0.49;
controls.target.set(0, 0, 0);

scene.add(new THREE.HemisphereLight(0x8ddcff, 0x09111b, 1.45));
const sun = new THREE.DirectionalLight(0xc6e8ff, 3.2);
sun.position.set(-35, 50, 32);
scene.add(sun);
const rim = new THREE.PointLight(0x278bff, 150, 100, 2);
rim.position.set(27, -8, -28);
scene.add(rim);

const starsGeo = new THREE.BufferGeometry();
const stars = new Float32Array(1900 * 3);
let seed = 17;
const rand = () => { seed = (seed * 16807) % 2147483647; return (seed - 1) / 2147483646; };
for (let i = 0; i < stars.length; i += 3) {
  const radius = 56 + rand() * 105, theta = rand() * Math.PI * 2, phi = Math.acos(2 * rand() - 1);
  stars[i] = radius * Math.sin(phi) * Math.cos(theta);
  stars[i + 1] = radius * Math.cos(phi);
  stars[i + 2] = radius * Math.sin(phi) * Math.sin(theta);
}
starsGeo.setAttribute('position', new THREE.BufferAttribute(stars, 3));
const starField = new THREE.Points(starsGeo, new THREE.PointsMaterial({ color: 0x9dbbd5, size: 0.13, transparent: true, opacity: 0.75, sizeAttenuation: true }));
scene.add(starField);

const earth = new THREE.Mesh(
  new THREE.SphereGeometry(7, 64, 48),
  new THREE.MeshStandardMaterial({ color: 0x12345a, emissive: 0x061b37, emissiveIntensity: 0.8, roughness: 0.48, metalness: 0.18 })
);
earth.position.y = -0.35;
scene.add(earth);
const atmosphere = new THREE.Mesh(
  new THREE.SphereGeometry(7.3, 48, 32),
  new THREE.MeshBasicMaterial({ color: 0x3bafff, transparent: true, opacity: 0.1, side: THREE.BackSide, blending: THREE.AdditiveBlending, depthWrite: false })
);
atmosphere.position.copy(earth.position);
scene.add(atmosphere);

const earthGrid = new THREE.Group();
for (let i = 0; i < 7; i++) {
  const phi = (i + 1) * Math.PI / 8;
  const r = 7.015 * Math.sin(phi), y = 7.015 * Math.cos(phi) - 0.35;
  const points = [];
  for (let j = 0; j <= 96; j++) { const a = j / 96 * Math.PI * 2; points.push(new THREE.Vector3(r * Math.cos(a), y, r * Math.sin(a))); }
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: 0x388bc0, transparent: true, opacity: 0.26 }));
  earthGrid.add(line);
}
scene.add(earthGrid);

const orbitGroup = new THREE.Group();
for (const radius of [12, 19, 26, 33, 40]) {
  const points = [];
  for (let i = 0; i <= 256; i++) { const a = i / 256 * Math.PI * 2; points.push(new THREE.Vector3(radius * Math.cos(a), 0, radius * Math.sin(a))); }
  const line = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(points),
    new THREE.LineBasicMaterial({ color: radius === 26 ? 0x3f86a7 : 0x31506d, transparent: true, opacity: radius === 26 ? 0.34 : 0.2 }));
  orbitGroup.add(line);
}
scene.add(orbitGroup);
const ecliptic = new THREE.GridHelper(88, 22, 0x29465f, 0x132940);
ecliptic.position.y = -0.05;
ecliptic.material.transparent = true;
ecliptic.material.opacity = 0.12;
scene.add(ecliptic);

const satelliteMap = new Map(), debrisMap = new Map(), vectors = new Map(), threatPaths = new Map();
const processedBurns = new Set();
const predictedRoute = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineDashedMaterial({ color: 0xff536c, dashSize: 0.8, gapSize: 0.36, transparent: true, opacity: 0.95 }));
predictedRoute.visible = false; scene.add(predictedRoute);
const encounterMarker = new THREE.Mesh(new THREE.SphereGeometry(0.34, 18, 12), new THREE.MeshBasicMaterial({ color: 0xff536c, transparent: true, opacity: 0.78 }));
encounterMarker.visible = false; scene.add(encounterMarker);
const pickables = [];
const raycaster = new THREE.Raycaster();
raycaster.params.Points.threshold = 1;
const pointer = new THREE.Vector2();
let hoverTarget = null;

function makeTextSprite(text, color = '#a8c8dc') {
  const c = document.createElement('canvas'); c.width = 256; c.height = 64;
  const g = c.getContext('2d'); g.font = '500 30px DM Mono, monospace'; g.fillStyle = color; g.shadowColor = color; g.shadowBlur = 9; g.fillText(text, 8, 43);
  const texture = new THREE.CanvasTexture(c); texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false }));
  sprite.scale.set(4.5, 1.125, 1); return sprite;
}

function makeSatellite(id) {
  const group = new THREE.Group();
  const body = new THREE.Mesh(new THREE.BoxGeometry(0.65, 0.58, 0.82), new THREE.MeshStandardMaterial({ color: 0xc1d3db, metalness: 0.82, roughness: 0.24, emissive: 0x102534 }));
  group.add(body);
  const gold = new THREE.MeshStandardMaterial({ color: 0xd5a960, metalness: 0.72, roughness: 0.28 });
  const boom = new THREE.Mesh(new THREE.CylinderGeometry(0.035, 0.035, 1.3, 8), gold); boom.rotation.z = Math.PI / 2; group.add(boom);
  const panelMaterial = new THREE.MeshStandardMaterial({ color: 0x187fb4, emissive: 0x08315a, emissiveIntensity: 1.2, metalness: 0.55, roughness: 0.32, side: THREE.DoubleSide });
  const panelGeometry = new THREE.BoxGeometry(1.55, 0.06, 0.74);
  for (const side of [-1, 1]) { const panel = new THREE.Mesh(panelGeometry, panelMaterial); panel.position.x = side * 1.02; group.add(panel); }
  const antenna = new THREE.Mesh(new THREE.ConeGeometry(0.3, 0.22, 16), new THREE.MeshStandardMaterial({ color: 0xf3d9a2, metalness: 0.7, roughness: 0.25 }));
  antenna.position.y = 0.39; antenna.rotation.x = Math.PI; group.add(antenna);
  const beacon = new THREE.Mesh(new THREE.SphereGeometry(0.09, 12, 8), new THREE.MeshBasicMaterial({ color: 0x78efff }));
  beacon.position.set(0, 0.02, 0.46); group.add(beacon);
  const glow = new THREE.PointLight(0x50dfff, 2.2, 5); glow.position.copy(beacon.position); group.add(glow);
  const plume = new THREE.Mesh(new THREE.ConeGeometry(0.18, 0.9, 12), new THREE.MeshBasicMaterial({ color: 0x59dcff, transparent: true, opacity: 0.9, blending: THREE.AdditiveBlending, depthWrite: false }));
  plume.rotation.x = -Math.PI / 2; plume.position.set(0, -0.02, -0.98); plume.visible = false; group.add(plume);
  const plumeLight = new THREE.PointLight(0x38caff, 0, 4); plumeLight.position.copy(plume.position); group.add(plumeLight);
  const label = makeTextSprite(id, '#a9d9e8'); label.position.set(0, 0.88, 0); group.add(label);
  group.userData = { id, kind: 'satellite', target: new THREE.Vector3(), phase: rand() * Math.PI * 2, panels: group.children.filter((_, i) => i === 2 || i === 3), plume, plumeLight, thrustUntil: 0 };
  scene.add(group); pickables.push(body); body.userData.entity = group;
  return group;
}

function makeDebris(id) {
  const geo = new THREE.IcosahedronGeometry(0.42 + rand() * 0.34, 1);
  const position = geo.attributes.position;
  for (let i = 0; i < position.count; i++) {
    const scale = 0.72 + rand() * 0.6;
    position.setXYZ(i, position.getX(i) * scale, position.getY(i) * (0.7 + rand() * 0.65), position.getZ(i) * (0.75 + rand() * 0.6));
  }
  geo.computeVertexNormals();
  const colors = [0x99795b, 0xb58153, 0x6d7780, 0xc18c56];
  const mesh = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({ color: colors[Math.floor(rand() * colors.length)], metalness: 0.58, roughness: 0.68, flatShading: true, emissive: 0x281a10, emissiveIntensity: 0.35 }));
  const threatRing = new THREE.Mesh(new THREE.TorusGeometry(0.85, 0.045, 8, 32), new THREE.MeshBasicMaterial({ color: 0xff536c, transparent: true, opacity: 0.9 }));
  threatRing.rotation.x = Math.PI / 2; threatRing.visible = false; mesh.add(threatRing);
  mesh.userData = { id, kind: 'debris', target: new THREE.Vector3(), phase: rand() * Math.PI * 2, spin: new THREE.Vector3(rand() * 0.8, rand() * 1.6, rand() * 0.8), threatRing };
  scene.add(mesh); pickables.push(mesh);
  return mesh;
}

function upsert(map, id, factory, data) {
  let object = map.get(id);
  if (!object) { object = factory(id); map.set(id, object); }
  object.userData.target.set(data.x, 0.25 + Math.sin(object.userData.phase + data.x * 0.08 + data.y * 0.06) * 3.2, data.y);
  object.userData.data = data;
  return object;
}

function updateVectors(events) {
  const recent = events.filter(e => e.type === 'approved' || (e.type === 'test_case' && e.passed)).slice(0, 10).reverse();
  const active = new Set();
  for (const event of recent) {
    const satellite = satelliteMap.get(event.satellite);
    if (!satellite) continue;
    active.add(event.satellite);
    const burnKey = `${event.task_id || ''}:${event.case || ''}:${event.satellite}`;
    if (!processedBurns.has(burnKey)) {
      satellite.userData.thrustUntil = performance.now() + 2200;
      satellite.userData.plumeYaw = Math.atan2(event.dvx || 0, event.dvy || 0.000001);
      processedBurns.add(burnKey);
    }
    const start = satellite.position.clone();
    const end = start.clone().add(new THREE.Vector3((event.dvx || 0) * 420, 0.7, (event.dvy || 0) * 420));
    let line = vectors.get(event.satellite);
    if (!line) {
      line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineDashedMaterial({ color: 0x56e8b0, dashSize: 0.7, gapSize: 0.34, transparent: true, opacity: 0.9 }));
      line.userData = { expires: performance.now() + 9000 };
      scene.add(line); vectors.set(event.satellite, line);
    }
    line.geometry.dispose(); line.geometry = new THREE.BufferGeometry().setFromPoints([start, end]); line.computeLineDistances();
    line.userData.expires = performance.now() + 9000;
  }
  for (const [id, line] of vectors) if (!active.has(id) && performance.now() > line.userData.expires) { scene.remove(line); line.geometry.dispose(); vectors.delete(id); }
}

function syncSatelliteSelector(satellites, testSatellite) {
  const select = document.getElementById('fpv-satellite');
  if (!select) return;
  const selected = selectedSatelliteId;
  const units = [...satellites.filter(s => s.alive !== false)];
  if (testSatellite) units.push(testSatellite);
  select.innerHTML = units.map(s => `<option value="${s.id}">${s.id}${s.id.startsWith('TEST-') ? ' · TEST' : ''}</option>`).join('');
  if (units.some(s => s.id === selected)) select.value = selected;
  else { selectedSatelliteId = units[0]?.id || 'S-01'; select.value = selectedSatelliteId; }
  document.getElementById('fpv-label').textContent = selectedSatelliteId;
}

function makeThreatPath(id) {
  const line = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineDashedMaterial({
    color: 0xff9c67, dashSize: 0.55, gapSize: 0.3, transparent: true, opacity: 0.86,
  }));
  line.userData.threatId = id;
  line.computeLineDistances(); scene.add(line); return line;
}

function updateThreatPaths(satellite, debrisObjects) {
  const visibleIds = new Set();
  const s = satellite?.userData.data;
  for (const d of debrisObjects || []) {
    const object = debrisMap.get(d.id), state = object?.userData.data;
    if (!object || !state || !s) continue;
    const rx = state.x - s.x, ry = state.y - s.y;
    const vx = state.vx - s.vx, vy = state.vy - s.vy;
    const speed2 = vx * vx + vy * vy;
    const time = speed2 < 1e-12 ? 0 : Math.max(0, Math.min(300, -(rx * vx + ry * vy) / speed2));
    const cpa = Math.hypot(rx + vx * time, ry + vy * time);
    const closing = rx * vx + ry * vy < 0;
    object.userData.threatRing.visible = closing && cpa < 10;
    if (!object.userData.threatRing.visible) continue;
    object.userData.threatRing.material.color.set(cpa < 5 ? 0xff536c : 0xffb168);
    visibleIds.add(d.id);
    let path = threatPaths.get(d.id);
    if (!path) { path = makeThreatPath(d.id); threatPaths.set(d.id, path); }
    const endD = new THREE.Vector3(state.x + state.vx * time,
      0.25 + Math.sin(object.userData.phase + (state.x + state.vx * time) * 0.08 + (state.y + state.vy * time) * 0.06) * 3.2,
      state.y + state.vy * time);
    const endS = new THREE.Vector3(s.x + s.vx * time,
      0.25 + Math.sin(satellite.userData.phase + (s.x + s.vx * time) * 0.08 + (s.y + s.vy * time) * 0.06) * 3.2,
      s.y + s.vy * time);
    path.geometry.dispose();
    path.geometry = new THREE.BufferGeometry().setFromPoints([object.position.clone(), endD, endS]);
    path.material.color.set(cpa < 5 ? 0xff536c : 0xffb168);
    path.visible = true; path.computeLineDistances();
  }
  for (const [id, path] of threatPaths) {
    if (!visibleIds.has(id)) path.visible = false;
  }
  document.getElementById('fpv-threat-count').textContent = `${visibleIds.size} APPROACHING CONTACT${visibleIds.size === 1 ? '' : 'S'} · 10 KM SCAN`;
}

function updateScenarioApproaches(approaches, safe) {
  const visibleIds = new Set();
  for (const approach of approaches || []) {
    if (!approach.debris_id || !approach.path?.length) continue;
    let path = threatPaths.get(approach.debris_id);
    if (!path) { path = makeThreatPath(approach.debris_id); threatPaths.set(approach.debris_id, path); }
    path.geometry.dispose();
    const points = approach.path.map(point => new THREE.Vector3(point.x, 0.42, point.y));
    points.push(new THREE.Vector3(approach.impact_x, 0.42, approach.impact_y));
    path.geometry = new THREE.BufferGeometry().setFromPoints(points);
    path.material.color.set(safe ? 0x5de2b2 : 0xff536c);
    path.visible = true; path.computeLineDistances(); visibleIds.add(approach.debris_id);
  }
  for (const [id, path] of threatPaths) if (id.startsWith('TEST-D') && !visibleIds.has(id)) path.visible = false;
}

let fpvMode = false;
let selectedSatelliteId = 'S-01';
function setFpvMode(enabled) {
  fpvMode = enabled;
  controls.enabled = !enabled;
  document.getElementById('fpv-toggle')?.classList.toggle('fpv-active', enabled);
  document.getElementById('fpv-toggle')?.setAttribute('aria-pressed', String(enabled));
  document.getElementById('fpv-toggle').textContent = enabled ? '◎ EXIT FIRST-PERSON' : '◎ FIRST-PERSON';
  document.getElementById('fpv-hud')?.classList.toggle('visible', enabled);
  if (!enabled) window.resetOrbitView();
}

function trackFpvCamera(dt) {
  if (!fpvMode) return;
  const craft = satelliteMap.get(selectedSatelliteId);
  const state = craft?.userData.data;
  if (!craft || !state) return;
  const forward = new THREE.Vector3(state.vx, 0, state.vy).normalize();
  if (forward.lengthSq() < 1e-8) forward.set(0, 0, 1);
  const desiredCamera = craft.position.clone().addScaledVector(forward, -7).add(new THREE.Vector3(0, 3.2, 0));
  const lookAhead = craft.position.clone().addScaledVector(forward, 16).add(new THREE.Vector3(0, 0.35, 0));
  camera.position.lerp(desiredCamera, 1 - Math.exp(-dt * 3));
  controls.target.lerp(lookAhead, 1 - Math.exp(-dt * 4));
}

document.getElementById('fpv-toggle')?.addEventListener('click', () => setFpvMode(!fpvMode));
document.getElementById('fpv-satellite')?.addEventListener('change', event => {
  selectedSatelliteId = event.target.value;
  document.getElementById('fpv-label').textContent = selectedSatelliteId;
});

window.updateOrbitalScene = function (data) {
  for (const satellite of data.satellites || []) upsert(satelliteMap, satellite.id, makeSatellite, satellite);
  for (const debris of data.debris || []) upsert(debrisMap, debris.id, makeDebris, debris);
  syncSatelliteSelector(data.satellites || [], data.test_overlay?.satellite);
  const testView = Boolean(data.test_overlay && (data.test_cases?.running || selectedSatelliteId.startsWith('TEST-')));
  for (const [id, object] of satelliteMap) if (id.startsWith('TEST-S')) object.visible = testView;
  for (const [id, object] of debrisMap) if (id.startsWith('TEST-D')) object.visible = testView;
  if (testView) {
    for (const [id, object] of satelliteMap) if (id.startsWith('TEST-S') && id !== data.test_overlay.satellite.id) { scene.remove(object); satelliteMap.delete(id); }
    const testThreatIds = new Set((data.test_overlay.threats || [data.test_overlay.debris]).map(threat => threat.id));
    for (const [id, object] of debrisMap) if (id.startsWith('TEST-D') && !testThreatIds.has(id)) { scene.remove(object); debrisMap.delete(id); }
    upsert(satelliteMap, data.test_overlay.satellite.id, makeSatellite, data.test_overlay.satellite);
    const overlayThreats = data.test_overlay.threats || [data.test_overlay.debris];
    for (const item of overlayThreats) {
      const threat = upsert(debrisMap, item.id, makeDebris, item);
      threat.userData.threatRing.visible = true;
      const pathSafe = data.test_overlay.phase === 'PATH AVOIDED';
      threat.userData.threatRing.material.color.set(pathSafe ? 0x5de2b2 : 0xff536c);
      threat.material.color.set(pathSafe ? 0x59b79a : 0xd95c68);
      threat.material.emissive.set(pathSafe ? 0x103c31 : 0x4b101e);
    }
    const pathSafe = data.test_overlay.phase === 'PATH AVOIDED';
    const approach = data.test_overlay.approach;
    if (approach?.path?.length) {
      predictedRoute.geometry.dispose();
      predictedRoute.geometry = new THREE.BufferGeometry().setFromPoints(approach.path.map(point => new THREE.Vector3(point.x, 0.7, point.y)));
      predictedRoute.computeLineDistances();
      const safe = pathSafe;
      predictedRoute.material.color.set(safe ? 0x5de2b2 : 0xff536c);
      predictedRoute.visible = true;
      encounterMarker.position.set(approach.impact_x, 0.7, approach.impact_y);
      encounterMarker.material.color.set(safe ? 0x5de2b2 : 0xff536c);
      encounterMarker.visible = true;
      document.getElementById('tick-label').textContent = `THREAT ${String(data.test_overlay.case).padStart(2, '0')} · ${safe ? `PATH CHANGED · ${approach.predicted_distance_km.toFixed(1)} KM MISS` : `IMPACT IN ${Math.round(approach.closest_in_sec)}s`}`;
    }
  } else {
    predictedRoute.visible = false; encounterMarker.visible = false;
  }
  if (testView) {
    const craft = satelliteMap.get(data.test_overlay.satellite.id);
    updateThreatPaths(craft, data.test_overlay.threats || [data.test_overlay.debris]);
    updateScenarioApproaches(data.test_overlay.approaches, data.test_overlay.phase === 'PATH AVOIDED');
  } else {
    for (const [id, object] of debrisMap) if (id.startsWith('TEST-D')) object.userData.threatRing.visible = false;
    updateThreatPaths(satelliteMap.get(selectedSatelliteId), data.debris || []);
  }
  updateVectors(data.events || []);
  document.getElementById('scene-loading')?.classList.add('hidden');
};

function resize() {
  const width = wrapper.clientWidth, height = wrapper.clientHeight;
  if (!width || !height) return;
  renderer.setSize(width, height, false); camera.aspect = width / height; camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(wrapper); resize();

const tooltip = document.getElementById('tooltip');
canvas.addEventListener('pointermove', event => {
  const previousHover = hoverTarget;
  const rect = canvas.getBoundingClientRect(); pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1; pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera); const hit = raycaster.intersectObjects(pickables, false)[0];
  hoverTarget = hit?.object.userData.entity || hit?.object || null;
  if (previousHover && previousHover !== hoverTarget) previousHover.scale.setScalar(1);
  if (hoverTarget) {
    const d = hoverTarget.userData.data, id = hoverTarget.userData.id, type = hoverTarget.userData.kind;
    tooltip.innerHTML = `<b>${id}</b><span>${type === 'satellite' ? 'SATELLITE' : 'DEBRIS'} · ${d ? `${d.x.toFixed(1)}, ${d.y.toFixed(1)} KM` : 'TRACKING'}</span>`;
    tooltip.style.display = 'block'; tooltip.style.left = `${Math.min(event.clientX - rect.left + 14, rect.width - 170)}px`; tooltip.style.top = `${Math.max(12, event.clientY - rect.top - 28)}px`; canvas.style.cursor = 'crosshair';
  } else { tooltip.style.display = 'none'; canvas.style.cursor = 'grab'; }
});
canvas.addEventListener('pointerleave', () => { if (hoverTarget) hoverTarget.scale.setScalar(1); hoverTarget = null; tooltip.style.display = 'none'; });

const clock = new THREE.Clock();
function animate() {
  requestAnimationFrame(animate);
  const dt = Math.min(clock.getDelta(), 0.05), t = clock.elapsedTime;
  earth.rotation.y += dt * 0.025; earthGrid.rotation.y = earth.rotation.y; atmosphere.scale.setScalar(1 + Math.sin(t * 0.8) * 0.004);
  starField.rotation.y += dt * 0.0012; orbitGroup.rotation.y = Math.sin(t * 0.06) * 0.018;
  for (const object of satelliteMap.values()) {
    object.position.lerp(object.userData.target, 1 - Math.exp(-dt * 3.5));
    object.rotation.y += dt * 0.11; object.rotation.z = Math.sin(t * 0.7 + object.userData.phase) * 0.045;
    for (const panel of object.userData.panels) panel.rotation.z = Math.sin(t * 0.32 + object.userData.phase) * 0.025;
    const beacon = object.children[5]; if (beacon?.material) beacon.material.color.setHSL(0.51, 0.9, 0.58 + Math.sin(t * 3 + object.userData.phase) * 0.16);
    const burning = performance.now() < object.userData.thrustUntil;
    object.userData.plume.rotation.y = object.userData.plumeYaw - object.rotation.y;
    object.userData.plume.visible = burning;
    object.userData.plumeLight.intensity = burning ? 2.6 + Math.sin(t * 34 + object.userData.phase) * 0.8 : 0;
    if (burning) object.userData.plume.scale.y = 0.9 + Math.sin(t * 29 + object.userData.phase) * 0.22;
  }
  for (const object of debrisMap.values()) {
    object.position.lerp(object.userData.target, 1 - Math.exp(-dt * 2.8));
    object.rotation.x += dt * object.userData.spin.x; object.rotation.y += dt * object.userData.spin.y; object.rotation.z += dt * object.userData.spin.z;
    if (object.userData.threatRing.visible) object.userData.threatRing.scale.setScalar(1 + Math.sin(t * 6 + object.userData.phase) * 0.12);
  }
  trackFpvCamera(dt);
  if (encounterMarker.visible) encounterMarker.scale.setScalar(0.8 + Math.sin(t * 5) * 0.15);
  if (hoverTarget) hoverTarget.scale.setScalar(1.2 + Math.sin(t * 5) * 0.06);
  controls.update(); renderer.render(scene, camera);
}
animate();
// The loading veil belongs to WebGL initialization, not telemetry. Keep the
// orbit scene visible while the agent stream reconnects or reports a failure.
document.getElementById('scene-loading')?.classList.add('hidden');
if (window.latestTelemetry) window.updateOrbitalScene(window.latestTelemetry);

window.resetOrbitView = () => { camera.position.set(0, 43, 72); controls.target.set(0, 0, 0); controls.update(); };
window.addEventListener('beforeunload', () => { controls.dispose(); renderer.dispose(); });
window.addEventListener('error', event => { document.getElementById('scene-loading')?.classList.remove('hidden'); document.getElementById('scene-loading').textContent = `3D renderer error: ${event.message}`; });
