// NDI DVR — watch page
// ----------------------------------------------------------------------------
// State model
//   state.panes[0..3]     per-pane {sourceId, video, chunks, activeChunkId}
//   state.masterUtc       the "now-playing" UTC timestamp (Date)
//   state.windowStart/End bounds currently shown on the timeline (Date)
//   state.rate            playback rate: -10|-5|-2|0.5|1|2|5|10
//   state.playing         bool
//   state.selection       {startUtc, endUtc} | null — for clip export
//   state.markers         array from the timeline API

const BOOT = JSON.parse(document.getElementById('watch-bootstrap').textContent);

const state = {
  panes: [null, null, null, null],
  masterUtc: null,
  windowStart: null,
  windowEnd:   null,
  rate:        1,
  playing:     false,
  selection:   null,
  markers:     [],
};

// ── Utilities ──────────────────────────────────────────────────────────────
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

function parseUtc(iso) {
  if (!iso) return null;
  // The API returns naive ISO strings (UTC). Adding 'Z' forces Date to parse as UTC.
  const s = iso.endsWith('Z') ? iso : iso + 'Z';
  return new Date(s);
}

function fmtUtc(d) {
  if (!d) return '—';
  return d.toISOString().replace('T', ' ').replace(/\.\d+Z$/, 'Z');
}

function fmtClock(d) {
  if (!d) return '—';
  return d.toISOString().substr(11, 8) + 'Z';
}

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

