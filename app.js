/**
 * DHMZ Radar Analyzer — main app
 * Handles: Mapbox map, WebSocket updates, target cards, fetch button.
 */

'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let map = null;
let lastCornersKey = null;
let currentStatus = null;
let wsReconnectTimer = null;
let debugOverlayVisible = false;

// Animation state
let animFrameCount  = 0;
let animTimestamps  = [];
let animIdx         = 0;      // currently displayed frame index
let animPlaying     = false;
let animTimer       = null;
let animSpeedMs     = 600;    // ms per frame

// ── Init ───────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', async () => {
  setupUIHandlers();
  await loadConfigAndInitMap();
  connectWebSocket();
});

// ── Map ────────────────────────────────────────────────────────────────────
async function loadConfigAndInitMap() {
  const cfg = await apiFetch('/api/config') || {};
  window._appConfig = cfg;
  ConfigUI.loadFromConfig(cfg);

  const token = cfg.mapbox_token || '';
  if (!token) {
    document.getElementById('map-overlay-hint').classList.remove('hidden');
    return;
  }
  initMap(token, cfg);
}

function initMap(token, cfg) {
  mapboxgl.accessToken = token;

  // Default centre: Croatia
  const centre = cfg.targets?.length
    ? [cfg.targets[0].lon, cfg.targets[0].lat]
    : [16.4, 45.1];

  map = new mapboxgl.Map({
    container: 'map',
    style: 'mapbox://styles/mapbox/dark-v11',
    center: centre,
    zoom: 7,
    attributionControl: true,
    // Lock to flat Mercator — the radar overlay is a flat raster image and
    // must not be draped around globe curvature or viewed at a pitch.
    projection: 'mercator',
    pitch: 0,
    maxPitch: 0,
    pitchWithRotate: false,
    dragRotate: false,
    touchPitch: false,
  });

  // Belt-and-braces: if a style or user interaction ever re-enables globe/pitch,
  // snap straight back to flat mercator so the overlay stays aligned.
  map.on('style.load', () => {
    if (typeof map.setProjection === 'function') map.setProjection('mercator');
  });
  map.on('pitchend', () => {
    if (map.getPitch() !== 0) map.setPitch(0);
  });

  map.addControl(new mapboxgl.NavigationControl({ showCompass: false }), 'top-right');
  map.addControl(new mapboxgl.ScaleControl({ unit: 'metric' }), 'bottom-right');

  map.on('load', () => {
    addRadarLayer();
    if (currentStatus) refreshMap(currentStatus);
  });
}

function addRadarLayer() {
  if (!map) return;

  // Placeholder corners (Croatia approximate) until real corners arrive
  const corners = [
    [13.0, 47.5],  // TL
    [20.5, 47.5],  // TR
    [20.5, 42.0],  // BR
    [13.0, 42.0],  // BL
  ];

  map.addSource('radar-overlay', {
    type: 'image',
    url: '/api/radar/overlay.png?' + Date.now(),
    coordinates: corners,
  });

  map.addLayer({
    id: 'radar-overlay-layer',
    type: 'raster',
    source: 'radar-overlay',
    paint: {
      'raster-opacity': 0.7,
      'raster-fade-duration': 500,
    },
  });

  // Debug frame overlay — same corners, hidden by default
  map.addSource('debug-overlay', {
    type: 'image',
    url: '/api/radar/debug-frame.png?' + Date.now(),
    coordinates: corners,
  });

  map.addLayer({
    id: 'debug-overlay-layer',
    type: 'raster',
    source: 'debug-overlay',
    layout: { visibility: 'none' },
    paint: {
      'raster-opacity': 0.55,
      'raster-fade-duration': 300,
    },
  });
}

