let socket, retry = 1000;
let lastTelemetryAt = 0;
let lastSocketAt = 0;
let snapshotInFlight = false;
let snapshotFailures = 0;
let dashboardBuild = null;
let reconnectTimer = null;
const $ = id => document.getElementById(id);

function showBackendProblem(message) {
  const loading = $('scene-loading');
  if (loading && !lastTelemetryAt) {
    loading.replaceChildren(document.createTextNode(message));
    loading.classList.remove('hidden');
  }
}

function formatTime(seconds) {
  const s = Math.floor(seconds);
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function setConnection(label, offline = false) {
  $('connection-label').textContent = label;
  document.querySelector('.connection').classList.toggle('offline', offline);
  $('connection-dot').style.background = offline ? '#e57979' : '#55d7a2';
}

function render(data, source = 'websocket') {
  lastTelemetryAt = Date.now();
  snapshotFailures = 0;
  if (source === 'websocket') lastSocketAt = lastTelemetryAt;
  if (source === 'rest') setConnection('REST SYNC');
  else setConnection('CONNECTED');
  window.latestTelemetry = data;
  window.updateOrbitalScene?.(data);
  if (!window.updateOrbitalScene && !window.sceneLoadWarning) {
    window.sceneLoadWarning = setTimeout(() => {
      if (!window.updateOrbitalScene) { $('scene-loading').textContent = '3D engine unavailable · check the Three.js CDN connection'; $('scene-loading').classList.remove('hidden'); }
    }, 10000);
  }
  const m = data.metrics || {}, satellites = data.satellites || [], debris = data.debris || [];
  const testReport = data.test_cases || {};
  const needsReview = Boolean(data.pending_human_reviews?.length);
  const providerTrouble = ['error', 'degraded', 'rate_limited', 'key_missing'].includes(m.inference_status);
  const systemText = $('system-status-text'), systemCaption = $('system-status-caption');
  if (needsReview) { systemText.textContent = 'REVIEW REQUIRED'; systemCaption.textContent = `${data.pending_human_reviews.length} mission awaiting operator decision`; }
  else if (providerTrouble) { systemText.textContent = 'DEGRADED'; systemCaption.textContent = m.mode || 'Inference fallback active'; }
  else if (data.mission_stage?.agent) { systemText.textContent = 'MISSION ACTIVE'; systemCaption.textContent = data.mission_stage.stage || 'Agents working'; }
  else { systemText.textContent = 'NOMINAL'; systemCaption.textContent = 'Telemetry and safety gate active'; }
  const alive = satellites.filter(s => s.alive !== false).length;
  const survival = m.survival_rate ?? Math.round(alive / Math.max(satellites.length, 1) * 100);
  $('survival').innerHTML = `${survival}<span>%</span>`;
  $('survival-bar').style.width = `${survival}%`;
  const avoidanceRate = testReport.total ? Math.round((testReport.passed || 0) / testReport.total * 100) : (m.avoidance_rate ?? 0);
  $('avoidance').innerHTML = `${avoidanceRate}<span>%</span>`;
  $('maneuver-count').textContent = testReport.total
    ? `${testReport.passed || 0}/${testReport.total} test cases · ${m.approved ?? 0} live burns`
    : `${m.approved ?? 0} live maneuvers verified`;
  $('latency').innerHTML = m.latency_ms == null ? '—<span>ms</span>' : `${m.latency_ms}<span>ms</span>`;
  const requestInfo = m.inference_requests ? ` · ${m.inference_successes || 0}/${m.inference_requests} calls` : '';
  const errorInfo = m.inference_error ? ` · ${m.inference_error}` : '';
  $('provider-mode').textContent = `${m.mode || 'Local planner'}${requestInfo}${errorInfo}`;
  // The API publishes these as playbook_version/playbook_delta.
  $('version').innerHTML = `v${m.playbook_version ?? 1}<span class="delta">${(m.playbook_delta ?? 0) >= 0 ? '+' : ''}${Number(m.playbook_delta ?? 0).toFixed(2)}</span>`;
  $('tokens').textContent = (m.token_total ?? 0).toLocaleString();
  $('coverage').textContent = `${Math.round(satellites.reduce((sum, s) => sum + (s.coverage || 0), 0) / Math.max(satellites.length, 1) * 100)}%`;
  $('memory-nodes').textContent = `${data.memory?.observation_nodes ?? 0} NODES`;
  $('sat-count').textContent = String(satellites.length).padStart(2, '0');
  $('debris-count').textContent = String(debris.length).padStart(2, '0');
  $('clock').textContent = `T+ ${formatTime(data.time_sec || 0)}`;
  if (!data.test_overlay) $('tick-label').textContent = `EPOCH ${String(data.epoch || 1).padStart(2, '0')} / T+ ${String(Math.floor(data.time_sec || 0)).padStart(3, '0')}`;
  $('fleet-summary').textContent = `${satellites.length} UNITS · ${alive} ACTIVE`;

  const brain = data.memory || {}, crew = data.supervisor || {}, bench = data.playbook || {};
  const report = bench.last_report || {};
  $('proof-done-main').innerHTML = `${crew.missions_completed ?? 0} / ${crew.missions_started ?? 0} <small>missions verified</small>`;
  $('proof-done-detail').textContent = `${crew.replans ?? 0} replans · ${crew.loop_detections ?? 0} loops stopped · ${crew.human_escalations ?? 0} escalations · ${crew.pending_human_reviews ?? 0} awaiting review`;
  $('proof-memory-main').innerHTML = `${(brain.observation_nodes ?? 0) + (brain.decision_records ?? 0) + (brain.curated_facts ?? 0)} <small>shared records</small>`;
  $('proof-memory-detail').textContent = `${brain.observation_nodes ?? 0} tracks · ${brain.decision_records ?? 0} outcomes · ${brain.deduped_observations ?? 0} repeats deduped · ${brain.cached_tokens ?? 0} cached tokens`;
  if (bench.evaluations) {
    const promotion = report.promoted ? `PROMOTED v${report.version}` : 'SHADOW / NOT PROMOTED';
    $('proof-version-state').innerHTML = `<i></i> ${promotion}`;
    const deltaSign = report.delta >= 0 ? '+' : '';
    $('proof-version-main').innerHTML = `${report.baseline_score.toFixed(1)} → ${report.candidate_score.toFixed(1)} <small>${deltaSign}${report.delta.toFixed(1)} score</small>`;
    const feedback = report.real_feedback;
    const learning = feedback?.outcomes_used
      ? ` · ${feedback.outcomes_used} real outcomes guided training search`
      : ' · no real outcomes in this training batch';
    $('proof-version-detail').textContent = `${report.holdout_passed_baseline}/${report.holdout_cases} → ${report.holdout_passed_candidate}/${report.holdout_cases} passed · active v${bench.active.version}${learning}`;
  }

  const testButton = $('run-collision-tests');
  if (testButton) {
    testButton.disabled = Boolean(testReport.running);
    testButton.textContent = testReport.running ? `▶ FLIGHT ${testReport.active_case || 1} / ${testReport.total || 8}` : '▶ START MULTI-THREAT FLIGHT';
    $('test-status').textContent = testReport.running
      ? `${testReport.passed || 0} compound maneuvers verified · craft ${testReport.active_case || 1} is tracking three concurrent objects.`
      : testReport.total ? `${testReport.passed}/${testReport.total} three-object encounters cleared · test flight is isolated from the live constellation.`
        : 'Watch three debris objects converge, then see one verified compound maneuver clear all three.';
    if (testReport.cases?.length) {
      $('test-results').innerHTML = testReport.cases.slice().reverse().map(c => `<div class="test-case-row ${c.passed ? 'case-pass' : 'case-fail'}"><b>CASE ${String(c.case).padStart(2, '0')}</b><span>${c.detected ? 'DETECTED' : 'NO WARNING'} · ${escapeHtml(c.satellite)} → ${c.threat_count || c.threat_ids?.length || 1} contacts</span><strong>${c.distance_before_km.toFixed(1)} → ${c.distance_after_km.toFixed(1)} KM</strong>${c.action ? `<small>COMPOUND ΔV (${c.action.dvx.toFixed(3)}, ${c.action.dvy.toFixed(3)}) KM/S · ${c.action.duration_sec}s · ${Object.entries(c.action.threat_distances_km || {}).map(([id, distance]) => `${escapeHtml(id)} ${Number(distance).toFixed(1)} km`).join(' · ')} · ${escapeHtml(c.message)}</small>` : `<small>${escapeHtml(c.message)}</small>`}</div>`).join('');
    } else if (testReport.running) $('test-results').innerHTML = `<div class="test-result-empty">THREE THREATS INBOUND · WAITING FOR THE COMPOUND PLAN AND VERIFIER GATE…</div>`;
  }

  const stage = data.mission_stage;
  $('mission-stage-label').textContent = stage?.stage || (testReport.running ? 'TEST FLIGHT' : 'SCANNING');
  document.querySelectorAll('.agent-row[data-agent]').forEach(row => {
    const active = stage?.agent === row.dataset.agent;
    row.classList.toggle('agent-active', Boolean(active));
    const status = row.querySelector('.agent-state');
    if (active) status.textContent = 'ACTIVE';
    else if (row.dataset.agent === 'thruster_controller') status.textContent = 'ARMED';
    else if (row.dataset.agent === 'shared_memory') status.textContent = 'SYNC';
    else if (row.dataset.agent === 'playbook_evaluator' || row.dataset.agent === 'mission_supervisor') status.textContent = 'WATCH';
    else status.textContent = 'READY';
  });
  const flow = data.mission_flow || [];
  const flowPanel = $('agent-flow');
  const followFlow = flowPanel.scrollHeight - flowPanel.scrollTop - flowPanel.clientHeight < 24;
  flowPanel.innerHTML = flow.length ? flow.slice(0, 12).reverse().map(item => `<div class="flow-entry ${stage?.task_id === item.task_id && stage?.stage === item.stage ? 'flow-current' : ''}"><div class="flow-entry-head"><b>${escapeHtml((item.agent || '').replaceAll('_', ' ').toUpperCase())}</b><span>${item.case ? `CASE ${String(item.case).padStart(2, '0')} · ` : ''}${escapeHtml(item.stage || '')}</span></div><p>${escapeHtml(item.message || '')}</p>${item.packet ? `<div class="flow-packet"><i>⇢</i><b>${escapeHtml((item.packet.from || '').replaceAll('_', ' '))} → ${escapeHtml(item.packet.to || 'mission mesh')}</b><span>${escapeHtml(item.packet.payload || '')}</span></div>` : item.shared ? `<small>↳ SHARED: ${escapeHtml(item.shared)}</small>` : ''}</div>`).join('') : '<div class="flow-empty">Waiting for an approaching object.</div>';
  if (followFlow) flowPanel.scrollTop = flowPanel.scrollHeight;

  const provider = (m.provider || 'nvidia').toLowerCase();
  $('model-label').textContent = provider === 'nvidia' ? 'NVIDIA · NEMOTRON' : `${provider.toUpperCase()} · INFERENCE`;
  $('provider-name').textContent = provider === 'nvidia' ? 'Nemotron' : provider === 'crusoe' ? 'Crusoe inference' : 'Local planner';
  $('runtime-name').textContent = m.runtime_mode === 'nemoclaw'
    ? 'NemoClaw / OpenShell sandbox active'
    : `${m.runtime_mode || 'local'} runtime · NemoClaw inactive`;
  const lastSuccess = m.inference_last_success_at
    ? ` · last success ${new Date(m.inference_last_success_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}` : '';
  $('inference-proof').textContent = `${m.inference_successes ?? 0} successful live calls${lastSuccess}`;

  $('fleet-rows').innerHTML = satellites.map(s => `<div class="fleet-row ${s.alive === false ? 'fleet-lost' : ''}"><span class="unit-id"><i></i>${escapeHtml(s.id)}</span><span>${s.x.toFixed(1)}, ${s.y.toFixed(1)}</span><span class="fuel-cell"><span class="fuel-track"><i style="width:${s.fuel}%"></i></span>${Math.round(s.fuel)}%</span><span>${Math.round((s.coverage || 0) * 100)}%</span><span class="fleet-state">${s.alive === false ? 'LOST' : 'NOMINAL'}</span></div>`).join('');

  const events = data.events || [];
  $('event-count').textContent = `${events.length} EVENTS`;
  $('events').innerHTML = events.length ? events.slice(0, 12).map(e => {
    const time = `<span class="event-time">T+${formatTime(e.time || 0)}</span>`;
    if (e.type === 'test_case') return `<div class="event-row"><span class="event-time">CASE ${String(e.case).padStart(2, '0')}</span><span class="event-description">${escapeHtml(e.message)}<small>${e.threat_ids?.length || 1} concurrent threats · worst approach ${Number(e.distance_before_km).toFixed(1)} → ${Number(e.distance_km).toFixed(1)} km · compound Δv ${Number(e.dvx || 0).toFixed(3)}, ${Number(e.dvy || 0).toFixed(3)} km/s · ${Number(e.duration_sec || 0)}s</small></span><span class="event-tag ${e.passed ? '' : 'rejected'}">${e.passed ? 'PASS' : 'FAIL'}</span></div>`;
    if (e.type === 'playbook') return `<div class="event-row"><span class="event-time">EVAL</span><span class="event-description">${escapeHtml(e.message)}<small>Held-out score ${e.baseline_score} → ${e.candidate_score} (${e.delta >= 0 ? '+' : ''}${e.delta}) · ${e.holdout_cases} unseen cases</small></span><span class="event-tag ${e.promoted ? '' : 'rejected'}">${e.promoted ? `V${e.version} LIVE` : 'SHADOW'}</span></div>`;
    if (e.type === 'human_decision') return `<div class="event-row"><span class="event-time">HUMAN</span><span class="event-description">${escapeHtml(e.message)}<small>Mission ${escapeHtml(e.task_id || '')}</small></span><span class="event-tag">${escapeHtml(e.action || 'REVIEW')}</span></div>`;
    const status = e.type === 'approved' ? 'COMMITTED' : e.type === 'plan_verified' ? 'PLAN OK' : e.type === 'needs_human' ? 'REVIEW' : 'REJECTED';
    const label = e.type === 'needs_human' ? `<span class="human-actions"><button data-mission="${escapeHtml(e.task_id)}" data-action="retry">RETRY</button><button data-mission="${escapeHtml(e.task_id)}" data-action="dismiss">DISMISS</button></span>` : '';
    const description = e.type === 'needs_human' ? e.message : `${escapeHtml(e.satellite)} avoidance near ${escapeHtml(e.debris)} · try ${e.attempt || 1}`;
    const detail = e.type === 'needs_human' ? 'Supervisor halted safely and is waiting for an operator decision.' : `${escapeHtml(e.message)} · ${Number(e.distance_km).toFixed(1)} km closest approach`;
    return `<div class="event-row ${e.type === 'needs_human' ? 'event-escalation' : ''}">${time}<span class="event-description">${escapeHtml(description)}<small>${detail}</small>${label}</span><span class="event-tag ${e.type === 'rejected' || e.type === 'needs_human' ? 'rejected' : ''}">${status}</span></div>`;
  }).join('') : `<div class="empty-state"><span>◌</span><b>Tracking ${satellites.length} spacecraft and ${debris.length} objects</b><small>Verified maneuvers will appear here</small></div>`;

  $('updated-at').textContent = `UPDATED ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
}

function connect() {
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
  clearTimeout(reconnectTimer);
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  socket = new WebSocket(`${scheme}://${location.host}/ws/telemetry`);
  socket.onopen = () => {
    setConnection('CONNECTED');
    retry = 1000;
  };
  socket.onmessage = event => { try { render(JSON.parse(event.data)); } catch (error) { console.error('Invalid telemetry frame', error); } };
  socket.onclose = () => {
    setConnection('RECONNECTING', true);
    if (!lastTelemetryAt) showBackendProblem('TELEMETRY DISCONNECTED · CHECK THAT ASTROMESH IS RUNNING');
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, retry);
    retry = Math.min(retry * 1.7, 10000);
  };
  socket.onerror = () => socket.close();
}