// ── Source pickers ─────────────────────────────────────────────────────────
function buildSourcePickers() {
  $$('select[data-pane-select]').forEach((sel) => {
    const paneIdx = parseInt(sel.dataset.paneSelect, 10);
    const preselect = BOOT.initialIds[paneIdx];
    const hasBlank = paneIdx > 0;  // A is required, B/C/D optional
    if (hasBlank && !sel.querySelector('option[value=""]')) {
      sel.innerHTML = '<option value="">—</option>';
    } else {
      sel.innerHTML = '';
    }
    BOOT.sources.forEach((s) => {
      const opt = document.createElement('option');
      opt.value = String(s.id);
      opt.textContent = s.label;
      if (s.id === preselect) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', () => onPaneSourceChanged(paneIdx, sel.value));
  });
}

// ── Pane setup ─────────────────────────────────────────────────────────────
function paneElements(idx) {
  return {
    container: document.querySelector(`[data-pane="${idx}"]`),
    video:     document.querySelector(`[data-video="${idx}"]`),
    label:     document.querySelector(`[data-label="${idx}"]`),
    stateEl:   document.querySelector(`[data-state="${idx}"]`),
  };
}

function updateGridLayout() {
  const active = state.panes.filter(Boolean).length;
  const grid = $('#video-grid');
  grid.classList.remove('cols-1', 'cols-2', 'cols-3', 'cols-4');
  grid.classList.add(`cols-${Math.max(1, active)}`);
  for (let i = 0; i < 4; i++) {
    const el = paneElements(i);
    const on = !!state.panes[i];
    el.container.hidden = !on;
    el.container.classList.toggle('empty', !on);
    el.container.classList.toggle('primary', i === 0);
    el.video.hidden = !on;
    el.label.hidden = !on;
    el.stateEl.hidden = !on;
    // Only the primary pane is audible; secondaries stay muted.
    el.video.muted = i !== 0;
  }
}

async function onPaneSourceChanged(idx, valueStr) {
  const sourceId = parseInt(valueStr, 10);
  if (!sourceId) {
    state.panes[idx] = null;
    const el = paneElements(idx);
    el.video.removeAttribute('src');
    el.video.load();
    updateGridLayout();
    await refreshTimelines();
    return;
  }
  state.panes[idx] = {
    sourceId,
    video: paneElements(idx).video,
    chunks: [],
    activeChunkId: null,
    nextPreloaded: null,
  };
  updateGridLayout();
  await refreshTimelines();
  if (state.masterUtc) seekAll(state.masterUtc);
}

// ── Timeline data load ─────────────────────────────────────────────────────
async function refreshTimelines() {
  if (!state.windowStart || !state.windowEnd) return;
  for (let i = 0; i < 4; i++) {
    const p = state.panes[i];
    if (!p) continue;
    await loadTimeline(p);
  }
  renderTimeline();
  renderMarkers();
}

async function loadTimeline(pane) {
  const qs = new URLSearchParams({
    source_id: pane.sourceId,
    start:     state.windowStart.toISOString(),
    end:       state.windowEnd.toISOString(),
  });
  const r = await fetch(`/api/timeline/?${qs}`);
  if (!r.ok) { pane.chunks = []; return; }
  const data = await r.json();
  pane.chunks = (data.chunks || []).map((c) => ({
    ...c,
    startedAt: parseUtc(c.started_at),
    endedAt:   parseUtc(c.effective_ended_at || c.ended_at),
  }));
  paneElements(state.panes.indexOf(pane)).label.textContent = data.source.label;
  // Keep marker list from pane 0 (primary) as the canonical set
  if (state.panes[0] === pane) state.markers = data.markers || [];
}

// ── Timeline rendering ─────────────────────────────────────────────────────
function xOfUtc(d) {
  if (!d) return 0;
  const total = state.windowEnd - state.windowStart;
  if (total <= 0) return 0;
  const w = $('#timeline').clientWidth;
  return clamp(((d - state.windowStart) / total) * w, 0, w);
}

function utcOfX(x) {
  const w = $('#timeline').clientWidth;
  const total = state.windowEnd - state.windowStart;
  return new Date(state.windowStart.getTime() + (x / w) * total);
}

function renderTimeline() {
  const chunksEl = $('#tl-chunks');
  chunksEl.innerHTML = '';
  // Lay each pane's chunks in a horizontal strip (A on top, D on bottom).
  const activePanes = state.panes.filter(Boolean);
  const stripH = 22 / Math.max(1, activePanes.length);
  state.panes.forEach((p, i) => {
    if (!p) return;
    const row = activePanes.indexOf(p);
    p.chunks.forEach((c) => {
      const x1 = xOfUtc(c.startedAt);
      const x2 = xOfUtc(c.endedAt);
      const bar = document.createElement('div');
      bar.className = 'tl-chunk';
      if (c.compressed) bar.classList.add('compressed');
      if (c.upload_status === 'pending' || c.upload_status === 'uploading') bar.classList.add('live');
      bar.style.left   = `${x1}px`;
      bar.style.width  = `${Math.max(1, x2 - x1)}px`;
      bar.style.top    = `${22 + row * stripH}px`;
      bar.style.height = `${Math.max(6, stripH - 1)}px`;
      bar.title = `${c.filename}  ${fmtClock(c.startedAt)} → ${fmtClock(c.endedAt)}`;
      chunksEl.appendChild(bar);
    });
  });

  // Axis ticks — pick interval so we get ~6-10 labels
  const axis = $('#tl-axis');
  axis.innerHTML = '';
  const totalSec = (state.windowEnd - state.windowStart) / 1000;
  const stepSec = pickTickStep(totalSec);
  let t = new Date(Math.ceil(state.windowStart.getTime() / (stepSec * 1000)) * (stepSec * 1000));
  while (t <= state.windowEnd) {
    const x = xOfUtc(t);
    const tick = document.createElement('div');
    tick.className = 'tl-tick';
    tick.style.left = `${x}px`;
    const lbl = document.createElement('span');
    lbl.textContent = stepSec < 3600
      ? t.toISOString().substr(11, 5)
      : t.toISOString().substr(8, 2) + ' ' + t.toISOString().substr(11, 5);
    tick.appendChild(lbl);
    axis.appendChild(tick);
    t = new Date(t.getTime() + stepSec * 1000);
  }

  renderSelection();
}

function pickTickStep(totalSec) {
  const steps = [5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 43200, 86400];
  for (const s of steps) if (totalSec / s <= 10) return s;
  return 86400;
}

function renderSelection() {
  const sel = $('#tl-sel');
  if (!state.selection) { sel.hidden = true; return; }
  const x1 = xOfUtc(state.selection.startUtc);
  const x2 = xOfUtc(state.selection.endUtc);
  sel.style.left  = `${Math.min(x1, x2)}px`;
  sel.style.width = `${Math.abs(x2 - x1)}px`;
  sel.hidden = false;
}

function renderMarkers() {
  // Wipe old pins
  $$('.tl-marker').forEach((m) => m.remove());
  const tl = $('#timeline');
  state.markers.forEach((m) => {
    const ts = parseUtc(m.timestamp_utc);
    if (ts < state.windowStart || ts > state.windowEnd) return;
    const pin = document.createElement('div');
    pin.className = 'tl-marker';
    pin.style.left  = `${xOfUtc(ts)}px`;
    pin.style.color = m.color || '#fcd34d';
    pin.title = (m.label || 'marker') + '  @ ' + fmtClock(ts);
    pin.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (ev.shiftKey) deleteMarker(m.id);
      else seekAll(ts);
    });
    tl.appendChild(pin);
  });
}

