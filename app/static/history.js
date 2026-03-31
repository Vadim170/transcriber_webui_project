const $ = (sel) => document.querySelector(sel);

let lastResults = null;
let brushState = null;
let chartWindowStart = 0;
let chartWindowEnd = 0;
let brushDragging = false;
let brushAnchorMs = null;
let intervalsData = [];
let currentInterval = null;
let overviewSocket = null;
let voiceActivityData = null;
let lastLoadedRangeKey = null;
let loadInFlight = false;

const CHART_H = 160;
const CHART_PAD_L = 56;
const CHART_PAD_R = 16;
const CHART_PAD_T = 12;
const CHART_PAD_B = 28;
const WEEK_MS = 7 * 24 * 3600 * 1000;
const GAP_BREAK_MULTIPLIER = 3;
const AUTO_LOAD_MAX_MS = 24 * 3600 * 1000;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.message || 'Request failed');
  return data;
}

function pad(n, w = 2) {
  return String(n).padStart(w, '0');
}

function toInputValue(date) {
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

function fmtDurationCompact(ms) {
  if (!ms || ms <= 0) return '—';
  const totalMinutes = Math.round(ms / 60000);
  const days = Math.floor(totalMinutes / (60 * 24));
  const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
  const minutes = totalMinutes % 60;
  const parts = [];
  if (days) parts.push(`${days} д`);
  if (hours) parts.push(`${hours} ч`);
  if (minutes || !parts.length) parts.push(`${minutes} мин`);
  return parts.join(' ');
}

function fmtDateTime(date) {
  return date.toLocaleString('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function toLocalDateISO(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function fmtAxisLabel(ts, stepMs) {
  const dt = new Date(ts);
  if (stepMs >= 24 * 3600 * 1000) return `${pad(dt.getDate())}.${pad(dt.getMonth() + 1)}`;
  if (stepMs >= 6 * 3600 * 1000) return `${pad(dt.getDate())}.${pad(dt.getMonth() + 1)} ${pad(dt.getHours())}:00`;
  return `${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
}

function startOfHour(date) {
  const d = new Date(date);
  d.setMinutes(0, 0, 0);
  return d;
}

function startOfDay(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return d;
}

function endOfDay(date) {
  const d = new Date(date);
  d.setHours(23, 59, 59, 0);
  return d;
}

function applyRange(from, to) {
  $('#range-from').value = toInputValue(from);
  $('#range-to').value = toInputValue(to);
  syncBrushFromInputs();
}

function shiftInput(selector, minutes) {
  const dt = fromInputValue($(selector).value) || new Date();
  const shifted = new Date(dt.getTime() + minutes * 60000);
  $(selector).value = toInputValue(shifted);
  syncBrushFromInputs();
}

function applyPreset(preset) {
  const now = new Date();
  let from;
  let to;
  if (preset === 'last-15m') {
    to = now;
    from = new Date(now.getTime() - 15 * 60 * 1000);
  } else if (preset === 'last-hour') {
    to = now;
    from = new Date(now.getTime() - 3600 * 1000);
  } else if (preset === 'previous-hour') {
    to = startOfHour(now);
    from = new Date(to.getTime() - 3600 * 1000);
  } else if (preset === 'today') {
    from = startOfDay(now);
    to = now;
  } else if (preset === 'yesterday') {
    const y = new Date(now);
    y.setDate(y.getDate() - 1);
    from = startOfDay(y);
    to = endOfDay(y);
  } else if (preset === 'week') {
    to = now;
    from = new Date(now.getTime() - WEEK_MS);
  } else if (preset === 'month') {
    to = now;
    from = new Date(now.getTime() - 30 * 24 * 3600 * 1000);
  } else if (preset === 'all') {
    if (intervalsData.length > 0) {
      const minTime = Math.min(...intervalsData.map((iv) => new Date(iv.start_at).getTime()));
      from = new Date(minTime);
    } else {
      from = new Date(now.getTime() - WEEK_MS);
    }
    to = now;
  }
  if (from && to) applyRange(from, to);
}

function updateRangeDurationLabel() {
  const fromDt = fromInputValue($('#range-from').value);
  const toDt = fromInputValue($('#range-to').value);
  const label = $('#range-duration-label');
  if (!label) return;
  if (!fromDt || !toDt || fromDt >= toDt) {
    label.textContent = '—';
    return;
  }
  label.textContent = fmtDurationCompact(toDt.getTime() - fromDt.getTime());
}

function currentRangeKey() {
  const range = selectedRange();
  if (range.error) return null;
  return `${range.fromDt.getTime()}-${range.toDt.getTime()}`;
}

function showIdleResultsHint(message = 'Выберите интервал на таймлайне или задайте От/До, чтобы увидеть текст.') {
  $('#results-count').textContent = '';
  $('#results-list').innerHTML = `<div class="muted" style="padding:20px 0">${esc(message)}</div>`;
}

function updateLoadControls() {
  const loadBtn = $('#load-btn');
  const status = $('#range-status');
  const range = selectedRange();
  if (range.error) {
    loadBtn.style.display = 'none';
    if (!loadInFlight) status.textContent = '';
    return;
  }

  const rangeMs = range.toDt.getTime() - range.fromDt.getTime();
  const rangeKey = `${range.fromDt.getTime()}-${range.toDt.getTime()}`;
  const needsManualLoad = rangeMs >= AUTO_LOAD_MAX_MS && rangeKey !== lastLoadedRangeKey;
  loadBtn.style.display = needsManualLoad ? '' : 'none';
  if (needsManualLoad && !loadInFlight) {
    showIdleResultsHint('Интервал выбран. Нажмите «Загрузить», чтобы получить результаты.');
  }
  if (!loadInFlight && rangeKey === lastLoadedRangeKey) status.textContent = '';
}

async function maybeAutoLoadRange() {
  const range = selectedRange();
  if (range.error || loadInFlight) {
    updateLoadControls();
    return;
  }
  const rangeMs = range.toDt.getTime() - range.fromDt.getTime();
  const rangeKey = `${range.fromDt.getTime()}-${range.toDt.getTime()}`;
  if (rangeMs < AUTO_LOAD_MAX_MS && rangeKey !== lastLoadedRangeKey) {
    await loadRange();
    return;
  }
  updateLoadControls();
}

function updateTimelineWindowLabel() {
  const el = $('#timeline-window-label');
  if (!el || !chartWindowStart || !chartWindowEnd) return;
  el.textContent = `${fmtDateTime(new Date(chartWindowStart))} - ${fmtDateTime(new Date(chartWindowEnd))}`;
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

function renderTimeAxis(innerH, innerW) {
  let svg = '';
  const durationMs = chartWindowEnd - chartWindowStart;
  const maxLabels = Math.max(2, Math.floor(innerW / 110));
  const candidateSteps = [
    30 * 60 * 1000,
    3600 * 1000,
    2 * 3600 * 1000,
    3 * 3600 * 1000,
    6 * 3600 * 1000,
    12 * 3600 * 1000,
    24 * 3600 * 1000,
    2 * 24 * 3600 * 1000,
  ];
  const labelStepMs = candidateSteps.find((s) => durationMs / s <= maxLabels) || (2 * 24 * 3600 * 1000);
  const firstMark = Math.ceil(chartWindowStart / labelStepMs) * labelStepMs;
  for (let ts = firstMark; ts <= chartWindowEnd; ts += labelStepMs) {
    const x = tsToX(ts, innerW);
    svg += `<line x1="${x}" y1="${CHART_PAD_T}" x2="${x}" y2="${CHART_PAD_T + innerH}" stroke="#263250" stroke-width="1" stroke-dasharray="3 3"/>`;
    svg += `<text x="${x}" y="${CHART_H - 6}" fill="#97a3c6" font-size="10" text-anchor="middle">${esc(fmtAxisLabel(ts, labelStepMs))}</text>`;
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
    <rect x="${bx}" y="${CHART_PAD_T}" width="${bw}" height="${innerH}" fill="#7c9cff" opacity="0.16" rx="4"/>
    <line x1="${bx}" y1="${CHART_PAD_T}" x2="${bx}" y2="${CHART_PAD_T + innerH}" stroke="#7c9cff" stroke-width="1.5"/>
    <line x1="${bx + bw}" y1="${CHART_PAD_T}" x2="${bx + bw}" y2="${CHART_PAD_T + innerH}" stroke="#7c9cff" stroke-width="1.5"/>
  `;
}

function renderTimeline() {
  const el = $('#timeline-chart');
  updateTimelineWindowLabel();
  const hasVisibleData = intervalsData.some((iv) => {
    const start = new Date(iv.start_at).getTime();
    const end = new Date(iv.end_at).getTime();
    return end >= chartWindowStart && start <= chartWindowEnd;
  });
  const hasCurrent = currentInterval && (new Date(currentInterval.start_at).getTime() <= chartWindowEnd);
  if (!hasVisibleData && !hasCurrent) {
    el.innerHTML = '<div class="muted" style="text-align:center;padding:40px 0">За последние 7 дней нет интервалов. Можно выбрать любой час вручную или сразу выделить область на таймлайне.</div>';
    return;
  }

  const w = el.clientWidth || 800;
  const innerW = w - CHART_PAD_L - CHART_PAD_R;
  const innerH = CHART_H - CHART_PAD_T - CHART_PAD_B;
  const rowH = 40;
  const rowY = CHART_PAD_T + (innerH - rowH) / 2;

  let svg = `<svg id="timeline-svg" viewBox="0 0 ${w} ${CHART_H}" preserveAspectRatio="none" style="cursor:crosshair;user-select:none">`;
  svg += `<rect x="0" y="0" width="${w}" height="${CHART_H}" fill="transparent"/>`;
  svg += renderTimeAxis(innerH, innerW);
  svg += `<text x="8" y="${rowY + rowH / 2 + 4}" fill="#97a3c6" font-size="11">Интервалы</text>`;
  svg += `<rect x="${CHART_PAD_L}" y="${rowY}" width="${innerW}" height="${rowH}" rx="10" fill="#0d1425" stroke="#263250"/>`;

  intervalsData.forEach((iv, idx) => {
    const startMs = Math.max(chartWindowStart, new Date(iv.start_at).getTime());
    const endMs = Math.min(chartWindowEnd, new Date(iv.end_at).getTime());
    if (endMs <= chartWindowStart || startMs >= chartWindowEnd || endMs <= startMs) return;
    const x1 = tsToX(startMs, innerW);
    const x2 = tsToX(endMs, innerW);
    const rectW = Math.max(3, x2 - x1);
    svg += `<rect x="${x1}" y="${rowY + 3}" width="${rectW}" height="${rowH - 6}" rx="7" fill="#7c9cff" opacity="0.86" class="interval-bar" data-idx="${idx}" style="cursor:pointer">`;
    svg += `<title>${fmtDuration(iv.duration_s)} · ${new Date(iv.start_at).toLocaleString('ru-RU')} – ${new Date(iv.end_at).toLocaleString('ru-RU')}</title>`;
    svg += `</rect>`;
  });

  if (currentInterval) {
    const startMs = Math.max(chartWindowStart, new Date(currentInterval.start_at).getTime());
    const endMs = Math.min(chartWindowEnd, startMs + currentInterval.elapsed_s * 1000);
    if (endMs > startMs) {
      const x1 = tsToX(startMs, innerW);
      const x2 = tsToX(endMs, innerW);
      const rectW = Math.max(3, x2 - x1);
      svg += `<rect x="${x1}" y="${rowY + 3}" width="${rectW}" height="${rowH - 6}" rx="7" fill="#f1c40f" opacity="0.72" stroke="#f1c40f" stroke-width="1" stroke-dasharray="4 3">`;
      svg += `<title>Запись… ${fmtDuration(currentInterval.elapsed_s)}</title>`;
      svg += `<animate attributeName="opacity" values="0.52;0.88;0.52" dur="2s" repeatCount="indefinite"/>`;
      svg += `</rect>`;
    }
  }

  svg += renderBrushOverlay(innerH, innerW);
  svg += '</svg>';
  el.innerHTML = svg;

  const svgEl = document.getElementById('timeline-svg');
  if (!svgEl) return;
  svgEl.addEventListener('pointerdown', (e) => {
    const target = e.target.closest('.interval-bar');
    if (target) {
      showSingleInterval(Number(target.dataset.idx));
      return;
    }
    e.preventDefault();
    brushDragging = true;
    brushAnchorMs = xToMs(e.clientX, svgEl);
    brushState = { startMs: brushAnchorMs, endMs: brushAnchorMs };
    renderTimeline();
  });
}

function showSingleInterval(idx) {
  if (idx < 0 || idx >= intervalsData.length) return;
  renderIntervalResults([intervalsData[idx]]);
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
  if (svgEl) brushState = { startMs: brushAnchorMs, endMs: xToMs(e.clientX, svgEl) };
  renderTimeline();
  syncInputsFromBrush();
});

function syncInputsFromBrush() {
  if (!brushState) return;
  const lo = Math.min(brushState.startMs, brushState.endMs);
  const hi = Math.max(brushState.startMs, brushState.endMs);
  $('#range-from').value = toInputValue(new Date(lo));
  $('#range-to').value = toInputValue(new Date(hi));
  updateRangeDurationLabel();
  maybeAutoLoadRange();
}

function syncBrushFromInputs() {
  if (!chartWindowStart) return;
  const fromDt = fromInputValue($('#range-from').value);
  const toDt = fromInputValue($('#range-to').value);
  updateRangeDurationLabel();
  if (!fromDt || !toDt || fromDt >= toDt) {
    brushState = null;
    renderTimeline();
    updateLoadControls();
    return;
  }
  brushState = { startMs: fromDt.getTime(), endMs: toDt.getTime() };
  renderTimeline();
  maybeAutoLoadRange();
}

function renderIntervalResults(intervals) {
  lastResults = intervals;
  $('#results-count').textContent = `(${intervals.length} интервалов)`;
  const list = $('#results-list');
  if (!intervals.length) {
    list.innerHTML = '<div class="muted" style="padding:20px 0">Нет интервалов в выбранном диапазоне. Попробуйте расширить период или выбрать другой час.</div>';
    return;
  }
  list.innerHTML = intervals.map((iv) => {
    const fromT = iv.start_at ? new Date(iv.start_at).toLocaleString('ru-RU') : '—';
    const toT = iv.end_at ? new Date(iv.end_at).toLocaleString('ru-RU') : '';
    const dur = iv.duration_s != null ? fmtDuration(iv.duration_s) : '';
    let html = '<div class="history-item">';
    html += '<div class="history-item-head">';
    html += `<span class="muted">${esc(fromT)}${toT ? ` – ${esc(toT)}` : ''}</span>`;
    html += dur ? `<span class="muted">${esc(dur)}</span>` : '';
    html += '</div>';
    if (iv.mic_text) {
      html += '<div style="margin-top:8px"><span class="role-pill role-mic">mic</span></div>';
      html += `<div class="history-item-text">${esc(iv.mic_text)}</div>`;
    }
    if (iv.remote_text) {
      html += '<div style="margin-top:8px"><span class="role-pill role-remote">remote</span></div>';
      html += `<div class="history-item-text">${esc(iv.remote_text)}</div>`;
    }
    if (!iv.mic_text && !iv.remote_text) html += '<div class="muted">Пустой интервал</div>';
    html += '</div>';
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
    updateLoadControls();
    return;
  }
  status.textContent = 'Загрузка…';
  $('#load-btn').disabled = true;
  loadInFlight = true;
  updateLoadControls();
  try {
    const data = await api(`/api/intervals?from=${encodeURIComponent(toISO(range.fromDt))}&to=${encodeURIComponent(toISO(range.toDt))}`);
    renderIntervalResults(data.intervals || []);
    lastLoadedRangeKey = `${range.fromDt.getTime()}-${range.toDt.getTime()}`;
    status.textContent = '';
  } catch (e) {
    status.textContent = e.message;
  } finally {
    loadInFlight = false;
    $('#load-btn').disabled = false;
    updateLoadControls();
  }
}

function applyOverviewData(data) {
  intervalsData = [...(data.intervals || [])].sort((a, b) => new Date(a.start_at) - new Date(b.start_at));
  currentInterval = data.current || null;
  const now = Date.now();
  const currentEnd = currentInterval
    ? new Date(currentInterval.start_at).getTime() + currentInterval.elapsed_s * 1000
    : 0;
  chartWindowEnd = Math.max(now, currentEnd);
  chartWindowStart = chartWindowEnd - WEEK_MS;
  renderTimeline();
  if ($('#range-from')?.value && $('#range-to')?.value) syncBrushFromInputs();
  renderVoiceActivityChart();
}

async function refreshOverview(showErrors = false) {
  if (brushDragging) return;
  try {
    const data = await api('/api/intervals/overview');
    applyOverviewData(data);
  } catch (e) {
    if (showErrors) $('#timeline-chart').innerHTML = `<div class="muted" style="text-align:center;padding:40px 0">Ошибка загрузки: ${esc(e.message)}</div>`;
  }
}

function connectOverviewSocket() {
  overviewSocket = io();
  overviewSocket.on('overview_update', async (payload) => {
    if (!payload || brushDragging) return;
    applyOverviewData(payload);
    await loadVoiceActivity();
  });
}

async function loadVoiceActivity() {
  if (!chartWindowStart || !chartWindowEnd) return;
  const from = toLocalDateISO(new Date(chartWindowStart));
  const to = toLocalDateISO(new Date(chartWindowEnd));
  try {
    const data = await api(`/api/voice-activity?type=hourly&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`);
    voiceActivityData = data;
    renderVoiceActivityChart();
  } catch (e) {
    console.error('Failed to load voice activity:', e);
  }
}

function buildLineSegments(series, key, bucketMs, innerW, yFn) {
  const activePoints = series
    .filter((point) => point[key] > 0)
    .map((point) => ({ ...point, x: tsToX(point.ts, innerW), y: yFn(point[key]) }));
  if (!activePoints.length) return { polylines: [], circles: [] };

  const maxGapMs = Math.max(bucketMs * GAP_BREAK_MULTIPLIER, 2 * 3600 * 1000);
  const polylines = [];
  const circles = [];
  let current = [];

  activePoints.forEach((point) => {
    const prev = current[current.length - 1];
    if (prev && point.ts - prev.ts > maxGapMs) {
      if (current.length) polylines.push(current);
      current = [];
    }
    current.push(point);
    circles.push(point);
  });
  if (current.length) polylines.push(current);
  return { polylines, circles };
}

function renderVoiceActivityChart() {
  const el = $('#voice-activity-chart');
  if (!el) return;
  if (!voiceActivityData || !voiceActivityData.series) {
    el.innerHTML = '<div class="chart-empty muted">Активность появится после загрузки недельного окна</div>';
    return;
  }

  const series = (voiceActivityData.series || [])
    .map((item) => ({ ts: new Date(item.ts).getTime(), mic: item.mic || 0, remote: item.remote || 0 }))
    .filter((item) => item.ts >= chartWindowStart && item.ts <= chartWindowEnd)
    .sort((a, b) => a.ts - b.ts);

  if (!series.length || !series.some((item) => item.mic > 0 || item.remote > 0)) {
    el.innerHTML = '<div class="chart-empty muted">За это окно нет голосовой активности</div>';
    return;
  }

  const VAD_H = 150;
  const VAD_PAD_T = 18;
  const VAD_PAD_B = 28;
  const VAD_PAD_R = CHART_PAD_R;
  const innerH = VAD_H - VAD_PAD_T - VAD_PAD_B;
  const w = el.clientWidth || 800;
  const innerW = w - CHART_PAD_L - VAD_PAD_R;
  const bucketMs = voiceActivityData.bucket_ms || 3600 * 1000;
  const maxVal = Math.max(...series.map((item) => Math.max(item.mic, item.remote)), 1);

  function valToY(v) {
    return VAD_PAD_T + innerH - (v / maxVal) * innerH;
  }

  const micSegments = buildLineSegments(series, 'mic', bucketMs, innerW, valToY);
  const remoteSegments = buildLineSegments(series, 'remote', bucketMs, innerW, valToY);

  let gridSvg = '';
  const ySteps = 4;
  for (let i = 0; i <= ySteps; i++) {
    const v = Math.round((maxVal * i) / ySteps);
    const y = valToY(v);
    gridSvg += `<line x1="${CHART_PAD_L}" y1="${y.toFixed(1)}" x2="${w - VAD_PAD_R}" y2="${y.toFixed(1)}" stroke="#263250" stroke-width="1" stroke-dasharray="3 3"/>`;
    gridSvg += `<text x="${CHART_PAD_L - 6}" y="${(y + 4).toFixed(1)}" fill="#97a3c6" font-size="9" text-anchor="end">${v}</text>`;
  }

  const micSvg = micSegments.polylines.map((segment) => (
    `<polyline points="${segment.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}" fill="none" stroke="#7c9cff" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round" opacity="0.88"/>`
  )).join('');
  const remoteSvg = remoteSegments.polylines.map((segment) => (
    `<polyline points="${segment.map((p) => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')}" fill="none" stroke="#2ecc71" stroke-width="2.25" stroke-linejoin="round" stroke-linecap="round" opacity="0.88"/>`
  )).join('');
  const dotSvg = [...micSegments.circles.map((p) => `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.8" fill="#7c9cff"/>`),
    ...remoteSegments.circles.map((p) => `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="2.8" fill="#2ecc71"/>`),
  ].join('');

  const legendX = w - VAD_PAD_R - 212;
  const legendSvg = `
    <line x1="${legendX}" y1="${VAD_PAD_T + 4}" x2="${legendX + 18}" y2="${VAD_PAD_T + 4}" stroke="#7c9cff" stroke-width="2"/>
    <text x="${legendX + 24}" y="${VAD_PAD_T + 8}" fill="#e6ebff" font-size="11">Микрофон (${voiceActivityData.total_mic || 0})</text>
    <line x1="${legendX}" y1="${VAD_PAD_T + 22}" x2="${legendX + 18}" y2="${VAD_PAD_T + 22}" stroke="#2ecc71" stroke-width="2"/>
    <text x="${legendX + 24}" y="${VAD_PAD_T + 26}" fill="#e6ebff" font-size="11">Системный звук (${voiceActivityData.total_remote || 0})</text>
  `;

  const axisSvg = renderTimeAxis(innerH, innerW).replaceAll(`y="${CHART_H - 6}"`, `y="${VAD_H - 6}"`);
  el.innerHTML = `
    <svg viewBox="0 0 ${w} ${VAD_H}" style="width:100%;height:${VAD_H}px;display:block">
      <rect width="${w}" height="${VAD_H}" fill="transparent"/>
      ${gridSvg}
      ${axisSvg}
      ${micSvg}
      ${remoteSvg}
      ${dotSvg}
      ${legendSvg}
    </svg>
  `;
}

async function boot() {
  $('#logout-btn').onclick = async () => {
    await fetch('/api/logout', { method: 'POST' });
    location.href = '/login';
  };

  document.querySelectorAll('.preset-btn').forEach((btn) => {
    btn.onclick = () => applyPreset(btn.dataset.preset);
  });

  document.querySelectorAll('.shift-from-btn').forEach((btn) => {
    btn.onclick = () => shiftInput('#range-from', Number(btn.dataset.minutes));
  });

  document.querySelectorAll('.shift-to-btn').forEach((btn) => {
    btn.onclick = () => shiftInput('#range-to', Number(btn.dataset.minutes));
  });

  $('#load-btn').onclick = loadRange;

  $('#copy-text-btn').onclick = () => {
    if (!lastResults || !lastResults.length) return;
    const text = lastResults.map((iv) => {
      const parts = [];
      const from = iv.start_at ? new Date(iv.start_at).toLocaleString('ru-RU') : '';
      const to = iv.end_at ? new Date(iv.end_at).toLocaleString('ru-RU') : '';
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

  $('#range-from').addEventListener('change', syncBrushFromInputs);
  $('#range-to').addEventListener('change', syncBrushFromInputs);

  showIdleResultsHint();
  applyPreset('last-hour');
  await refreshOverview(true);
  await loadVoiceActivity();
  connectOverviewSocket();
  window.addEventListener('resize', () => {
    renderTimeline();
    renderVoiceActivityChart();
  });
}

boot();
