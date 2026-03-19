const $ = (sel) => document.querySelector(sel);

let overviewRefreshTimer = null;
let retranscribePollTimer = null;
let lastResults = null;
let latestRetranscribeResult = null;
let activeRetranscribeJobId = null;
let overviewBuckets = [];
let audioCoverage = { mic: [], remote: [] };
let audioCoverageEnabled = true;
let brushState = null;
let chartWindowStart = 0;
let chartWindowEnd = 0;
let manualBrushSelection = false;
let brushDragging = false;
let brushAnchorMs = null;
let activeBrushSvgId = null;

const CHART_H = 160;
const CHART_PAD_L = 48;
const CHART_PAD_R = 16;
const CHART_PAD_T = 12;
const CHART_PAD_B = 24;
const BUCKET_MS = 120 * 1000;

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

function applyPreset(preset) {
  const now = new Date();
  let from;
  let to;
  if (preset === '5m') {
    to = now;
    from = new Date(now - 5 * 60 * 1000);
  } else if (preset === '15m') {
    to = now;
    from = new Date(now - 15 * 60 * 1000);
  } else if (preset === '1h') {
    to = now;
    from = new Date(now - 60 * 60 * 1000);
  } else if (preset === '4h') {
    to = now;
    from = new Date(now - 4 * 60 * 60 * 1000);
  } else if (preset === 'today') {
    from = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
    to = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59);
  } else if (preset === 'yesterday') {
    const y = new Date(now);
    y.setDate(y.getDate() - 1);
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

function rerenderAllOverviews() {
  renderOverview();
  renderAudioOverview();
}

function currentBrushSvgEl() {
  return activeBrushSvgId ? document.getElementById(activeBrushSvgId) : null;
}

function handleGlobalBrushMove(e) {
  if (!brushDragging) return;
  const svgEl = currentBrushSvgEl();
  if (!svgEl) return;
  brushState = { startMs: brushAnchorMs, endMs: xToMs(e.clientX, svgEl) };
  rerenderAllOverviews();
}

function handleGlobalBrushUp(e) {
  if (!brushDragging) return;
  const svgEl = currentBrushSvgEl();
  brushDragging = false;
  if (svgEl) {
    brushState = { startMs: brushAnchorMs, endMs: xToMs(e.clientX, svgEl) };
  }
  rerenderAllOverviews();
  syncInputsFromBrush();
}

function attachBrushEvents(svgId) {
  const svgEl = document.getElementById(svgId);
  if (!svgEl) return;
  svgEl.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    manualBrushSelection = true;
    brushDragging = true;
    activeBrushSvgId = svgId;
    brushAnchorMs = xToMs(e.clientX, svgEl);
    brushState = { startMs: brushAnchorMs, endMs: brushAnchorMs };
    rerenderAllOverviews();
  });
}

window.addEventListener('pointermove', handleGlobalBrushMove);
window.addEventListener('pointerup', handleGlobalBrushUp);

function buildOverviewChart(buckets, windowFrom = null, windowTo = null) {
  overviewBuckets = buckets || [];
  chartWindowEnd = windowTo ? windowTo.getTime() : Date.now();
  chartWindowStart = windowFrom ? windowFrom.getTime() : (chartWindowEnd - 24 * 3600 * 1000);
  renderOverview();
}

function renderOverview() {
  const el = $('#overview-chart');
  const buckets = overviewBuckets;
  if (!buckets.length) {
    el.innerHTML = '<div class="muted" style="text-align:center;padding:40px 0">Нет данных за последние 24 часа</div>';
    return;
  }
  const w = el.clientWidth || 800;
  const innerW = w - CHART_PAD_L - CHART_PAD_R;
  const innerH = CHART_H - CHART_PAD_T - CHART_PAD_B;
  const windowDur = chartWindowEnd - chartWindowStart || 1;
  const barW = Math.max(1, (BUCKET_MS / windowDur) * innerW - 1);
  const maxVal = Math.max(1, ...buckets.map((b) => b.mic + b.remote));
  let svg = `<svg id="overview-svg" viewBox="0 0 ${w} ${CHART_H}" preserveAspectRatio="none" style="cursor:crosshair;user-select:none">`;
  svg += `<rect x="0" y="0" width="${w}" height="${CHART_H}" fill="transparent"/>`;
  for (let i = 0; i <= 3; i += 1) {
    const y = CHART_PAD_T + (innerH * i / 3);
    svg += `<line x1="${CHART_PAD_L}" y1="${y}" x2="${w - CHART_PAD_R}" y2="${y}" stroke="#263250" stroke-width="1"/>`;
  }
  svg += renderTimeAxis(w, innerH, innerW);
  for (const b of buckets) {
    const x = tsToX(new Date(b.ts).getTime(), innerW);
    const remoteH = (b.remote / maxVal) * innerH;
    const micH = (b.mic / maxVal) * innerH;
    if (remoteH > 0) svg += `<rect x="${x}" y="${CHART_PAD_T + innerH - remoteH}" width="${barW}" height="${remoteH}" fill="#2ecc71" opacity="0.7"/>`;
    if (micH > 0) svg += `<rect x="${x}" y="${CHART_PAD_T + innerH - remoteH - micH}" width="${barW}" height="${micH}" fill="#7c9cff" opacity="0.7"/>`;
  }
  svg += renderBrushOverlay(innerH, innerW);
  svg += '</svg>';
  el.innerHTML = svg;
  attachBrushEvents('overview-svg');
}