function moveCursor() {
  if (!state.masterUtc) return;
  $('#tl-cursor').style.left = `${xOfUtc(state.masterUtc)}px`;
  $('#time-readout').textContent = fmtUtc(state.masterUtc) +
    `   (${state.rate > 0 ? state.rate + '×' : state.rate + '× ⏪'})`;
}

// ── Playback engine ────────────────────────────────────────────────────────
function chunkContaining(pane, utc) {
  return pane.chunks.find((c) => c.startedAt <= utc && utc <= c.endedAt) || null;
}

function nextChunkAfter(pane, utc) {
  return pane.chunks.find((c) => c.startedAt > utc) || null;
}

function prevChunkBefore(pane, utc) {
  let best = null;
  for (const c of pane.chunks) {
    if (c.endedAt < utc && (!best || c.startedAt > best.startedAt)) best = c;
  }
  return best;
}

function loadChunkInPane(pane, chunk, offsetSec) {
  if (!chunk || !chunk.available) {
    pane.video.removeAttribute('src');
    pane.video.load();
    pane.activeChunkId = null;
    return;
  }
  if (pane.activeChunkId !== chunk.id) {
    pane.video.src = chunk.stream_url;
    pane.activeChunkId = chunk.id;
    // Let metadata load, then seek to offset.
    pane.video.addEventListener('loadedmetadata', function once() {
      pane.video.removeEventListener('loadedmetadata', once);
      pane.video.currentTime = clamp(offsetSec, 0, pane.video.duration || offsetSec);
      if (state.playing && state.rate > 0) pane.video.play().catch(() => {});
    }, { once: true });
  } else {
    pane.video.currentTime = clamp(offsetSec, 0, pane.video.duration || offsetSec);
  }
}

function seekAll(utc) {
  state.masterUtc = utc;
  for (let i = 0; i < 4; i++) {
    const p = state.panes[i];
    if (!p) continue;
    const c = chunkContaining(p, utc);
    if (c) {
      const offset = (utc - c.startedAt) / 1000;
      loadChunkInPane(p, c, offset);
      setPaneState(i, '');
    } else {
      // Gap — blank the pane but keep layout
      loadChunkInPane(p, null, 0);
      setPaneState(i, 'no recording');
    }
  }
  moveCursor();
}

function setPaneState(idx, text) {
  const el = paneElements(idx).stateEl;
  el.textContent = text;
  el.style.display = text ? '' : 'none';
}

function primary() { return state.panes[0]; }

function setPlaying(on) {
  state.playing = on;
  for (let i = 0; i < 4; i++) {
    const p = state.panes[i];
    if (!p) continue;
    if (on && state.rate > 0) {
      p.video.playbackRate = state.rate;
      p.video.play().catch(() => {});
    } else {
      p.video.pause();
    }
  }
  $('#btn-play').classList.toggle('active', on && state.rate > 0);
}

function setRate(newRate) {
  state.rate = newRate;
  $$('[data-rate]').forEach((b) => b.classList.toggle('active', parseFloat(b.dataset.rate) === newRate));
  if (newRate > 0) {
    for (let i = 0; i < 4; i++) {
      const p = state.panes[i];
      if (p) p.video.playbackRate = newRate;
    }
    if (state.playing) setPlaying(true);
  } else {
    // Reverse: pause native playback; the reverse stepper advances time.
    for (let i = 0; i < 4; i++) {
      const p = state.panes[i];
      if (p) p.video.pause();
    }
  }
}