async function refreshMap(status) {
  if (!map || !map.isStyleLoaded()) return;

  // Fetch frame list from backend so we know how many frames are available
  const info = await apiFetch('/api/radar/frames');
  if (info && info.count > 0) {
    const prevCount = animFrameCount;
    animFrameCount  = info.count;
    animTimestamps  = info.timestamps || [];

    // If new frame arrived (count grew), jump to latest before next anim tick
    if (info.count > prevCount) {
      animIdx = info.count - 1;
    }

    // Show latest frame if we're not mid-playback
    if (!animPlaying) {
      _showFrame(info.count - 1);
    }

    _updateAnimUI();
  }

  // Refresh debug overlay if visible
  if (debugOverlayVisible) {
    const dbgSrc = map.getSource('debug-overlay');
    if (dbgSrc) dbgSrc.updateImage({ url: '/api/radar/debug-frame.png?' + Date.now() });
  }

  // Update corners whenever they change (crop/calibration can change after
  // the map's first fit, so this must not be a one-time thing).
  if (window._radarCorners) {
    const key = JSON.stringify(window._radarCorners);
    if (key !== lastCornersKey) {
      setRadarCorners(window._radarCorners);
      lastCornersKey = key;
    }
  }

  // Target markers
  updateTargetMarkers(status.targets || []);
}

// Show one specific frame on the map (by 0-based index into frame_overlays)
function _showFrame(idx) {
  if (!map || !map.isStyleLoaded()) return;
  animIdx = idx;
  const src = map.getSource('radar-overlay');
  if (src) src.updateImage({ url: `/api/radar/overlay/${idx}.png?` + Date.now() });
  _updateAnimUI();
}

// Step animation forward by one frame; wrap around
function _animStep() {
  if (animFrameCount < 1) return;
  const next = (animIdx + 1) % animFrameCount;
  _showFrame(next);
}

function _startAnim() {
  if (animTimer) clearInterval(animTimer);
  animPlaying = true;
  animTimer = setInterval(_animStep, animSpeedMs);
  _updateAnimUI();
}

function _stopAnim() {
  animPlaying = false;
  if (animTimer) { clearInterval(animTimer); animTimer = null; }
  _updateAnimUI();
}

function _updateAnimUI() {
  const btn   = document.getElementById('btn-play');
  const scrub = document.getElementById('anim-scrub');
  const lbl   = document.getElementById('anim-label');
  if (!btn) return;

  btn.textContent = animPlaying ? '⏸' : '▶';
  btn.title = animPlaying ? 'Pause animation' : 'Play animation';

  if (scrub) {
    scrub.max   = Math.max(0, animFrameCount - 1);
    scrub.value = animIdx;
  }

  if (lbl && animTimestamps.length > animIdx && animTimestamps[animIdx]) {
    const d = new Date(animTimestamps[animIdx] + (animTimestamps[animIdx].endsWith('Z') ? '' : 'Z'));
    lbl.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } else if (lbl) {
    lbl.textContent = animFrameCount > 0 ? `${animIdx + 1}/${animFrameCount}` : '';
  }
}

function setRadarCorners(corners) {
  if (!map) return;
  const src = map.getSource('radar-overlay');
  if (src) src.setCoordinates(corners);
  // Keep debug overlay in sync with same corners
  const dbg = map.getSource('debug-overlay');
  if (dbg) dbg.setCoordinates(corners);
}

// ── Target markers ──────────────────────────────────────────────────────────
const _markers = {};

function updateTargetMarkers(targets) {
  if (!map) return;
  const cfg = window._appConfig || {};
  const targetMap = {};
  (cfg.targets || []).forEach(t => { targetMap[t.id] = t; });

  targets.forEach(teta => {
    const def = targetMap[teta.target_id];
    if (!def) return;

    const color = stateColor(teta.state);
    const label = etaLabel(teta);

    if (_markers[teta.target_id]) {
      // Update existing marker's popup
      _markers[teta.target_id].popup.setHTML(markerPopupHTML(teta));
    } else {
      // Create marker
      const el = document.createElement('div');
      el.className = 'target-marker';
      el.style.cssText = `
        width:34px; height:34px; border-radius:50%;
        background:${color}22; border:2.5px solid ${color};
        display:flex; align-items:center; justify-content:center;
        font-size:11px; color:${color}; font-weight:700;
        cursor:pointer; white-space:nowrap;
      `;
      el.textContent = def.name.slice(0, 2).toUpperCase();

      const popup = new mapboxgl.Popup({ offset: 20, closeButton: false })
        .setHTML(markerPopupHTML(teta));

      const marker = new mapboxgl.Marker({ element: el })
        .setLngLat([def.lon, def.lat])
        .setPopup(popup)
        .addTo(map);

      marker.popup = popup;
      _markers[teta.target_id] = marker;
    }
  });
}

