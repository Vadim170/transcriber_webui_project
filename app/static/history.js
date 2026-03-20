const $ = (sel) => document.querySelector(sel);

let overviewRefreshTimer = null;
let lastResults = null;
let brushState = null;
let chartWindowStart = 0;
let chartWindowEnd = 0;
let manualBrushSelection = false;
let brushDragging = false;
let brushAnchorMs = null;
let intervalsData = [];
let currentInterval = null;

const CHART_H = 160;
const CHART_PAD_L = 48;
const CHART_PAD_R = 16;
const CHART_PAD_T = 12;
const CHART_PAD_B = 24;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.message || 'Request failed');
  return data;
}

function toInputValue(date) {
  const pad = (n, w = 2) => String(n).padStart(w, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function fromInputValue(val) {
  return val ? new Date(val) : null;
}

function toISO(date) {
  return date.toISOString();
}

function fmtDuration(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? `${m} мин ${s} с` : `${s} с`;
}

function applyPreset(preset) {
  const now = new Date();
  let from, to;
  if (preset === '5m') {
    to = now; from = new Date(now - 5 * 60 * 1000);
  } else if (preset === '15m') {
    to = now; from = new Date(now - 15 * 60 * 1000);
  } else if (preset === '1h') {
    to = now; from = new Date(now - 60 * 60 * 1000);
  } else if (preset === '4h') {
    to = now; from = new Date(now - 4 * 60 * 60 * 1000);
  } else if (preset === 'today') {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
    to = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59);
  } else if (preset === 'yesterday') {
    const y = new Date(now); y.setDate(y.getDate() - 1);
    from = new Date(y.getFullYear(), y.getMonth(), y.getDate(), 0, 0, 0);
    to = new Date(y.getFullYear(), y.getMonth(), y.getDate(), 23, 59, 59);
  }
  if (from && to) {
    manualBrushSelection = false;
    $('#range-from').value = toInputValue(from);
    $('#range-to').value = toInputValue(to);
    syncBrushFromInputs();
  }
}

function tsToX(ms, innerW) {
  const windowDur = chartWindowEnd - chartWindowStart || 1;
  return CHART_PAD_L + ((ms - chartWindowStart) / windowDur) * innerW;
}

function xToMs(clientX, svgEl) {
  const rect = svgEl.getBoundingClientRect();
  const frac = Math.max(0, Math.min(1, (clientX - rect.left - CHART_PAD_L) / Math.max(1, rect.width - CHART_PAD_L - CHART_PAD_R)));
  return chartWindowStart + frac * (chartWindowEnd - chartWindowStart);
}

function renderTimeAxis(w, innerH, innerW) {
  let svg = '';
  const labelStepMs = 3 * 3600 * 1000;
  const firstMark = Math.ceil(chartWindowStart / labelStepMs) * labelStepMs;
  for (let ts = firstMark; ts <= chartWindowEnd; ts += labelStepMs) {
    const x = tsToX(ts, innerW);
    const dt = new Date(ts);
    const label = `${String(dt.getHours()).padStart(2, '0')}:00`;
    svg += `<line x1="${x}" y1="${CHART_PAD_T}" x2="${x}" y2="${CHART_PAD_T + innerH}" stroke="#263250" stroke-width="1" stroke-dasharray="3 3"/>`;
    svg += `<text x="${x}" y="${CHART_H - 4}" fill="#97a3c6" font-size="10" text-anchor="middle">${esc(label)}</text>`;
  }
  return svg;
}

function renderBrushOverlay(innerH, innerW) {
  if (!brushState) return '';
  const lo = Math.min(brushState.startMs, brushState.endMs);
  const hi = Math.max(brushState.startMs, brushState.endMs);
  const bx = tsToX(lo, innerW);
  const bw = Math.max(1, tsToX(hi, innerW) - bx);
  return `
    <rect x="${bx}" y="${CHART_PAD_T}" width="${bw}" height="${innerH}" fill="#7c9cff" opacity="0.18" rx="3"/>
    <line x1="${bx}" y1="${CHART_PAD_T}" x2="${bx}" y2="${CHART_PAD_T + innerH}" stroke="#7c9cff" stroke-width="1.5"/>
    <line x1="${bx + bw}" y1="${CHART_PAD_T}" x2="${bx + bw}" y2="${CHART_PAD_T + innerH}" stroke="#7c9cff" stroke-width="1.5"/>
  `;
}

function renderTimeline() {
  const el = $('#timeline-chart');
  if (!intervalsData.length && !currentInterval) {
    el.innerHTML = '<div class="muted" style="text-align:center;padding:40px 0">Нет интервалов</div>';
    return;
  }
  const w = el.clientWidth || 800;
  const innerW = w - CHART_PAD_L - CHART_PAD_R;
  const innerH = CHART_H - CHART_PAD_T - CHART_PAD_B;
  const rowH = 36;
  const rowY = CHART_PAD_T + (innerH - rowH) / 2;

  let svg = `<svg id="timeline-svg" viewBox="0 0 ${w} ${CHART_H}" preserveAspectRatio="none" style="cursor:crosshair;user-select:none">`;
  svg += `<rect x="0" y="0" width="${w}" height="${CHART_H}" fill="transparent"/>`;
  svg += renderTimeAxis(w, innerH, innerW);

  // Background track
  svg += `<rect x="${CHART_PAD_L}" y="${rowY}" width="${innerW}" height="${rowH}" rx="8" fill="#0d1425" stroke="#263250"/>`;

  // Transcribed intervals
  for (let i = 0; i < intervalsData.length; i++) {
    const iv = intervalsData[i];
    const startMs = new Date(iv.start_at).getTime();
    const endMs = new Date(iv.end_at).getTime();
    const x1 = tsToX(startMs, innerW);
    const x2 = tsToX(endMs, innerW);
    const rectW = Math.max(3, x2 - x1);
    svg += `<rect x="${x1}" y="${rowY + 2}" width="${rectW}" height="${rowH - 4}" rx="6" fill="#7c9cff" opacity="0.85" class="interval-bar" data-idx="${i}" style="cursor:pointer">`;
    svg += `<title>${fmtDuration(iv.duration_s)} · ${new Date(iv.start_at).toLocaleTimeString('ru-RU')} – ${new Date(iv.end_at).toLocaleTimeString('ru-RU')}</title>`;
    svg += `</rect>`;
  }

  // Current recording interval (pulsing)
  if (currentInterval) {
    const startMs = new Date(currentInterval.start_at).getTime();
    const endMs = startMs + currentInterval.elapsed_s * 1000;
    const x1 = tsToX(startMs, innerW);
    const x2 = tsToX(endMs, innerW);
    const rectW = Math.max(3, x2 - x1);
    svg += `<rect x="${x1}" y="${rowY + 2}" width="${rectW}" height="${rowH - 4}" rx="6" fill="#f1c40f" opacity="0.7" stroke="#f1c40f" stroke-width="1" stroke-dasharray="4 3">`;
    svg += `<title>Запись… ${fmtDuration(currentInterval.elapsed_s)}</title>`;
    svg += `<animate attributeName="opacity" values="0.5;0.85;0.5" dur="2s" repeatCount="indefinite"/>`;
    svg += `</rect>`;
    svg += `<text x="${x1 + rectW / 2}" y="${rowY + rowH / 2 + 4}" fill="#0b1020" font-size="10" font-weight="600" text-anchor="middle">Запись…</text>`;
  }

  svg += renderBrushOverlay(innerH, innerW);
  svg += '</svg>';
  el.innerHTML = svg;

  // Attach brush events
  const svgEl = document.getElementById('timeline-svg');
  if (svgEl) {
    svgEl.addEventListener('pointerdown', (e) => {
      // Check if clicking on an interval bar
      const target = e.target.closest('.interval-bar');
      if (target) {
        const idx = Number(target.dataset.idx);
        showSingleInterval(idx);
        return;
      }
      e.preventDefault();
      manualBrushSelection = true;
      brushDragging = true;
      brushAnchorMs = xToMs(e.clientX, svgEl);
      brushState = { startMs: brushAnchorMs, endMs: brushAnchorMs };
      renderTimeline();
    });
  }
}

function showSingleInterval(idx) {
  if (idx < 0 || idx >= intervalsData.length) return;
  const iv = intervalsData[idx];
  renderIntervalResults([iv]);
}

window.addEventListener('pointermove', (e) => {
  if (!brushDragging) return;
  const svgEl = document.getElementById('timeline-svg');
  if (!svgEl) return;
  brushState = { startMs: brushAnchorMs, endMs: xToMs(e.clientX, svgEl) };
  renderTimeline();
});

window.addEventListener('pointerup', (e) => {
  if (!brushDragging) return;
  const svgEl = document.getElementById('timeline-svg');
  brushDragging = false;
  if (svgEl) {
    brushState = { startMs: brushAnchorMs, endMs: xToMs(e.clientX, svgEl) };
  }
  renderTimeline();
  syncInputsFromBrush();
});

function syncInputsFromBrush() {
  if (!brushState) return;
  const lo = Math.min(brushState.startMs, brushState.endMs);
  const hi = Math.max(brushState.startMs, brushState.endMs);
  $('#range-from').value = toInputValue(new Date(lo));
  $('#range-to').value = toInputValue(new Date(hi));
}

function syncBrushFromInputs() {
  if (!chartWindowStart) return;
  const fromDt = fromInputValue($('#range-from').value);
  const toDt = fromInputValue($('#range-to').value);
  if (!fromDt || !toDt) {
    brushState = null;
    renderTimeline();
    return;
  }
  brushState = { startMs: fromDt.getTime(), endMs: toDt.getTime() };
  renderTimeline();
}

function renderIntervalResults(intervals) {
  lastResults = intervals;
  const panel = $('#results-panel');
  panel.style.display = '';
  $('#results-count').textContent = `(${intervals.length} интервалов)`;
  const list = $('#results-list');
  if (!intervals.length) {
    list.innerHTML = '<div class="muted" style="padding:20px 0">Нет интервалов в выбранном диапазоне</div>';
    return;
  }
  list.innerHTML = intervals.map((iv) => {
    const fromT = iv.start_at ? new Date(iv.start_at).toLocaleTimeString('ru-RU') : '—';
    const toT = iv.end_at ? new Date(iv.end_at).toLocaleTimeString('ru-RU') : '';
    const dur = iv.duration_s != null ? fmtDuration(iv.duration_s) : '';
    let html = `<div class="history-item">`;
    html += `<div class="history-item-head">`;
    html += `<span class="muted">${esc(fromT)}${toT ? ` – ${esc(toT)}` : ''}</span>`;
    html += dur ? `<span class="muted">${esc(dur)}</span>` : '';
    html += `</div>`;
    if (iv.mic_text) {
      html += `<div style="margin-top:8px"><span class="role-pill role-mic">mic</span></div>`;
      html += `<div class="history-item-text">${esc(iv.mic_text)}</div>`;
    }
    if (iv.remote_text) {
      html += `<div style="margin-top:8px"><span class="role-pill role-remote">remote</span></div>`;
      html += `<div class="history-item-text">${esc(iv.remote_text)}</div>`;
    }
    if (!iv.mic_text && !iv.remote_text) {
      html += `<div class="muted">Пустой интервал</div>`;
    }
    html += `</div>`;
    return html;
  }).join('');
}

function selectedRange() {
  const fromVal = $('#range-from').value;
  const toVal = $('#range-to').value;
  if (!fromVal || !toVal) return { error: 'Укажите начало и конец периода.' };
  const fromDt = fromInputValue(fromVal);
  const toDt = fromInputValue(toVal);
  if (!fromDt || !toDt || fromDt >= toDt) return { error: '«От» должно быть раньше «До».' };
  return { fromDt, toDt };
}

async function loadRange() {
  const status = $('#range-status');
  const range = selectedRange();
  if (range.error) {
    status.textContent = range.error;
    return;
  }
  status.textContent = 'Загрузка…';
  $('#load-btn').disabled = true;
  try {
    const data = await api(`/api/intervals?from=${encodeURIComponent(toISO(range.fromDt))}&to=${encodeURIComponent(toISO(range.toDt))}`);
    renderIntervalResults(data.intervals || []);
    status.textContent = '';
  } catch (e) {
    status.textContent = e.message;
  } finally {
    $('#load-btn').disabled = false;
  }
}

async function refreshOverview(showErrors = false) {
  if (brushDragging) return;
  const overviewTo = manualBrushSelection && chartWindowEnd ? new Date(chartWindowEnd) : new Date();
  const overviewFrom = manualBrushSelection && chartWindowStart ? new Date(chartWindowStart) : new Date(overviewTo.getTime() - 24 * 3600 * 1000);
  chartWindowEnd = overviewTo.getTime();
  chartWindowStart = overviewFrom.getTime();
  try {
    const data = await api('/api/intervals/overview');
    intervalsData = data.intervals || [];
    currentInterval = data.current || null;
    renderTimeline();
    if (!brushState) syncBrushFromInputs();
  } catch (e) {
    if (showErrors) {
      $('#timeline-chart').innerHTML = `<div class="muted" style="text-align:center;padding:40px 0">Ошибка загрузки: ${esc(e.message)}</div>`;
    }
  }
}

async function boot() {
  $('#logout-btn').onclick = async () => {
    await fetch('/api/logout', { method: 'POST' });
    location.href = '/login';
  };

  document.querySelectorAll('.preset-btn').forEach((btn) => {
    btn.onclick = () => applyPreset(btn.dataset.preset);
  });

  $('#load-btn').onclick = loadRange;

  $('#copy-text-btn').onclick = () => {
    if (!lastResults || !lastResults.length) return;
    const text = lastResults.map(iv => {
      const parts = [];
      const from = iv.start_at ? new Date(iv.start_at).toLocaleTimeString('ru-RU') : '';
      const to = iv.end_at ? new Date(iv.end_at).toLocaleTimeString('ru-RU') : '';
      parts.push(`[${from} – ${to}]`);
      if (iv.mic_text) parts.push(`[mic] ${iv.mic_text}`);
      if (iv.remote_text) parts.push(`[remote] ${iv.remote_text}`);
      return parts.join('\n');
    }).join('\n\n');
    navigator.clipboard.writeText(text)
      .then(() => {
        $('#copy-text-btn').textContent = 'Скопировано!';
        setTimeout(() => { $('#copy-text-btn').textContent = 'Скопировать текст'; }, 1500);
      })
      .catch(() => {});
  };

  $('#range-from').addEventListener('change', () => { manualBrushSelection = false; syncBrushFromInputs(); });
  $('#range-to').addEventListener('change', () => { manualBrushSelection = false; syncBrushFromInputs(); });

  applyPreset('1h');
  await refreshOverview(true);
  overviewRefreshTimer = setInterval(() => { refreshOverview(false); }, 4000);
  window.addEventListener('resize', renderTimeline);
}

boot();