function buildAudioOverview(data) {
  audioCoverageEnabled = data.enabled !== false;
  audioCoverage = data.sources || { mic: [], remote: [] };
  renderAudioOverview();
}

function renderAudioOverview() {
  const el = $('#audio-overview-chart');
  if (!audioCoverageEnabled) {
    el.innerHTML = '<div class="muted" style="text-align:center;padding:40px 0">Хранение полного аудио выключено</div>';
    return;
  }
  const micSegments = audioCoverage.mic || [];
  const remoteSegments = audioCoverage.remote || [];
  if (!micSegments.length && !remoteSegments.length) {
    el.innerHTML = '<div class="muted" style="text-align:center;padding:40px 0">Нет доступной полной записи за последние 24 часа</div>';
    return;
  }
  const w = el.clientWidth || 800;
  const innerW = w - CHART_PAD_L - CHART_PAD_R;
  const innerH = CHART_H - CHART_PAD_T - CHART_PAD_B;
  const rowH = 26;
  const rowGap = 18;
  const micY = CHART_PAD_T + 26;
  const remoteY = micY + rowH + rowGap;
  let svg = `<svg id="audio-overview-svg" viewBox="0 0 ${w} ${CHART_H}" preserveAspectRatio="none" style="cursor:crosshair;user-select:none">`;
  svg += `<rect x="0" y="0" width="${w}" height="${CHART_H}" fill="transparent"/>`;
  svg += renderTimeAxis(w, innerH, innerW);
  svg += `<text x="8" y="${micY + 17}" fill="#97a3c6" font-size="11">mic</text>`;
  svg += `<text x="8" y="${remoteY + 17}" fill="#97a3c6" font-size="11">remote</text>`;
  svg += `<rect x="${CHART_PAD_L}" y="${micY}" width="${innerW}" height="${rowH}" rx="6" fill="#0d1425" stroke="#263250"/>`;
  svg += `<rect x="${CHART_PAD_L}" y="${remoteY}" width="${innerW}" height="${rowH}" rx="6" fill="#0d1425" stroke="#263250"/>`;
  for (const seg of micSegments) {
    const x1 = tsToX(new Date(seg.start_at).getTime(), innerW);
    const x2 = tsToX(new Date(seg.end_at).getTime(), innerW);
    svg += `<rect x="${x1}" y="${micY + 2}" width="${Math.max(2, x2 - x1)}" height="${rowH - 4}" rx="5" fill="#7c9cff" opacity="0.85"/>`;
  }
  for (const seg of remoteSegments) {
    const x1 = tsToX(new Date(seg.start_at).getTime(), innerW);
    const x2 = tsToX(new Date(seg.end_at).getTime(), innerW);
    svg += `<rect x="${x1}" y="${remoteY + 2}" width="${Math.max(2, x2 - x1)}" height="${rowH - 4}" rx="5" fill="#2ecc71" opacity="0.85"/>`;
  }
  svg += renderBrushOverlay(innerH, innerW);
  svg += '</svg>';
  el.innerHTML = svg;
  attachBrushEvents('audio-overview-svg');
}

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
    rerenderAllOverviews();
    return;
  }
  brushState = { startMs: fromDt.getTime(), endMs: toDt.getTime() };
  rerenderAllOverviews();
}

