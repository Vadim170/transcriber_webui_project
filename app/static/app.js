const $ = (sel) => document.querySelector(sel);

let currentState = null;
let stateSocket = null;
let actionInFlight = false;
let pendingAction = null;
let actionWatchdogTimer = null;
let recentIntervals = [];
let recentIntervalsLoadedAt = 0;
let recentIntervalsLastWriteAt = null;
let recentIntervalsRequest = null;

function saveLastSettings(settings) {
  try {
    localStorage.setItem('transcriber_last_settings', JSON.stringify(settings));
  } catch (e) {
    console.warn('Failed to save settings to localStorage:', e);
  }
}

function loadLastSettings() {
  try {
    const saved = localStorage.getItem('transcriber_last_settings');
    return saved ? JSON.parse(saved) : null;
  } catch (e) {
    console.warn('Failed to load settings from localStorage:', e);
    return null;
  }
}

function fmtBytes(bytes) {
  if (bytes == null) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 ? 0 : 1)} ${units[i]}`;
}

function fmtNum(v, digits = 1) { return v == null ? '—' : Number(v).toFixed(digits); }
function fmtPct(v) { return v == null ? '—' : `${Number(v).toFixed(v >= 10 ? 0 : 1)} %`; }
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

function fmtChartNum(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  const n = Number(v);
  const abs = Math.abs(n);
  let digits = 0;
  if (abs < 10) digits = 2;
  else if (abs < 100) digits = 1;
  const text = n.toFixed(digits);
  return text.replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');
}

function fmtDuration(sec) {
  const total = Math.max(0, Math.round(Number(sec || 0)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h} ч ${m} мин`;
  if (m > 0) return `${m} мин ${s} с`;
  return `${s} с`;
}

function statusClass(v) {
  if (v == null) return '';
  const x = Number(v);
  if (x < 0.9) return 'good';
  if (x <= 1.1) return 'warn';
  return 'bad';
}

async function api(url, options = {}) {
  const res = await fetch(url, { headers: { 'Content-Type': 'application/json' }, ...options });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.message || 'Request failed');
  return data;
}

function toISO(dt) {
  return dt.toISOString();
}

function kv(el, items) {
  el.innerHTML = items.map(([k, v, cls = '']) => `<div class="kv"><div class="k">${esc(k)}</div><div class="v ${cls}">${esc(v)}</div></div>`).join('');
}

function timeFilter(items, seconds) {
  const now = Date.now() / 1000;
  return items.filter(x => x.ts >= now - Number(seconds));
}

function getChartTicks(minY, maxY, count = 4) {
  if (minY === maxY) return [minY];
  return Array.from({ length: count }, (_, i) => maxY - ((maxY - minY) * i / (count - 1)));
}