connect();
async function refreshState() {
  if (snapshotInFlight) return;
  snapshotInFlight = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  try {
    const response = await fetch('/api/state', { cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw new Error(`API returned ${response.status}`);
    render(await response.json(), 'rest');
  } catch (error) {
    console.warn('AstroMesh telemetry snapshot unavailable:', error);
    const reason = error.name === 'AbortError' ? 'request timed out' : error.message;
    snapshotFailures += 1;
    const unavailable = snapshotFailures >= 3;
    setConnection(unavailable ? 'API OFFLINE' : 'RECONNECTING', unavailable);
    showBackendProblem(`ASTROMESH API UNAVAILABLE · ${reason} · RETRYING…`);
  } finally {
    clearTimeout(timeout);
    snapshotInFlight = false;
  }
}
refreshState();
// Recover quickly if a proxy or suspended tab leaves a WebSocket open but stale.
setInterval(() => {
  if (socket?.readyState !== WebSocket.OPEN || Date.now() - lastTelemetryAt > 1500) refreshState();
  else if (Date.now() - lastSocketAt > 1500) setConnection('REST SYNC');
}, 1000);

async function checkDashboardBuild() {
  try {
    const response = await fetch('/api/build', { cache: 'no-store' });
    if (!response.ok) return;
    const { build } = await response.json();
    if (dashboardBuild && build !== dashboardBuild) location.reload();
    dashboardBuild = build;
  } catch (error) {
    // Build polling is only for development convenience; telemetry has its own recovery path.
  }
}
checkDashboardBuild();
setInterval(checkDashboardBuild, 3000);
window.addEventListener('focus', () => {
  if (Date.now() - lastTelemetryAt > 1000) refreshState();
  if (socket?.readyState === WebSocket.CLOSED) connect();
});
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    refreshState();
    if (socket?.readyState === WebSocket.CLOSED) connect();
  }
});
setTimeout(() => {
  if (!window.updateOrbitalScene) showBackendProblem('3D ENGINE UNAVAILABLE · CHECK THE THREE.JS CDN CONNECTION');
  else if (!lastTelemetryAt) {
    setConnection(snapshotFailures >= 3 ? 'API OFFLINE' : 'CONNECTING', snapshotFailures >= 3);
    showBackendProblem('WAITING FOR ASTROMESH TELEMETRY · CHECK SERVER STATUS');
  }
}, 8000);