// Reverse playback: since browsers have no native reverse rate, we step
// currentTime backward in 250 ms ticks. Rate is multiplied by 4 because
// we tick 4× per second.
let _reverseTick = null;
function reverseLoop() {
  if (_reverseTick) clearInterval(_reverseTick);
  _reverseTick = setInterval(() => {
    if (!state.playing || state.rate >= 0) return;
    const stepSec = Math.abs(state.rate) * 0.25;
    const nextUtc = new Date(state.masterUtc.getTime() - stepSec * 1000);
    if (nextUtc < state.windowStart) {
      setPlaying(false);
      return;
    }
    // If we've crossed the current chunk boundary, swap to previous chunk.
    const p = primary();
    if (!p) return;
    const cur = chunkContaining(p, nextUtc);
    if (cur && cur.id === p.activeChunkId) {
      const offset = (nextUtc - cur.startedAt) / 1000;
      for (let i = 0; i < 4; i++) {
        const pane = state.panes[i];
        if (pane && pane.activeChunkId === cur.id) {
          pane.video.currentTime = clamp(offset, 0, pane.video.duration || offset);
        }
      }
      state.masterUtc = nextUtc;
      moveCursor();
    } else {
      seekAll(nextUtc);
    }
  }, 250);
}

// Forward playback: ride the primary video's timeupdate. The master UTC =
// primary's currentChunk.startedAt + currentTime. Secondaries are resynced
// every 500 ms if they drift more than 300 ms.
function onPrimaryTimeUpdate() {
  const p = primary();
  if (!p || !p.activeChunkId) return;
  const chunk = p.chunks.find((c) => c.id === p.activeChunkId);
  if (!chunk) return;
  state.masterUtc = new Date(chunk.startedAt.getTime() + p.video.currentTime * 1000);
  moveCursor();
}

// Chunk boundary handling: when primary's currentTime is within 2 s of the
// end, preload the next chunk in a hidden <video> so the swap is smooth.
function maybePreloadNext() {
  const p = primary();
  if (!p || !p.video.duration) return;
  const remain = p.video.duration - p.video.currentTime;
  if (remain > 2) return;
  const chunk = p.chunks.find((c) => c.id === p.activeChunkId);
  if (!chunk) return;
  const next = nextChunkAfter(p, chunk.endedAt);
  if (next && next.available && p.nextPreloaded !== next.id) {
    // Cheap preload: HEAD/GET first byte of next chunk. Using fetch so the
    // browser warms the TCP / Flask path without paying for the full file.
    fetch(next.stream_url, { headers: { 'Range': 'bytes=0-65535' } }).catch(() => {});
    p.nextPreloaded = next.id;
  }
}

function onPrimaryEnded() {
  // Jump to the next chunk (or the live edge if there isn't one yet).
  const p = primary();
  if (!p) return;
  const chunk = p.chunks.find((c) => c.id === p.activeChunkId);
  const nextUtc = chunk ? new Date(chunk.endedAt.getTime() + 1) : null;
  if (!nextUtc) { setPlaying(false); return; }
  const hasNext = nextChunkAfter(p, chunk.endedAt);
  if (!hasNext) {
    // We hit the live edge — pause and tell the user.
    setPlaying(false);
    setPaneState(0, 'reached live edge');
    return;
  }
  seekAll(nextUtc);
}

// Drift correction for secondary panes (2-4)
function driftCorrect() {
  if (!state.masterUtc) return;
  const targetMs = state.masterUtc.getTime();
  for (let i = 1; i < 4; i++) {
    const p = state.panes[i];
    if (!p || !p.activeChunkId) continue;
    const chunk = p.chunks.find((c) => c.id === p.activeChunkId);
    if (!chunk) continue;
    const paneMs = chunk.startedAt.getTime() + p.video.currentTime * 1000;
    const drift = Math.abs(targetMs - paneMs);
    if (drift > 300) {
      const offset = (targetMs - chunk.startedAt.getTime()) / 1000;
      p.video.currentTime = clamp(offset, 0, p.video.duration || offset);
    }
  }
}
setInterval(driftCorrect, 500);