function renderResults(data) {
  lastResults = data;
  const panel = $('#results-panel');
  panel.style.display = '';
  $('#results-count').textContent = `(${data.count} фраз)`;
  const list = $('#results-list');
  if (!data.utterances.length) {
    list.innerHTML = '<div class="muted" style="padding:20px 0">Нет фраз в выбранном диапазоне</div>';
    return;
  }
  list.innerHTML = data.utterances.map((u) => {
    const roleClass = u.role === 'mic' ? 'role-mic' : 'role-remote';
    const fromT = u.start_at ? new Date(u.start_at).toLocaleTimeString('ru-RU') : '—';
    const toT = u.end_at ? new Date(u.end_at).toLocaleTimeString('ru-RU') : '';
    const dur = (u.end_s != null && u.start_s != null) ? `${(u.end_s - u.start_s).toFixed(1)} с` : '';
    return `
      <div class="history-item">
        <div class="history-item-head">
          <span class="role-pill ${roleClass}">${esc(u.role)}</span>
          <span class="muted">${esc(fromT)}${toT ? ` – ${esc(toT)}` : ''}</span>
          ${dur ? `<span class="muted">${esc(dur)}</span>` : ''}
          ${u.language ? `<span class="lang-badge">${esc(u.language)}</span>` : ''}
          <span class="muted">${u.words} сл.</span>
        </div>
        <div class="history-item-text">${esc(u.text)}</div>
      </div>`;
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
    const data = await api(`/api/combined?from=${encodeURIComponent(toISO(range.fromDt))}&to=${encodeURIComponent(toISO(range.toDt))}`);
    renderResults(data);
    status.textContent = '';
  } catch (e) {
    status.textContent = e.message;
  } finally {
    $('#load-btn').disabled = false;
  }
}

function parseDownloadName(res, fallback) {
  const header = res.headers.get('Content-Disposition') || '';
  const match = header.match(/filename="?([^"]+)"?/i);
  return match?.[1] || fallback;
}

async function downloadClip(source) {
  const status = $('#download-status');
  const range = selectedRange();
  if (range.error) {
    status.textContent = range.error;
    return;
  }
  status.textContent = `Подготовка ${source}…`;
  $('#download-mic-btn').disabled = true;
  $('#download-remote-btn').disabled = true;
  try {
    const url = `/api/audio/clip?source=${encodeURIComponent(source)}&from=${encodeURIComponent(toISO(range.fromDt))}&to=${encodeURIComponent(toISO(range.toDt))}`;
    const res = await fetch(url);
    if (!res.ok) {
      let message = 'Не удалось скачать аудио.';
      try {
        const data = await res.json();
        message = data.error || data.message || message;
      } catch (_) {}
      throw new Error(message);
    }
    const blob = await res.blob();
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = objectUrl;
    link.download = parseDownloadName(res, `${source}_${toInputValue(range.fromDt).replace(/:/g, '-')}.wav`);
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(objectUrl);
    status.textContent = '';
  } catch (e) {
    status.textContent = e.message;
  } finally {
    $('#download-mic-btn').disabled = false;
    $('#download-remote-btn').disabled = false;
  }
}

function setRetranscribeButtonsEnabled(enabled) {
  $('#retranscribe-btn').disabled = !enabled;
  $('#download-retranscribe-json-btn').disabled = !(enabled && latestRetranscribeResult);
  $('#download-retranscribe-txt-btn').disabled = !(enabled && latestRetranscribeResult);
}

function formatRetranscribeText(result) {
  return (result?.utterances || []).map((u) => {
    const from = u.start_at ? new Date(u.start_at).toLocaleTimeString('ru-RU') : '—';
    const to = u.end_at ? new Date(u.end_at).toLocaleTimeString('ru-RU') : '—';
    return `[${u.role}] ${from} - ${to}\n${u.text}`;
  }).join('\n\n');
}