function stateColor(state) {
  return { clear: '#8b949e', approaching: '#58a6ff', overhead: '#ffa657',
           departing: '#e3b341', unknown: '#8b949e' }[state] || '#8b949e';
}

function markerPopupHTML(t) {
  const rows = [];
  rows.push(`<strong>${t.target_name}</strong>`);
  rows.push(`<div style="margin:4px 0;color:${stateColor(t.state)};font-weight:600;text-transform:uppercase;font-size:11px">${t.state}</div>`);
  if (t.eta_start_minutes != null)
    rows.push(`Rain in <b>${Math.round(t.eta_start_minutes)} min</b>`);
  if (t.eta_end_minutes != null)
    rows.push(`Clears in <b>${Math.round(t.eta_end_minutes)} min</b>`);
  if (t.current_intensity_mmh > 0)
    rows.push(`Intensity: <b>${t.current_intensity_mmh} mm/h</b>`);
  if (t.leading_edge_distance_km != null)
    rows.push(`Edge: <b>${t.leading_edge_distance_km} km</b>`);
  rows.push(`<div style="margin-top:4px;color:#8b949e;font-size:11px">Confidence: ${Math.round(t.confidence * 100)}%</div>`);
  return `<div style="font-size:13px;line-height:1.6;color:#e6edf3">${rows.join('<br>')}</div>`;
}

function etaLabel(t) {
  if (t.state === 'overhead') return t.eta_end_minutes != null ? `~${Math.round(t.eta_end_minutes)}m` : 'NOW';
  if (t.state === 'approaching') return t.eta_start_minutes != null ? `${Math.round(t.eta_start_minutes)}m` : '?';
  return '';
}

// ── Sidebar cards ───────────────────────────────────────────────────────────
function renderTargetCards(targets) {
  const container = document.getElementById('target-cards');
  if (!targets || targets.length === 0) {
    container.innerHTML = '<div class="card-empty">No targets configured.<br>Open Config → Targets.</div>';
    return;
  }

  container.innerHTML = targets.map(t => {
    const stateClass = `state-${t.state}`;
    const startRow = t.eta_start_minutes != null
      ? `<div class="tc-row"><span>Rain in</span><span>${Math.round(t.eta_start_minutes)} min</span></div>` : '';
    const endRow = t.eta_end_minutes != null
      ? `<div class="tc-row"><span>Clears in</span><span>${Math.round(t.eta_end_minutes)} min</span></div>` : '';
    const distRow = t.leading_edge_distance_km != null
      ? `<div class="tc-row"><span>Edge dist.</span><span>${t.leading_edge_distance_km} km</span></div>` : '';
    const intRow = t.current_intensity_mmh > 0
      ? `<div class="tc-row"><span>Intensity</span><span>${t.current_intensity_mmh} mm/h</span></div>` : '';
    const confPct = Math.round(t.confidence * 100);
    return `
      <div class="target-card" id="card-${t.target_id}">
        <div class="tc-name">${t.target_name}</div>
        <div class="tc-state ${stateClass}">${t.state}</div>
        ${startRow}${endRow}${distRow}${intRow}
        <div class="tc-conf">
          <div class="tc-row"><span>Confidence</span><span>${confPct}%</span></div>
          <div class="conf-bar"><div class="conf-fill" style="width:${confPct}%"></div></div>
        </div>
      </div>`;
  }).join('');
}