// ── Timeline interactions ──────────────────────────────────────────────────
function wireTimelineEvents() {
  const tl = $('#timeline');
  let dragStartX = null;
  let dragStartUtc = null;
  let dragged = false;

  tl.addEventListener('mousedown', (e) => {
    if (e.target.classList.contains('tl-marker')) return;
    dragStartX = e.offsetX;
    dragStartUtc = utcOfX(e.offsetX);
    dragged = false;
  });
  tl.addEventListener('mousemove', (e) => {
    if (dragStartX === null) return;
    const dx = Math.abs(e.offsetX - dragStartX);
    if (dx > 3) {
      dragged = true;
      const cur = utcOfX(e.offsetX);
      state.selection = {
        startUtc: dragStartUtc < cur ? dragStartUtc : cur,
        endUtc:   dragStartUtc < cur ? cur          : dragStartUtc,
      };
      renderSelection();
    }
  });
  tl.addEventListener('mouseup', (e) => {
    if (dragStartX === null) return;
    if (!dragged) {
      // Plain click → seek
      seekAll(utcOfX(e.offsetX));
    }
    dragStartX = null;
  });
  tl.addEventListener('mouseleave', () => { dragStartX = null; });
  tl.addEventListener('dblclick', async (e) => {
    const utc = utcOfX(e.offsetX);
    const label = prompt('Marker label (optional):', '');
    if (label === null) return;
    await addMarker(utc, label.trim());
  });

  window.addEventListener('resize', renderTimeline);
}

// ── Markers ────────────────────────────────────────────────────────────────
async function addMarker(utc, label) {
  const p = primary();
  if (!p) return;
  const r = await fetch('/api/markers/', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_id: p.sourceId,
      timestamp_utc: utc.toISOString(),
      label: label,
    }),
  });
  if (r.ok) {
    const m = await r.json();
    state.markers.push(m);
    renderMarkers();
  }
}

async function deleteMarker(id) {
  if (!confirm('Delete marker?')) return;
  await fetch(`/api/markers/${id}`, { method: 'DELETE' });
  state.markers = state.markers.filter((m) => m.id !== id);
  renderMarkers();
}

// ── Clip export ────────────────────────────────────────────────────────────
async function exportClip() {
  if (!state.selection) {
    alert('Drag on the timeline to select an in/out range first.');
    return;
  }
  const p = primary();
  if (!p) return;
  const label = prompt('Clip label (optional):', '') || '';
  const r = await fetch('/api/clips/', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source_id: p.sourceId,
      start_utc: state.selection.startUtc.toISOString(),
      end_utc:   state.selection.endUtc.toISOString(),
      label:     label.trim(),
    }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    alert('Export failed: ' + (err.error || r.status));
    return;
  }
  refreshClipList();
}

async function refreshClipList() {
  const r = await fetch('/api/clips/?per_page=10');
  if (!r.ok) return;
  const data = await r.json();
  const host = $('#clip-list');
  if (!data.clips.length) {
    host.innerHTML = '<div class="text-muted text-sm">No exports yet.</div>';
    return;
  }
  host.innerHTML = '';
  data.clips.forEach((c) => {
    const row = document.createElement('div');
    row.className = 'clip-row';
    const dur = Math.round(c.duration_seconds);
    let status;
    if (c.status === 'done') {
      status = `<a href="/api/clips/${c.id}/download" class="btn btn-primary btn-sm">⬇ Download</a>`;
    } else if (c.status === 'failed') {
      status = `<span style="color:var(--danger)" title="${(c.error_message || '').replace(/"/g, '&quot;')}">✗ failed</span>`;
    } else {
      status = `<div class="progress"><div style="width:${c.progress}%"></div></div>
                <span class="mono">${c.status} ${c.progress}%</span>`;
    }
    row.innerHTML = `
      <span class="mono">${(c.start_utc || '').substr(11, 8)}</span>
      <span>→</span>
      <span class="mono">${(c.end_utc   || '').substr(11, 8)}</span>
      <span class="text-muted">${dur}s</span>
      <span style="flex:1">${c.label || ''}</span>
      ${status}
      <button class="btn btn-ghost btn-sm" data-del="${c.id}">✕</button>
    `;
    host.appendChild(row);
  });
  host.querySelectorAll('[data-del]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      if (!confirm('Delete clip?')) return;
      await fetch(`/api/clips/${btn.dataset.del}`, { method: 'DELETE' });
      refreshClipList();
    });
  });
}