function downloadTextBlob(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function updateRetranscribePanel(job) {
  const panel = $('#retranscribe-panel');
  panel.style.display = '';
  $('#retranscribe-phase').textContent = job.phase || 'queued';
  $('#retranscribe-progress-bar').style.width = `${Math.max(0, Math.min(100, Number(job.progress_pct || 0)))}%`;
  $('#retranscribe-progress-text').textContent = `${Number(job.progress_pct || 0).toFixed(1)}% · ${job.processed_seconds || 0} / ${job.total_seconds || 0} с`;
  $('#retranscribe-status').textContent = job.error || (job.current_source ? `Источник: ${job.current_source}` : '');
}

async function refreshOverviews(showErrors = false) {
  if (brushDragging) return;
  const overviewTo = manualBrushSelection && chartWindowEnd ? new Date(chartWindowEnd) : new Date();
  const overviewFrom = manualBrushSelection && chartWindowStart ? new Date(chartWindowStart) : new Date(overviewTo.getTime() - 24 * 3600 * 1000);
  try {
    const [textData, audioData] = await Promise.all([
      api('/api/combined/overview'),
      api(`/api/audio/overview?from=${encodeURIComponent(toISO(overviewFrom))}&to=${encodeURIComponent(toISO(overviewTo))}`),
    ]);
    buildOverviewChart(textData.buckets || [], overviewFrom, overviewTo);
    buildAudioOverview(audioData);
    if (!brushState) syncBrushFromInputs();
  } catch (e) {
    if (showErrors) {
      $('#overview-chart').innerHTML = `<div class="muted" style="text-align:center;padding:40px 0">Ошибка загрузки обзора: ${esc(e.message)}</div>`;
      $('#audio-overview-chart').innerHTML = `<div class="muted" style="text-align:center;padding:40px 0">Ошибка загрузки аудио: ${esc(e.message)}</div>`;
    }
  }
}

async function startRetranscribe() {
  const range = selectedRange();
  if (range.error) {
    $('#retranscribe-status').textContent = range.error;
    $('#retranscribe-panel').style.display = '';
    return;
  }
  latestRetranscribeResult = null;
  setRetranscribeButtonsEnabled(false);
  updateRetranscribePanel({ phase: 'starting', progress_pct: 0, processed_seconds: 0, total_seconds: 0, error: '' });
  try {
    const data = await api('/api/audio/retranscribe', {
      method: 'POST',
      body: JSON.stringify({ from: toISO(range.fromDt), to: toISO(range.toDt) }),
    });
    activeRetranscribeJobId = data.job.job_id;
    updateRetranscribePanel(data.job);
    if (retranscribePollTimer) clearInterval(retranscribePollTimer);
    retranscribePollTimer = setInterval(refreshRetranscribeJob, 1500);
    await refreshRetranscribeJob();
  } catch (e) {
    updateRetranscribePanel({ phase: 'error', progress_pct: 0, processed_seconds: 0, total_seconds: 0, error: e.message });
    setRetranscribeButtonsEnabled(true);
  }
}

async function refreshRetranscribeJob() {
  if (!activeRetranscribeJobId) return;
  try {
    const data = await api(`/api/audio/retranscribe/${encodeURIComponent(activeRetranscribeJobId)}`);
    const job = data.job;
    updateRetranscribePanel(job);
    if (job.status === 'done') {
      latestRetranscribeResult = job.result;
      renderResults(job.result);
      setRetranscribeButtonsEnabled(true);
      if (retranscribePollTimer) {
        clearInterval(retranscribePollTimer);
        retranscribePollTimer = null;
      }
    } else if (job.status === 'error') {
      setRetranscribeButtonsEnabled(true);
      if (retranscribePollTimer) {
        clearInterval(retranscribePollTimer);
        retranscribePollTimer = null;
      }
    }
  } catch (e) {
    updateRetranscribePanel({ phase: 'error', progress_pct: 0, processed_seconds: 0, total_seconds: 0, error: e.message });
    setRetranscribeButtonsEnabled(true);
    if (retranscribePollTimer) {
      clearInterval(retranscribePollTimer);
      retranscribePollTimer = null;
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
  $('#download-mic-btn').onclick = () => downloadClip('mic');
  $('#download-remote-btn').onclick = () => downloadClip('remote');
  $('#retranscribe-btn').onclick = startRetranscribe;
  $('#download-retranscribe-json-btn').onclick = () => {
    if (!latestRetranscribeResult) return;
    downloadTextBlob(JSON.stringify(latestRetranscribeResult, null, 2), 'retranscribe_result.json', 'application/json');
  };
  $('#download-retranscribe-txt-btn').onclick = () => {
    if (!latestRetranscribeResult) return;
    downloadTextBlob(formatRetranscribeText(latestRetranscribeResult), 'retranscribe_result.txt', 'text/plain;charset=utf-8');
  };

  $('#copy-json-btn').onclick = () => {
    if (!lastResults) return;
    navigator.clipboard.writeText(JSON.stringify(lastResults, null, 2))
      .then(() => {
        $('#copy-json-btn').textContent = 'Скопировано!';
        setTimeout(() => { $('#copy-json-btn').textContent = 'Скопировать JSON'; }, 1500);
      })
      .catch(() => {});
  };

  $('#range-from').addEventListener('change', () => { manualBrushSelection = false; syncBrushFromInputs(); });
  $('#range-to').addEventListener('change', () => { manualBrushSelection = false; syncBrushFromInputs(); });

  applyPreset('5m');
  setRetranscribeButtonsEnabled(true);
  await refreshOverviews(true);
  overviewRefreshTimer = setInterval(() => { refreshOverviews(false); }, 4000);
  window.addEventListener('resize', rerenderAllOverviews);
}

boot();