function renderLineChart(target, seriesDefs, opts = {}) {
  const w = target.clientWidth || 500;
  const h = 220;
  const padL = 56;
  const padR = 18;
  const padT = 18;
  const padB = 18;
  const allVals = seriesDefs.flatMap(s => s.values.map(v => v.y));
  if (!allVals.length) {
    target.innerHTML = '<div class="chart-empty muted">Пока нет данных</div>';
    return;
  }
  const maxY = Math.max(opts.minMaxY || 0, ...allVals, opts.forceMax ?? 0);
  const minY = opts.forceMin ?? Math.min(0, ...allVals);
  const ySpan = Math.max(1e-6, maxY - minY);
  const xMax = Math.max(1, ...seriesDefs.flatMap(s => s.values.map(v => v.x)));
  const innerW = w - padL - padR;
  const innerH = h - padT - padB;
  const ticks = getChartTicks(minY, maxY, 4);
  const formatValue = opts.valueFormatter || fmtChartNum;
  let svg = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`;
  svg += `<rect x="0" y="0" width="${w}" height="${h}" fill="transparent"/>`;
  svg += `<line x1="${padL}" y1="${padT}" x2="${padL}" y2="${h - padB}" stroke="#31405f" stroke-width="1"/>`;
  for (const tick of ticks) {
    const y = padT + ((maxY - tick) / ySpan) * innerH;
    svg += `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="#263250" stroke-width="1"/>`;
    svg += `<text x="${padL - 8}" y="${y + 4}" fill="#97a3c6" font-size="10" text-anchor="end">${esc(formatValue(tick))}</text>`;
  }
  if (opts.forceMax != null && opts.forceMax >= 1 && minY <= 1 && maxY >= 1) {
    const y = h - padB - ((1 - minY) / ySpan) * innerH;
    svg += `<line x1="${padL}" y1="${y}" x2="${w - padR}" y2="${y}" stroke="#f1c40f" stroke-width="1" stroke-dasharray="4 4"/>`;
  }
  for (const s of seriesDefs) {
    const points = s.values.map(v => {
      const x = padL + (v.x / xMax) * innerW;
      const y = h - padB - ((v.y - minY) / ySpan) * innerH;
      return { x, y, value: v.y };
    });
    const pts = points.map(p => `${p.x},${p.y}`).join(' ');
    svg += `<polyline fill="none" stroke="${s.color}" stroke-width="2.5" points="${pts}"/>`;
    for (const p of points) {
      const title = `${s.label || 'series'}: ${formatValue(p.value)}`;
      svg += `<circle cx="${p.x}" cy="${p.y}" r="3.5" fill="${s.color}" class="chart-point"><title>${esc(title)}</title></circle>`;
    }
  }
  svg += '</svg>';
  target.innerHTML = svg;
}

function renderRTFChart(state, rangeSec, el) {
  const items = timeFilter(state.processing_history, rangeSec);
  const normalize = (role) => items.filter(x => x.role === role).map((x, i) => ({ x: i, y: x.rtf || 0 }));
  renderLineChart(el, [
    { label: 'mic', color: '#7c9cff', values: normalize('mic') },
    { label: 'remote', color: '#2ecc71', values: normalize('remote') },
  ], { forceMin: 0, forceMax: 1.2 });
}

function renderLagChart(state, rangeSec, el) {
  const items = timeFilter(state.processing_history, rangeSec);
  const normalize = (role) => items.filter(x => x.role === role).map((x, i) => ({ x: i, y: x.lag || 0 }));
  renderLineChart(el, [
    { label: 'mic', color: '#7c9cff', values: normalize('mic') },
    { label: 'remote', color: '#2ecc71', values: normalize('remote') },
  ], { forceMin: 0 });
}

function renderLangs(target, data) {
  const entries = Object.entries(data || {});
  if (!entries.length) {
    target.innerHTML = '<div class="muted">Пока нет данных</div>';
    return;
  }
  target.innerHTML = entries.map(([k, v]) => `<div class="lang-tag"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
}

function renderRecentIntervals(target, intervals) {
  if (!intervals.length) {
    target.innerHTML = '<div class="muted">Пока нет интервалов за последний час</div>';
    return;
  }
  target.innerHTML = intervals.map((iv) => {
    const fromT = iv.start_at ? new Date(iv.start_at).toLocaleTimeString('ru-RU') : '—';
    const toT = iv.end_at ? new Date(iv.end_at).toLocaleTimeString('ru-RU') : '';
    const dur = iv.duration_s != null ? fmtDuration(iv.duration_s) : '';
    let html = `<div class="log-item">`;
    html += `<div class="log-head"><span>${esc(fromT)}${toT ? ` - ${esc(toT)}` : ''}</span>${dur ? `<span>${esc(dur)}</span>` : ''}</div>`;
    if (iv.mic_text) {
      html += `<div style="margin-top:8px"><span class="role-pill role-mic">mic</span></div>`;
      html += `<div class="log-text">${esc(iv.mic_text)}</div>`;
    }
    if (iv.remote_text) {
      html += `<div style="margin-top:8px"><span class="role-pill role-remote">remote</span></div>`;
      html += `<div class="log-text">${esc(iv.remote_text)}</div>`;
    }
    if (!iv.mic_text && !iv.remote_text) {
      html += `<div class="muted">Пустой интервал</div>`;
    }
    html += `</div>`;
    return html;
  }).join('');
}