$('run-benchmark').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true; button.textContent = 'EVALUATING CANDIDATE…';
  try {
    const response = await fetch('/api/benchmark/run', { method: 'POST' });
    if (!response.ok) throw new Error((await response.json()).detail || 'Benchmark request failed');
  } catch (error) {
    console.error('Benchmark failed', error);
    button.textContent = 'EVALUATION BUSY · TRY AGAIN';
    setTimeout(() => { button.disabled = false; button.innerHTML = 'RUN 48-CASE HELD-OUT EVAL <span>↗</span>'; }, 1800);
    return;
  }
  button.textContent = 'BENCHMARK COMPLETE';
  setTimeout(() => { button.disabled = false; button.innerHTML = 'RUN 48-CASE HELD-OUT EVAL <span>↗</span>'; }, 1800);
});

$('inference-probe').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = 'CONTACTING CRUSOE…';
  try {
    const response = await fetch('/api/inference/probe', { method: 'POST' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || 'Inference probe failed');
    button.textContent = `LIVE · ${Math.round(result.latency_ms)} MS`;
    $('inference-proof').textContent = `${result.successes} successful live calls · confirmed ${new Date(result.last_success_at * 1000).toLocaleTimeString()}`;
  } catch (error) {
    button.textContent = 'PROBE FAILED · RETRY';
    $('inference-proof').textContent = error.message;
    console.error('Crusoe inference probe failed:', error);
  } finally {
    setTimeout(() => { button.disabled = false; button.textContent = 'VERIFY LIVE CALL'; }, 4000);
  }
});

$('run-collision-tests').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true; button.textContent = '▶ STARTING MULTI-THREAT FLIGHT…';
  $('test-status').textContent = 'One spacecraft will face eight encounters with three simultaneous collision paths; the agents will optimize and verify one compound burn per encounter.';
  try {
    const response = await fetch('/api/test-cases/run', { method: 'POST' });
    const report = await response.json();
    if (!response.ok) throw new Error(report.detail || 'Collision test request failed');
    $('test-status').textContent = `${report.passed}/${report.total} three-object encounters cleared by one spacecraft · each compound burn was verified against every threat.`;
    button.textContent = '▶ FLY AGAIN';
  } catch (error) {
    $('test-status').textContent = error.message;
    button.textContent = '▶ RETRY COLLISION TESTS';
    console.error('Collision test suite failed', error);
  } finally { button.disabled = false; }
});

$('events').addEventListener('click', async event => {
  const button = event.target.closest('button[data-mission]');
  if (!button) return;
  button.disabled = true;
  try {
    await fetch(`/api/missions/${encodeURIComponent(button.dataset.mission)}/decision`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: button.dataset.action, note: 'Dashboard operator decision' }),
    });
  } catch (error) { console.error('Could not send operator decision', error); button.disabled = false; }
});
