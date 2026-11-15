/**
 * DHMZ Radar Analyzer — config UI
 */

'use strict';

// ── Shared city list ──────────────────────────────────────────────────────
// Used for both calibration presets and target presets.
const CITIES = [
  // Croatia — major cities
  { name: 'Zagreb',           lat: 45.8150, lon: 15.9819 },
  { name: 'Split',            lat: 43.5081, lon: 16.4402 },
  { name: 'Rijeka',           lat: 45.3271, lon: 14.4422 },
  { name: 'Osijek',           lat: 45.5511, lon: 18.6939 },
  { name: 'Zadar',            lat: 44.1194, lon: 15.2314 },
  { name: 'Pula',             lat: 44.8666, lon: 13.8496 },
  { name: 'Slavonski Brod',   lat: 45.1600, lon: 18.0158 },
  { name: 'Karlovac',         lat: 45.4876, lon: 15.5477 },
  { name: 'Varaždin',         lat: 46.3044, lon: 16.3378 },
  { name: 'Šibenik',          lat: 43.7350, lon: 15.8952 },
  { name: 'Sisak',            lat: 45.4696, lon: 16.3754 },
  { name: 'Dubrovnik',        lat: 42.6507, lon: 18.0944 },
  { name: 'Bjelovar',         lat: 45.8990, lon: 16.8420 },
  { name: 'Koprivnica',       lat: 46.1620, lon: 16.8325 },
  { name: 'Čakovec',          lat: 46.3838, lon: 16.4332 },
  { name: 'Virovitica',       lat: 45.8319, lon: 17.3844 },
  { name: 'Požega',           lat: 45.3401, lon: 17.6855 },
  { name: 'Vukovar',          lat: 45.3516, lon: 18.9994 },
  { name: 'Nova Gradiška',    lat: 45.2546, lon: 17.3826 },
  { name: 'Gospić',           lat: 44.5468, lon: 15.3730 },
  { name: 'Makarska',         lat: 43.2971, lon: 17.0164 },
  { name: 'Rovinj',           lat: 45.0804, lon: 13.6388 },
  { name: 'Opatija',          lat: 45.3387, lon: 14.3051 },
  { name: 'Vinkovci',         lat: 45.2882, lon: 18.8041 },
  { name: 'Knin',             lat: 44.0414, lon: 16.1975 },
  { name: 'Petrinja',         lat: 45.4418, lon: 16.2812 },
  // Neighboring countries — useful calibration anchors near radar edge
  { name: 'Ljubljana (SI)',   lat: 46.0511, lon: 14.5051 },
  { name: 'Graz (AT)',        lat: 47.0707, lon: 15.4395 },
  { name: 'Vienna (AT)',      lat: 48.2082, lon: 16.3738 },
  { name: 'Budapest (HU)',    lat: 47.4979, lon: 19.0402 },
  { name: 'Belgrade (RS)',    lat: 44.8176, lon: 20.4633 },
  { name: 'Sarajevo (BA)',    lat: 43.8563, lon: 18.4131 },
  { name: 'Trieste (IT)',     lat: 45.6495, lon: 13.7768 },
  { name: 'Novi Sad (RS)',    lat: 45.2671, lon: 19.8335 },
];