async function refreshRecentIntervals(force = false) {
  const target = $('#last-interval');
  const now = Date.now();
  const lastWriteAt = currentState?.session?.last_write_at || null;
  const hasFreshData = (now - recentIntervalsLoadedAt) < 15000;
  const writeChanged = Boolean(lastWriteAt && lastWriteAt !== recentIntervalsLastWriteAt);

  if (!force && recentIntervalsRequest) return recentIntervalsRequest;
  if (!force && hasFreshData && !writeChanged) return;

  const toDt = new Date();
  const fromDt = new Date(toDt.getTime() - 60 * 60 * 1000);

  recentIntervalsRequest = api(`/api/intervals?from=${encodeURIComponent(toISO(fromDt))}&to=${encodeURIComponent(toISO(toDt))}`)
    .then((data) => {
      recentIntervals = (data.intervals || []).sort((a, b) => {
        const aTs = a.start_at ? new Date(a.start_at).getTime() : 0;
        const bTs = b.start_at ? new Date(b.start_at).getTime() : 0;
        return bTs - aTs;
      });
      recentIntervalsLoadedAt = Date.now();
      recentIntervalsLastWriteAt = lastWriteAt;
      renderRecentIntervals(target, recentIntervals);
    })
    .catch((e) => {
      console.warn('Failed to load recent intervals:', e);
      if (!recentIntervals.length) {
        target.innerHTML = '<div class="muted">Не удалось загрузить интервалы за последний час</div>';
      }
    })
    .finally(() => {
      recentIntervalsRequest = null;
    });

  return recentIntervalsRequest;
}

function modelStatusText(m) {
  if (m.loading) return `загрузка ${fmtPct(m.progress_pct)}`;
  if (m.installed) return 'локально';
  if (m.last_error) return `ошибка: ${m.last_error}`;
  return 'не загружена';
}