// ── Status bar ──────────────────────────────────────────────────────────────
function updateStatusBar(status) {
  const el = document.getElementById('last-updated');
  if (status.last_updated) {
    const d = new Date(status.last_updated + 'Z');
    el.textContent = 'Updated ' + d.toLocaleTimeString();
  }
  const geoBadge = document.getElementById('geo-badge');
  geoBadge.textContent = status.geo_ready ? 'geo ✓' : 'geo not ready';
  geoBadge.className = 'badge ' + (status.geo_ready ? 'badge-ok' : 'badge-warn');

  document.getElementById('frames-badge').textContent =
    `${status.frames_cached} frame${status.frames_cached !== 1 ? 's' : ''}`;
}

// ── WebSocket ───────────────────────────────────────────────────────────────
function connectWebSocket() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onmessage = e => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.type === 'update') {
        currentStatus = msg.status;
        if (msg.corners) window._radarCorners = msg.corners;
        renderTargetCards(msg.status.targets);
        updateStatusBar(msg.status);
        refreshMap(msg.status);
      }
    } catch {}
  };

  ws.onclose = () => {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = setTimeout(connectWebSocket, 5000);
  };

  ws.onerror = () => ws.close();
}

// ── UI handlers ─────────────────────────────────────────────────────────────
function setupUIHandlers() {
  // Fetch now
  document.getElementById('btn-fetch').addEventListener('click', async () => {
    const btn = document.getElementById('btn-fetch');
    btn.disabled = true;
    btn.textContent = '⟳ …';
    await apiFetch('/api/radar/fetch', { method: 'POST' });
    setTimeout(() => { btn.disabled = false; btn.textContent = '⟳ Fetch'; }, 2000);
  });

  // Open / close config drawer
  document.getElementById('btn-config').addEventListener('click', openConfig);
  document.getElementById('btn-close-config').addEventListener('click', closeConfig);
  document.getElementById('config-backdrop').addEventListener('click', closeConfig);

  // Animation play/pause
  document.getElementById('btn-play')?.addEventListener('click', () => {
    animPlaying ? _stopAnim() : _startAnim();
  });

  // Scrubber
  document.getElementById('anim-scrub')?.addEventListener('input', e => {
    _stopAnim();
    _showFrame(+e.target.value);
  });

  // Speed selector
  document.getElementById('anim-speed')?.addEventListener('change', e => {
    animSpeedMs = +e.target.value;
    if (animPlaying) _startAnim();   // restart with new interval
  });

  // Debug overlay toggle
  document.getElementById('btn-debug').addEventListener('click', () => {
    debugOverlayVisible = !debugOverlayVisible;
    const btn = document.getElementById('btn-debug');
    btn.classList.toggle('active', debugOverlayVisible);
    btn.title = debugOverlayVisible ? 'Hide debug frame overlay' : 'Show debug frame overlay';

    if (!map || !map.isStyleLoaded()) return;
    map.setLayoutProperty(
      'debug-overlay-layer', 'visibility',
      debugOverlayVisible ? 'visible' : 'none'
    );
    // Force a fresh fetch so we don't show a stale frame on first open
    if (debugOverlayVisible) {
      const dbgSrc = map.getSource('debug-overlay');
      if (dbgSrc) dbgSrc.updateImage({ url: '/api/radar/debug-frame.png?' + Date.now() });
    }
  });
}

function openConfig() {
  document.getElementById('config-drawer').classList.remove('hidden');
  document.getElementById('config-backdrop').classList.remove('hidden');
}
function closeConfig() {
  document.getElementById('config-drawer').classList.add('hidden');
  document.getElementById('config-backdrop').classList.add('hidden');
}

// ── Utilities ────────────────────────────────────────────────────────────────
async function apiFetch(url, opts = {}) {
  try {
    const r = await fetch(url, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (!r.ok) return null;
    if (r.headers.get('content-type')?.includes('json')) return r.json();
    return null;
  } catch { return null; }
}

window.apiFetch = apiFetch;
window.loadConfigAndInitMap = loadConfigAndInitMap;