// Poll clip list every 3s so progress updates live while an export runs.
setInterval(refreshClipList, 3000);

// ── Controls wiring ────────────────────────────────────────────────────────
function wireControls() {
  $('#btn-play').addEventListener('click', () => setPlaying(true));
  $('#btn-pause').addEventListener('click', () => setPlaying(false));
  $$('[data-rate]').forEach((b) => {
    b.addEventListener('click', () => {
      setRate(parseFloat(b.dataset.rate));
      setPlaying(true);
    });
  });

  $('#btn-mark').addEventListener('click', async () => {
    if (!state.masterUtc) return;
    const label = prompt('Marker label (optional):', '');
    if (label === null) return;
    await addMarker(state.masterUtc, label.trim());
  });

  $('#btn-set-in').addEventListener('click', () => {
    if (!state.masterUtc) return;
    const end = state.selection ? state.selection.endUtc : new Date(state.masterUtc.getTime() + 60000);
    state.selection = {
      startUtc: state.masterUtc,
      endUtc:   end > state.masterUtc ? end : new Date(state.masterUtc.getTime() + 60000),
    };
    renderSelection();
  });
  $('#btn-set-out').addEventListener('click', () => {
    if (!state.masterUtc) return;
    const start = state.selection ? state.selection.startUtc : new Date(state.masterUtc.getTime() - 60000);
    state.selection = {
      startUtc: start < state.masterUtc ? start : new Date(state.masterUtc.getTime() - 60000),
      endUtc:   state.masterUtc,
    };
    renderSelection();
  });
  $('#btn-export').addEventListener('click', exportClip);

  $('#btn-goto').addEventListener('click', () => {
    const val = $('#goto-time').value;
    if (!val) return;
    // datetime-local produces a naive local string. Treat it as UTC.
    const utc = new Date(val + 'Z');
    recenterWindow(utc);
    seekAll(utc);
  });
  $('#btn-live').addEventListener('click', () => {
    const now = new Date();
    recenterWindow(now);
    seekAll(now);
  });

  $('#window-size').addEventListener('change', () => {
    const secs = parseInt($('#window-size').value, 10);
    const center = state.masterUtc || new Date();
    state.windowStart = new Date(center.getTime() - secs * 500);
    state.windowEnd   = new Date(center.getTime() + secs * 500);
    refreshTimelines();
  });

  // Primary video events → master clock
  const pv = paneElements(0).video;
  pv.addEventListener('timeupdate', () => {
    onPrimaryTimeUpdate();
    maybePreloadNext();
  });
  pv.addEventListener('ended', onPrimaryEnded);
}

function recenterWindow(center) {
  const secs = parseInt($('#window-size').value, 10);
  state.windowStart = new Date(center.getTime() - secs * 500);
  state.windowEnd   = new Date(center.getTime() + secs * 500);
  refreshTimelines();
}

// ── Boot ───────────────────────────────────────────────────────────────────
async function boot() {
  buildSourcePickers();

  // Initial pane state from BOOT.initialIds
  BOOT.initialIds.forEach((id, idx) => {
    if (id) {
      state.panes[idx] = {
        sourceId: id,
        video:    paneElements(idx).video,
        chunks: [],
        activeChunkId: null,
        nextPreloaded: null,
      };
    }
  });
  updateGridLayout();

  const startT = BOOT.initialT ? parseUtc(BOOT.initialT + (BOOT.initialT.endsWith('Z') ? '' : 'Z')) : new Date();
  recenterWindow(startT);
  seekAll(startT);

  wireTimelineEvents();
  wireControls();
  reverseLoop();
  refreshClipList();
}

boot();