function renderModels(groups) {
  const target = $('#models-list');
  const summary = $('#models-summary');
  const all = groups.flatMap(g => g.models || []);
  const installed = all.filter(m => m.installed).length;
  const loading = all.filter(m => m.loading).length;
  summary.textContent = `${installed}/${all.length} локально, ${loading} загружается`;
  if (!all.length) {
    target.innerHTML = '<div class="muted">Пока нет моделей</div>';
    return;
  }
  target.innerHTML = groups.map(group => `
    <div class="log-item">
      <div class="log-head"><strong>${esc(group.group)}</strong></div>
      ${(group.models || []).map(m => {
        const progress = Math.max(0, Math.min(100, Number(m.progress_pct || 0)));
        const cacheInfo = m.total_bytes ? `${fmtBytes(m.downloaded_bytes)} / ${fmtBytes(m.total_bytes)}` : fmtBytes(m.downloaded_bytes);
        const preloadDisabled = m.loading || m.available === false;
        const deleteDisabled = m.loading || !m.installed;
        return `
          <div class="log-item" style="margin-top:8px;">
            <div class="log-head">
              <span>${esc(m.label || m.id)}</span>
              <span>${esc(m.backend || '')}</span>
              <span>${esc(modelStatusText(m))}</span>
            </div>
            <div class="muted" style="margin-bottom:4px;">${esc(m.note || '')}</div>
            ${m.available === false && m.error ? `<div class="muted bad" style="margin-bottom:4px;">${esc(m.error)}</div>` : ''}
            <div class="muted" style="margin-bottom:6px;">Кэш: ${esc(cacheInfo)}${m.cache_path ? ` · ${esc(m.cache_path)}` : ''}</div>
            <div style="height:8px; background:#263250; border-radius:999px; overflow:hidden; margin-bottom:8px;">
              <div style="height:100%; width:${progress}%; background:${m.loading ? '#f1c40f' : (m.installed ? '#2ecc71' : '#7c9cff')};"></div>
            </div>
            <div class="button-row">
              <button data-model-action="preload" data-model-id="${esc(m.id)}" ${preloadDisabled ? 'disabled' : ''}>Загрузить</button>
              <button class="ghost" data-model-action="delete" data-model-id="${esc(m.id)}" ${deleteDisabled ? 'disabled' : ''}>Удалить</button>
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `).join('');
}

async function refreshModels() {
  const data = await api('/api/models/status');
  renderModels(data.groups || []);
}

function setupModelSelector(groups, currentModel) {
  const sel = $('#model-select');
  const custom = $('#model-custom');
  const hidden = $('#model');
  const quant = $('#quantization');
  const quantMap = new Map();
  let html = '';
  for (const g of groups) {
    html += `<optgroup label="${esc(g.group)}">`;
    for (const m of g.models) {
      const extraParts = [];
      if (m.note) extraParts.push(m.note);
      if (m.available === false && m.error) extraParts.push(`недоступно: ${m.error}`);
      const extra = extraParts.length ? ` — ${extraParts.join(' · ')}` : '';
      quantMap.set(m.id, m.quantization || ['none']);
      html += `<option value="${esc(m.id)}" ${m.available === false ? 'disabled' : ''}>${esc(m.label || m.id)}${extra}</option>`;
    }
    html += '</optgroup>';
  }
  html += '<optgroup label="Другое"><option value="__custom__">Свой путь к .bin файлу…</option></optgroup>';
  sel.innerHTML = html;

  function syncHidden() {
    if (sel.value === '__custom__') {
      custom.style.display = '';
      hidden.value = custom.value.trim();
      Array.from(quant.options).forEach(opt => { opt.disabled = false; });
    } else {
      custom.style.display = 'none';
      hidden.value = sel.value;
      const supported = quantMap.get(sel.value) || ['none'];
      Array.from(quant.options).forEach(opt => {
        opt.disabled = !supported.includes(opt.value);
      });
      if (!supported.includes(quant.value)) {
        quant.value = supported[0] || 'none';
      }
    }
  }

  sel.addEventListener('change', syncHidden);
  custom.addEventListener('input', syncHidden);

  const allIds = groups.flatMap(g => g.models.map(m => m.id));
  if (currentModel && allIds.includes(currentModel)) {
    sel.value = currentModel;
  } else if (currentModel) {
    sel.value = '__custom__';
    custom.value = currentModel;
  }
  syncHidden();
}

async function loadConfigAndDevices() {
  const [cfgRes, devRes, modRes] = await Promise.all([
    api('/api/config'),
    api('/api/devices'),
    api('/api/models/status'),
  ]);
  const cfg = cfgRes.config;
  const devices = devRes.devices;
  const lastSettings = loadLastSettings();

  const fill = (sel, selected) => {
    const options = ['<option value="">— не использовать —</option>'].concat(devices.map(d => `<option value="${d.id}">${d.id}: ${esc(d.name)} (${d.default_samplerate} Hz)</option>`));
    sel.innerHTML = options.join('');
    sel.value = selected == null ? '' : String(selected);
  };

  fill($('#mic-device'), lastSettings?.mic_device ?? cfg.mic_device);
  fill($('#remote-device'), lastSettings?.remote_device ?? cfg.remote_device);
  setupModelSelector(modRes.groups || [], lastSettings?.model ?? cfg.model ?? '');
  renderModels(modRes.groups || []);
  $('#threads').value = lastSettings?.threads ?? cfg.threads ?? 6;
  $('#quantization').value = lastSettings?.quantization ?? cfg.quantization ?? 'none';
  $('#out-dir').value = cfg.out_dir || './transcripts';
  $('#min-interval-s').value = cfg.min_interval_s || 300;
  $('#max-interval-s').value = cfg.max_interval_s || 600;
  $('#silence-cut-ms').value = cfg.silence_cut_ms || 2000;
  $('#model-select').dispatchEvent(new Event('change'));
}

function currentForm() {
  const parseId = (v) => v === '' ? null : Number(v);
  const formData = {
    mic_device: parseId($('#mic-device').value),
    remote_device: parseId($('#remote-device').value),
    model: $('#model').value.trim(),
    quantization: $('#quantization').value,
    threads: Number($('#threads').value || 6),
    out_dir: $('#out-dir').value.trim(),
    min_interval_s: Number($('#min-interval-s').value || 300),
    max_interval_s: Number($('#max-interval-s').value || 600),
    silence_cut_ms: Number($('#silence-cut-ms').value || 2000),
  };

  saveLastSettings({
    mic_device: formData.mic_device,
    remote_device: formData.remote_device,
    model: formData.model,
    quantization: formData.quantization,
    threads: formData.threads,
  });

  return formData;
}

function renderSessionProgress(sess, ci) {
  const target = $('#session-progress');
  if (!target) return;
  if (!sess.running || !ci) {
    target.innerHTML = '<div class="session-progress-empty muted">Текущий интервал появится после запуска. Здесь будет видно прогресс до минимальной/максимальной длины и сколько речи уже поймал VAD.</div>';
    return;
  }

  const elapsed = Number(ci.elapsed_s || 0);
  const minInterval = Number(sess.min_interval_s || 0);
  const maxInterval = Number(sess.max_interval_s || 0);
  const speechFrames = Number(ci.speech_frames_count || 0);
  const speechSeconds = Number(ci.speech_seconds || 0);
  const vadFrames = Number(ci.vad_frames_count || 0);
  const rmsFrames = Number(ci.rms_frames_count || 0);
  const rmsThreshold = Number(ci.rms_threshold || 0);
  const byChannel = ci.speech_by_channel || {};
  const minPct = minInterval > 0 ? Math.min(100, (elapsed / minInterval) * 100) : 0;
  const maxPct = maxInterval > 0 ? Math.min(100, (elapsed / maxInterval) * 100) : 0;
  let hint = 'Накопление интервала.';
  if (elapsed >= minInterval) {
    hint = ci.channels_silent ? 'Минимум достигнут, тишина уже есть — следующий срез близко.' : 'Минимум достигнут, ждём окно тишины для среза.';
  }

  const channelNames = { mic: 'mic', remote: 'remote' };
  const compactStats = Object.entries(byChannel)
    .map(([role, stats]) => {
      const speech = Number(stats?.speech_frames || 0);
      const vad = Number(stats?.vad_frames || 0);
      const rms = Number(stats?.rms_frames || 0);
      return `${channelNames[role] || role}: ${speech} фр. (VAD ${vad}, RMS ${rms})`;
    })
    .join(' · ');

  target.innerHTML = `
    <div class="session-progress-title">
      <strong>Текущий интервал: ${esc(fmtDuration(elapsed))}</strong>
      <span class="muted">${esc(hint)}</span>
    </div>
    <div class="session-progress-bars">
      <div class="session-progress-row">
        <span>До минимального интервала: ${esc(fmtDuration(elapsed))} / ${esc(fmtDuration(minInterval))}</span>
        <div class="session-progress-track"><div class="session-progress-fill min" style="width:${minPct}%"></div></div>
      </div>
      <div class="session-progress-row">
        <span>До максимального интервала: ${esc(fmtDuration(elapsed))} / ${esc(fmtDuration(maxInterval))}</span>
        <div class="session-progress-track"><div class="session-progress-fill max" style="width:${maxPct}%"></div></div>
      </div>
    </div>
    <div class="session-progress-meta muted">
      <span>Речевые фреймы: ${esc(String(speechFrames))}</span>
      <span>Обнаружено речи: ${esc(fmtNum(speechSeconds, 1))} с</span>
      <span>Тишина по всем каналам: ${ci.channels_silent ? 'да' : 'нет'}</span>
    </div>
    <div class="session-progress-diagnostics muted">
      <span>Общее: VAD ${esc(String(vadFrames))} · RMS ${esc(String(rmsFrames))} · порог RMS ${esc(fmtNum(rmsThreshold, 3))}</span>
      ${compactStats ? `<span>Каналы: ${esc(compactStats)}</span>` : ''}
    </div>
  `;
}

function updateToggleButton(state) {
  const btn = $('#toggle-btn');
  if (!btn) return;
  const sess = state?.session || {};
  btn.classList.remove('danger', 'is-loading');
  btn.disabled = !!actionInFlight;

  if (sess.loading) {
    btn.textContent = 'Запуск...';
    btn.classList.add('is-loading');
    btn.disabled = true;
  } else if (sess.stopping) {
    btn.textContent = 'Останавливается...';
    btn.classList.add('danger', 'is-loading');
    btn.disabled = true;
  } else if (sess.running) {
    btn.textContent = 'Остановить';
    btn.classList.add('danger');
  } else {
    btn.textContent = 'Запустить';
  }
}

function clearActionWatchdog() {
  if (actionWatchdogTimer) {
    clearTimeout(actionWatchdogTimer);
    actionWatchdogTimer = null;
  }
}

function isPendingActionComplete(state) {
  if (!pendingAction || !state?.session) return false;
  const sess = state.session;
  if (pendingAction === 'start') {
    return sess.running && !sess.loading && !sess.stopping;
  }
  if (pendingAction === 'stop') {
    return !sess.running && !sess.loading && !sess.stopping;
  }
  return false;
}

function finalizePendingAction(state) {
  if (!pendingAction) return;
  if (pendingAction === 'start') {
    $('#action-result').textContent = 'Запущено';
  } else if (pendingAction === 'stop') {
    $('#action-result').textContent = 'Остановлено';
  }
  pendingAction = null;
  actionInFlight = false;
  clearActionWatchdog();
  updateToggleButton(state);
}

function scheduleActionWatchdog(attempt = 0) {
  clearActionWatchdog();
  if (!pendingAction) return;
  const delays = [500, 1200, 2500, 4000, 6000];
  const delay = delays[Math.min(attempt, delays.length - 1)];
  actionWatchdogTimer = setTimeout(async () => {
    if (!pendingAction) return;
    try {
      await refreshState();
    } catch (_) {
      // Ignore watchdog fetch errors, next retry may recover.
    }
    if (currentState && isPendingActionComplete(currentState)) {
      finalizePendingAction(currentState);
      return;
    }
    if (attempt < delays.length - 1) {
      scheduleActionWatchdog(attempt + 1);
      return;
    }
    actionInFlight = false;
    pendingAction = null;
    updateToggleButton(currentState);
  }, delay);
}

function updateUI(state) {
  currentState = state;
  const sess = state.session;
  const system = state.system;
  const mic = state.sources.mic;
  const remote = state.sources.remote;
  const ci = state.current_interval;

  if (isPendingActionComplete(state)) {
    finalizePendingAction(state);
  } else if (!pendingAction && !sess.loading && !sess.stopping) {
    actionInFlight = false;
  }

  let serverStatus = 'stopped';
  if (sess.loading) serverStatus = 'loading';
  else if (sess.stopping) serverStatus = 'stopping';
  else if (sess.running) serverStatus = 'running';
  $('#server-status').textContent = serverStatus;

  const intervalStatus = ci ? `Запись: ${fmtDuration(ci.elapsed_s)}` : '—';
  let sessionState = 'Остановлена';
  if (sess.loading) sessionState = 'Запуск...';
  else if (sess.stopping) sessionState = 'Остановка...';
  else if (sess.running) sessionState = 'Идёт запись';
  kv($('#session-metrics'), [
    ['Состояние', sessionState],
    ['Останавливается', sess.stopping ? 'да' : 'нет'],
    ['Загрузка модели', sess.loading ? 'да' : 'нет'],
    ['Модель загружена', sess.model_loaded ? 'да' : 'нет'],
    ['Сессия начата', sess.started_at || '—'],
    ['Текущий интервал', intervalStatus],
    ['Мин. интервал', `${fmtNum(sess.min_interval_s, 0)} с`],
    ['Макс. интервал', `${fmtNum(sess.max_interval_s, 0)} с`],
    ['Тишина для среза', `${fmtNum(sess.silence_cut_ms, 0)} мс`],
    ['Интервалов всего', sess.total_intervals],
    ['Слов всего', sess.total_words],
    ['Последняя запись', sess.last_write_at],
    ['intervals.jsonl', fmtBytes(sess.intervals_file_size)],
    ['Ошибка', sess.server_error || '—', sess.server_error ? 'bad' : ''],
  ]);
  renderSessionProgress(sess, ci);

  kv($('#system-metrics'), [
    ['CPU системы', system.cpu_percent == null ? '—' : `${fmtNum(system.cpu_percent)} %`],
    ['Память системы', system.memory_percent == null ? '—' : `${fmtNum(system.memory_percent)} %`],
    ['CPU процесса', system.process_cpu_percent == null ? '—' : `${fmtNum(system.process_cpu_percent)} %`],
    ['Память процесса', fmtBytes(system.process_rss)],
    ['Потоков процесса', system.threads == null ? '—' : system.threads],
  ]);

  const sourceItems = (src) => [
    ['Активен', src.enabled ? 'да' : 'нет'],
    ['Устройство', src.device_name || '—'],
    ['Статус', src.status || '—'],
    ['Сейчас распознаёт', src.busy ? 'да' : 'нет'],
    ['Очередь', src.queue_size],
    ['Последний аудиокусок, с', fmtNum(src.last_audio_sec, 2)],
    ['Последняя обработка, с', fmtNum(src.last_processing_sec, 2)],
    ['RTF', fmtNum(src.last_rtf, 2), statusClass(src.last_rtf)],
    ['Оценка задержки, с', fmtNum(src.lag_estimate_sec, 2), statusClass(src.lag_estimate_sec > 1 ? 2 : 0)],
    ['Потерянные чанки', src.dropped_chunks],
    ['Слов', src.words],
    ['Последняя запись', src.last_commit_at || '—'],
    ['Ошибка', src.last_error || '—', src.last_error ? 'bad' : ''],
  ];

  kv($('#mic-metrics'), sourceItems(mic));
  kv($('#remote-metrics'), sourceItems(remote));
  renderLangs($('#langs-mic'), {});
  renderLangs($('#langs-remote'), {});
  renderRecentIntervals($('#last-interval'), recentIntervals);
  refreshRecentIntervals();
  renderRTFChart(state, Number($('#range-rtf').value), $('#chart-rtf'));
  renderLagChart(state, Number($('#range-lag').value), $('#chart-lag'));
  updateToggleButton(state);
}

async function refreshState() {
  try {
    const { state } = await api('/api/state');
    updateUI(state);
  } catch (e) {
    $('#server-status').textContent = 'error';
  }
}

function connectStateSocket() {
  stateSocket = io();
  stateSocket.on('connect', () => {
    $('#server-status').textContent = 'online';
  });
  stateSocket.on('disconnect', () => {
    $('#server-status').textContent = 'offline';
  });
  stateSocket.on('state_update', (payload) => {
    if (payload?.state) updateUI(payload.state);
  });
}

async function boot() {
  await loadConfigAndDevices();

  $('#models-list').onclick = async (event) => {
    const btn = event.target.closest('button[data-model-action]');
    if (!btn) return;
    const model = btn.dataset.modelId;
    const action = btn.dataset.modelAction;
    try {
      if (action === 'preload') {
        const data = await api('/api/models/preload', {
          method: 'POST',
          body: JSON.stringify({ model, quantization: $('#quantization').value }),
        });
        $('#action-result').textContent = data.message || 'Загрузка модели запущена';
      } else if (action === 'delete') {
        const data = await api('/api/models/delete', {
          method: 'POST',
          body: JSON.stringify({ model }),
        });
        $('#action-result').textContent = data.message || 'Модель удалена';
      }
      await refreshModels();
    } catch (e) {
      $('#action-result').textContent = e.message;
    }
  };

  $('#save-config-btn').onclick = async () => {
    try {
      await api('/api/config', { method: 'POST', body: JSON.stringify(currentForm()) });
      $('#action-result').textContent = 'Настройки сохранены';
      await refreshState();
    } catch (e) {
      $('#action-result').textContent = e.message;
    }
  };

  $('#toggle-btn').onclick = async () => {
    if (actionInFlight || !currentState) return;
    actionInFlight = true;
    updateToggleButton(currentState);
    try {
      if (currentState.session.running) {
        pendingAction = 'stop';
        const data = await api('/api/stop', { method: 'POST' });
        $('#action-result').textContent = data.message || 'Остановлено';
      } else {
        pendingAction = 'start';
        const data = await api('/api/start', { method: 'POST', body: JSON.stringify(currentForm()) });
        $('#action-result').textContent = data.message || 'Запущено';
      }
      await refreshState();
      if (currentState && isPendingActionComplete(currentState)) {
        finalizePendingAction(currentState);
      } else {
        scheduleActionWatchdog();
      }
    } catch (e) {
      actionInFlight = false;
      pendingAction = null;
      clearActionWatchdog();
      updateToggleButton(currentState);
      $('#action-result').textContent = e.message;
    }
  };

  const modelsModal = $('#models-modal');
  const modelsBtn = $('#models-btn');
  const modelsModalClose = $('#models-modal-close');

  modelsBtn.onclick = () => {
    modelsModal.classList.add('show');
    refreshModels();
  };
  modelsModalClose.onclick = () => {
    modelsModal.classList.remove('show');
  };
  modelsModal.onclick = (e) => {
    if (e.target === modelsModal) modelsModal.classList.remove('show');
  };
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modelsModal.classList.contains('show')) {
      modelsModal.classList.remove('show');
    }
  });

  $('#logout-btn').onclick = async () => {
    await api('/api/logout', { method: 'POST' });
    location.href = '/login';
  };

  document.querySelectorAll('select[id^="range-"]').forEach(el => {
    el.addEventListener('change', () => {
      if (currentState) updateUI(currentState);
    });
  });

  await refreshState();
  connectStateSocket();
}

boot();