const ConfigUI = (() => {
  // ── Internal state ─────────────────────────────────────────────────────
  let _calibPoints = [];    // { name, pixel_x, pixel_y, lat, lon }
  let _targets     = [];    // { id, name, lat, lon, enabled }
  let _regions     = {};    // keyed by region type
  let _pendingPx   = null;  // { x, y } pending calibration click

  // Canvas state
  const calib = { canvas: null, ctx: null, imgW: 0, imgH: 0, scale: 1 };
  const sri   = { canvas: null, ctx: null, imgW: 0, imgH: 0, scale: 1,
                  drawing: false, startX: 0, startY: 0 };

  // ── Boot ────────────────────────────────────────────────────────────────
  function init() {
    _setupTabs();
    _setupCalibCanvas();
    _setupSriCanvas();
    _setupTargetForm();
    _setupSaveButton();
    _buildCityPresets();
  }

  // ── Tabs ────────────────────────────────────────────────────────────────
  function _setupTabs() {
    document.querySelectorAll('.tab').forEach(tab => {
      tab.addEventListener('click', () => {
        document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        tab.classList.add('active');
        document.querySelector(`.tab-panel[data-panel="${tab.dataset.tab}"]`)
          ?.classList.add('active');
      });
    });
  }

  // ── Build city preset buttons from shared CITIES list ──────────────────
  function _buildCityPresets() {
    // Calibration presets
    const calibPresets = document.getElementById('calib-city-presets');
    if (calibPresets) {
      calibPresets.innerHTML = CITIES.map(c =>
        `<button class="preset" data-lat="${c.lat}" data-lon="${c.lon}" data-name="${c.name}">${c.name}</button>`
      ).join('');
      calibPresets.querySelectorAll('.preset').forEach(btn => {
        btn.addEventListener('click', () => {
          document.getElementById('cp-lat').value  = btn.dataset.lat;
          document.getElementById('cp-lon').value  = btn.dataset.lon;
          document.getElementById('cp-name').value = btn.dataset.name;
        });
      });
    }

    // Target presets
    const tgtPresets = document.getElementById('target-city-presets');
    if (tgtPresets) {
      tgtPresets.innerHTML = CITIES.map(c =>
        `<button class="preset" data-lat="${c.lat}" data-lon="${c.lon}" data-name="${c.name}">${c.name}</button>`
      ).join('');
      tgtPresets.querySelectorAll('.preset').forEach(btn => {
        btn.addEventListener('click', () => {
          document.getElementById('tgt-lat').value  = btn.dataset.lat;
          document.getElementById('tgt-lon').value  = btn.dataset.lon;
          document.getElementById('tgt-name').value = btn.dataset.name;
        });
      });
    }
  }

  // ── Calibration canvas ─────────────────────────────────────────────────
  function _setupCalibCanvas() {
    calib.canvas = document.getElementById('calib-canvas');
    calib.ctx    = calib.canvas.getContext('2d');

    document.getElementById('btn-load-frame').addEventListener('click', () => {
      _loadFrame(calib, 'calib-status', _drawCalibOverlay);
    });

    calib.canvas.addEventListener('click', e => {
      if (!calib.imgW) return;
      const rect = calib.canvas.getBoundingClientRect();
      _pendingPx = {
        x: (e.clientX - rect.left)  / calib.scale,
        y: (e.clientY - rect.top)   / calib.scale,
      };
      document.getElementById('calib-point-form').classList.remove('hidden');
      _drawCalibOverlay();
    });

    document.getElementById('btn-cp-save').addEventListener('click', () => {
      const name = document.getElementById('cp-name').value.trim();
      const lat  = parseFloat(document.getElementById('cp-lat').value);
      const lon  = parseFloat(document.getElementById('cp-lon').value);
      if (!name || isNaN(lat) || isNaN(lon) || !_pendingPx) return;
      _calibPoints.push({ name, pixel_x: _pendingPx.x, pixel_y: _pendingPx.y, lat, lon });
      _pendingPx = null;
      document.getElementById('calib-point-form').classList.add('hidden');
      _clearCalibForm();
      _renderCalibList();
      _drawCalibOverlay();
    });

    document.getElementById('btn-cp-cancel').addEventListener('click', () => {
      _pendingPx = null;
      document.getElementById('calib-point-form').classList.add('hidden');
      _clearCalibForm();
      _drawCalibOverlay();
    });
  }

  function _clearCalibForm() {
    ['cp-name','cp-lat','cp-lon'].forEach(id =>
      document.getElementById(id).value = '');
  }

  function _drawCalibOverlay() {
    if (!calib.imgW) return;
    const img = calib.canvas._img;
    if (img) calib.ctx.drawImage(img, 0, 0, calib.canvas.width, calib.canvas.height);

    _calibPoints.forEach(p => {
      const dx = p.pixel_x * calib.scale;
      const dy = p.pixel_y * calib.scale;
      calib.ctx.beginPath();
      calib.ctx.arc(dx, dy, 6, 0, Math.PI * 2);
      calib.ctx.fillStyle = '#58a6ff';
      calib.ctx.fill();
      calib.ctx.strokeStyle = '#fff';
      calib.ctx.lineWidth = 1.5;
      calib.ctx.stroke();
      calib.ctx.fillStyle = '#fff';
      calib.ctx.font = 'bold 11px system-ui';
      calib.ctx.fillText(p.name, dx + 9, dy + 4);
    });

    if (_pendingPx) {
      const dx = _pendingPx.x * calib.scale;
      const dy = _pendingPx.y * calib.scale;
      calib.ctx.beginPath();
      calib.ctx.arc(dx, dy, 6, 0, Math.PI * 2);
      calib.ctx.fillStyle = '#ffa657';
      calib.ctx.fill();
      // Crosshair
      calib.ctx.strokeStyle = '#ffa657';
      calib.ctx.lineWidth = 1;
      calib.ctx.beginPath();
      calib.ctx.moveTo(dx - 12, dy); calib.ctx.lineTo(dx + 12, dy);
      calib.ctx.moveTo(dx, dy - 12); calib.ctx.lineTo(dx, dy + 12);
      calib.ctx.stroke();
    }
  }

  function _renderCalibList() {
    const el = document.getElementById('calib-list');
    el.innerHTML = _calibPoints.map((p, i) => `
      <div class="list-item">
        <div class="list-item-label">
          <div>${p.name}</div>
          <div class="list-item-sub">
            px (${Math.round(p.pixel_x)}, ${Math.round(p.pixel_y)}) ·
            ${p.lat.toFixed(4)}°N, ${p.lon.toFixed(4)}°E
          </div>
        </div>
        <button class="btn-remove" data-idx="${i}">✕</button>
      </div>`).join('');
    el.querySelectorAll('.btn-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        _calibPoints.splice(+btn.dataset.idx, 1);
        _renderCalibList();
        _drawCalibOverlay();
      });
    });
  }

  // ── SRI / OCR / crop canvas ─────────────────────────────────────────────
  const _REGION_COLORS = {
    radar_frame:  '#a371f7',   // purple  — highest visual priority
    sri_gradient: '#58a6ff',   // blue
    sri_labels:   '#3fb950',   // green
    timestamp:    '#ffa657',   // orange
  };
  const _REGION_LABELS = {
    radar_frame:  'Radar data area',
    sri_gradient: 'SRI gradient',
    sri_labels:   'SRI labels',
    timestamp:    'Timestamp OCR',
  };

  function _setupSriCanvas() {
    sri.canvas = document.getElementById('sri-canvas');
    sri.ctx    = sri.canvas.getContext('2d');

    document.getElementById('btn-load-frame-sri').addEventListener('click', () => {
      _loadFrame(sri, 'sri-status', _drawSriOverlay);
    });

    sri.canvas.addEventListener('mousedown', e => {
      if (!sri.imgW) return;
      const rect = sri.canvas.getBoundingClientRect();
      sri.drawing = true;
      sri.startX = (e.clientX - rect.left) / sri.scale;
      sri.startY = (e.clientY - rect.top)  / sri.scale;
    });

    sri.canvas.addEventListener('mousemove', e => {
      if (!sri.drawing) return;
      const rect = sri.canvas.getBoundingClientRect();
      _drawSriOverlay({
        liveX: (e.clientX - rect.left) / sri.scale,
        liveY: (e.clientY - rect.top)  / sri.scale,
      });
    });

    sri.canvas.addEventListener('mouseup', e => {
      if (!sri.drawing) return;
      sri.drawing = false;
      const rect = sri.canvas.getBoundingClientRect();
      const ex = (e.clientX - rect.left) / sri.scale;
      const ey = (e.clientY - rect.top)  / sri.scale;
      const x1 = Math.round(Math.min(sri.startX, ex));
      const y1 = Math.round(Math.min(sri.startY, ey));
      const x2 = Math.round(Math.max(sri.startX, ex));
      const y2 = Math.round(Math.max(sri.startY, ey));
      if (x2 - x1 < 5 || y2 - y1 < 5) return;
      const type = document.getElementById('sri-region-type').value;
      _regions[type] = { x1, y1, x2, y2 };
      _renderSriRegionList();
      _drawSriOverlay();
    });

    // Prevent losing the draw on mouseup outside canvas
    document.addEventListener('mouseup', () => { sri.drawing = false; });
  }

  function _drawSriOverlay(live = null) {
    if (!sri.imgW) return;
    const img = sri.canvas._img;
    if (img) sri.ctx.drawImage(img, 0, 0, sri.canvas.width, sri.canvas.height);

    // Draw the radar_frame region last so it's on top
    const drawOrder = ['timestamp', 'sri_labels', 'sri_gradient', 'radar_frame'];
    drawOrder.forEach(type => {
      const r = _regions[type];
      if (!r) return;
      const c = _REGION_COLORS[type];
      const s = sri.scale;
      sri.ctx.strokeStyle = c;
      sri.ctx.lineWidth = type === 'radar_frame' ? 2.5 : 1.5;
      sri.ctx.setLineDash([]);
      sri.ctx.strokeRect(r.x1*s, r.y1*s, (r.x2-r.x1)*s, (r.y2-r.y1)*s);
      sri.ctx.fillStyle = c + (type === 'radar_frame' ? '18' : '28');
      sri.ctx.fillRect(r.x1*s, r.y1*s, (r.x2-r.x1)*s, (r.y2-r.y1)*s);
      sri.ctx.fillStyle = c;
      sri.ctx.font = `bold ${type === 'radar_frame' ? 12 : 11}px system-ui`;
      sri.ctx.fillText(_REGION_LABELS[type], r.x1*s + 4, r.y1*s + 15);
    });

    // Live drag
    if (live && sri.drawing) {
      const x1 = Math.min(sri.startX, live.liveX) * sri.scale;
      const y1 = Math.min(sri.startY, live.liveY) * sri.scale;
      const w  = Math.abs(live.liveX - sri.startX) * sri.scale;
      const h  = Math.abs(live.liveY - sri.startY) * sri.scale;
      const type = document.getElementById('sri-region-type').value;
      sri.ctx.strokeStyle = _REGION_COLORS[type] || '#fff';
      sri.ctx.lineWidth = 1.5;
      sri.ctx.setLineDash([5, 3]);
      sri.ctx.strokeRect(x1, y1, w, h);
      sri.ctx.setLineDash([]);
    }
  }

  function _renderSriRegionList() {
    const el = document.getElementById('sri-regions-list');
    el.innerHTML = Object.entries(_regions).map(([type, r]) => `
      <div class="list-item">
        <div class="list-item-label">
          <div style="color:${_REGION_COLORS[type]}">${_REGION_LABELS[type]}</div>
          <div class="list-item-sub">(${r.x1},${r.y1}) → (${r.x2},${r.y2})
            &nbsp;·&nbsp; ${r.x2-r.x1}×${r.y2-r.y1} px</div>
        </div>
        <button class="btn-remove" data-type="${type}">✕</button>
      </div>`).join('');
    el.querySelectorAll('.btn-remove').forEach(btn => {
      btn.addEventListener('click', () => {
        delete _regions[btn.dataset.type];
        _renderSriRegionList();
        _drawSriOverlay();
      });
    });
  }

  // ── Shared: load frame into a canvas ────────────────────────────────────
  function _loadFrame(state, statusId, redrawFn) {
    const statusEl = document.getElementById(statusId);
    statusEl.textContent = 'Loading…';
    const img = new Image();
    img.onload = () => {
      state.imgW = img.naturalWidth;
      state.imgH = img.naturalHeight;
      const displayW = Math.min(400, img.naturalWidth);
      state.scale = displayW / img.naturalWidth;
      state.canvas.width  = Math.round(img.naturalWidth  * state.scale);
      state.canvas.height = Math.round(img.naturalHeight * state.scale);
      state.canvas._img   = img;
      state.ctx.drawImage(img, 0, 0, state.canvas.width, state.canvas.height);
      statusEl.textContent = `${img.naturalWidth}×${img.naturalHeight} px — click / drag to define regions`;
      redrawFn();
    };
    img.onerror = () => { statusEl.textContent = '✗ Failed — check network'; };
    img.src = '/api/radar/sample-frame?' + Date.now();
  }

  // ── Targets ─────────────────────────────────────────────────────────────
  function _setupTargetForm() {
    document.getElementById('btn-add-target').addEventListener('click', _addTarget);
  }

  function _addTarget() {
    const name = document.getElementById('tgt-name').value.trim();
    const lat  = parseFloat(document.getElementById('tgt-lat').value);
    const lon  = parseFloat(document.getElementById('tgt-lon').value);
    if (!name || isNaN(lat) || isNaN(lon)) return;
    const id = name.toLowerCase().replace(/[^a-z0-9]/g, '_');
    if (!_targets.find(t => t.id === id)) {
      _targets.push({ id, name, lat, lon, enabled: true });
      _renderTargetList();
      ['tgt-name','tgt-lat','tgt-lon'].forEach(i => document.getElementById(i).value = '');
    }
  }

  function _renderTargetList() {
    const el = document.getElementById('target-list');
    el.innerHTML = _targets.map((t, i) => `
      <div class="list-item">
        <div class="list-item-label">
          <div>${t.name}</div>
          <div class="list-item-sub">${t.lat.toFixed(4)}, ${t.lon.toFixed(4)}</div>
        </div>
        <label style="display:flex;align-items:center;gap:5px;cursor:pointer;margin-right:4px">
          <input type="checkbox" ${t.enabled?'checked':''} data-idx="${i}" class="tgt-toggle">
          <span style="font-size:11px;color:#8b949e">on</span>
        </label>
        <button class="btn-remove" data-idx="${i}">✕</button>
      </div>`).join('');
    el.querySelectorAll('.btn-remove').forEach(btn =>
      btn.addEventListener('click', () => {
        _targets.splice(+btn.dataset.idx, 1); _renderTargetList();
      }));
    el.querySelectorAll('.tgt-toggle').forEach(cb =>
      cb.addEventListener('change', () => {
        _targets[+cb.dataset.idx].enabled = cb.checked;
      }));
  }

  // ── Save ─────────────────────────────────────────────────────────────────
  function _setupSaveButton() {
    document.getElementById('btn-save-config').addEventListener('click', async () => {
      const st = document.getElementById('save-status');
      st.textContent = 'Saving…';
      const cfg = _buildConfig();
      const res = await window.apiFetch('/api/config', {
        method: 'POST', body: JSON.stringify(cfg),
      });
      if (res?.ok) {
        window._appConfig = cfg;
        st.textContent = '✓ Saved';
        if (cfg.mapbox_token && !window.map) window.loadConfigAndInitMap();
        setTimeout(() => { st.textContent = ''; }, 3000);
      } else {
        st.textContent = '✗ Failed';
      }
    });
  }

  function _buildConfig() {
    const v  = id => document.getElementById(id)?.value ?? '';
    const n  = id => { const val = parseFloat(v(id)); return isNaN(val) ? null : val; };

    return {
      radar_url:              v('cf-radar-url'),
      poll_interval_seconds:  parseInt(v('cf-poll-interval'))  || 300,
      rain_threshold_mmh:     parseFloat(v('cf-rain-threshold')) || 0.5,
      max_forecast_minutes:   parseFloat(v('cf-max-forecast'))   || 120,
      optical_flow_window:    parseInt(v('cf-flow-window'))      || 4,
      sri_max_color_distance: parseFloat(v('cf-sri-max-dist'))   || 45,

      radar_frame_region: _regions.radar_frame || null,
      bounds_north: n('cf-north'),
      bounds_south: n('cf-south'),
      bounds_west:  n('cf-west'),
      bounds_east:  n('cf-east'),

      calibration_points: _calibPoints,
      targets:            _targets,

      sri_gradient_region: _regions.sri_gradient || null,
      sri_labels_region:   _regions.sri_labels   || null,
      timestamp_region:    _regions.timestamp    || null,

      mqtt: {
        enabled:      document.getElementById('cf-mqtt-enabled')?.checked ?? false,
        host:         v('cf-mqtt-host'),
        port:         parseInt(v('cf-mqtt-port')) || 1883,
        username:     v('cf-mqtt-user') || null,
        password:     v('cf-mqtt-pass') || null,
        topic_prefix: v('cf-mqtt-prefix') || 'dhmz',
      },

      mapbox_token: v('cf-mapbox-token'),
    };
  }

  // ── Load from saved config ────────────────────────────────────────────────
  function loadFromConfig(cfg) {
    if (!cfg) return;
    const sv = (id, val) => {
      const el = document.getElementById(id);
      if (el && val != null) el.value = val;
    };

    sv('cf-radar-url',      cfg.radar_url);
    sv('cf-poll-interval',  cfg.poll_interval_seconds);
    sv('cf-rain-threshold', cfg.rain_threshold_mmh);
    sv('cf-max-forecast',   cfg.max_forecast_minutes);
    sv('cf-flow-window',    cfg.optical_flow_window);
    sv('cf-sri-max-dist',   cfg.sri_max_color_distance);
    sv('cf-north', cfg.bounds_north);
    sv('cf-south', cfg.bounds_south);
    sv('cf-west',  cfg.bounds_west);
    sv('cf-east',  cfg.bounds_east);
    sv('cf-mqtt-host',   cfg.mqtt?.host);
    sv('cf-mqtt-port',   cfg.mqtt?.port);
    sv('cf-mqtt-user',   cfg.mqtt?.username);
    sv('cf-mqtt-pass',   cfg.mqtt?.password);
    sv('cf-mqtt-prefix', cfg.mqtt?.topic_prefix);
    sv('cf-mapbox-token', cfg.mapbox_token);
    if (cfg.mqtt?.enabled) {
      const el = document.getElementById('cf-mqtt-enabled');
      if (el) el.checked = true;
    }

    _calibPoints = cfg.calibration_points || [];
    _targets     = cfg.targets || [];
    _regions = {};
    if (cfg.radar_frame_region)  _regions.radar_frame  = cfg.radar_frame_region;
    if (cfg.sri_gradient_region) _regions.sri_gradient = cfg.sri_gradient_region;
    if (cfg.sri_labels_region)   _regions.sri_labels   = cfg.sri_labels_region;
    if (cfg.timestamp_region)    _regions.timestamp    = cfg.timestamp_region;

    _renderCalibList();
    _renderTargetList();
    _renderSriRegionList();
  }

  document.addEventListener('DOMContentLoaded', init);
  return { loadFromConfig };
})();
