const $ = (sel) => document.querySelector(sel);
let pollTimer = null;

function fmtBytes(bytes) {
  if (bytes == null) return '—';
  const units = ['B','KB','MB','GB'];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 ? 0 : 1)} ${units[i]}`;
}
function fmtNum(v, digits=1) { return v == null ? '—' : Number(v).toFixed(digits); }
function fmtPct(v) { return v == null ? '—' : `${Number(v).toFixed(v >= 10 ? 0 : 1)} %`; }
function fmtChartNum(v) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  const n = Number(v);
  const abs = Math.abs(n);
  let digits = 0;
  if (abs < 1) digits = 2;
  else if (abs < 10) digits = 2;
  else if (abs < 100) digits = 1;
  const text = n.toFixed(digits);
  return text.replace(/\.0+$/, '').replace(/(\.\d*[1-9])0+$/, '$1');
}
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function statusClass(v) {
  if (v == null) return '';
  const x = Number(v);
  if (x < 0.9) return 'good';
  if (x <= 1.1) return 'warn';
  return 'bad';
}
async function api(url, options={}) {
  const res = await fetch(url, {headers:{'Content-Type':'application/json'}, ...options});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.message || 'Request failed');
  return data;
}
function kv(el, items) {
  el.innerHTML = items.map(([k, v, cls='']) => `<div class="kv"><div class="k">${esc(k)}</div><div class="v ${cls}">${esc(v)}</div></div>`).join('');
}
function timeFilter(items, seconds) {
  const now = Date.now() / 1000;
  return items.filter(x => x.ts >= now - Number(seconds));
}
function aggregate(items, rangeSec, bucketSec, valueFn) {
  const now = Date.now() / 1000;
  const start = now - rangeSec;
  const buckets = [];
  for (let t = start; t <= now; t += bucketSec) buckets.push({t, mic:0, remote:0});
  for (const item of items) {
    if (item.ts < start) continue;
    const idx = Math.min(buckets.length - 1, Math.max(0, Math.floor((item.ts - start) / bucketSec)));
    const val = valueFn(item);
    buckets[idx][item.role] += val;
  }
  return buckets;
}
function getChartTicks(minY, maxY, count=4) {
  if (minY === maxY) return [minY];
  return Array.from({length: count}, (_, i) => maxY - ((maxY - minY) * i / (count - 1)));
}
function renderLineChart(target, seriesDefs, opts={}) {
  const w = target.clientWidth || 500, h = 220;
  const padL = 56, padR = 18, padT = 18, padB = 18;
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
    svg += `<line x1="${padL}" y1="${y}" x2="${w-padR}" y2="${y}" stroke="#263250" stroke-width="1"/>`;
    svg += `<text x="${padL - 8}" y="${y + 4}" fill="#97a3c6" font-size="10" text-anchor="end">${esc(formatValue(tick))}</text>`;
  }
  if (opts.forceMax != null && opts.forceMax >= 1 && minY <= 1 && maxY >= 1) {
    const y = h - padB - ((1 - minY) / ySpan) * innerH;
    svg += `<line x1="${padL}" y1="${y}" x2="${w-padR}" y2="${y}" stroke="#f1c40f" stroke-width="1" stroke-dasharray="4 4"/>`;
  }
  for (const s of seriesDefs) {
    const points = s.values.map(v => {
      const x = padL + (v.x / xMax) * innerW;
      const y = h - padB - ((v.y - minY) / ySpan) * innerH;
      return {x, y, value: v.y};
    });
    const pts = points.map(p => `${p.x},${p.y}`).join(' ');
    svg += `<polyline fill="none" stroke="${s.color}" stroke-width="2.5" points="${pts}"/>`;
    for (const p of points) {
      const title = `${s.label || 'series'}: ${formatValue(p.value)}`;
      svg += `<circle cx="${p.x}" cy="${p.y}" r="3.5" fill="${s.color}" class="chart-point">`
        + `<title>${esc(title)}</title></circle>`;
    }
  }
  svg += `</svg>`;
  target.innerHTML = svg;
}
function renderWordsChart(state, rangeSec, el) {
  const bucket = rangeSec <= 3600 ? 60 : 1800;
  const rows = aggregate(state.history, rangeSec, bucket, x => x.words);
  const series = [
    {label:'mic', color:'#7c9cff', values: rows.map((r,i)=>({x:i,y:r.mic}))},
    {label:'remote', color:'#2ecc71', values: rows.map((r,i)=>({x:i,y:r.remote}))},
  ];
  renderLineChart(el, series, {forceMin:0});
}
function renderRTFChart(state, rangeSec, el) {
  const items = timeFilter(state.processing_history, rangeSec);
  const normalize = (role) => items.filter(x => x.role === role).map((x,i)=>({x:i, y:x.rtf || 0}));
  renderLineChart(el, [
    {label:'mic', color:'#7c9cff', values: normalize('mic')},
    {label:'remote', color:'#2ecc71', values: normalize('remote')},
  ], {forceMin:0, forceMax:1.2});
}
function renderLagChart(state, rangeSec, el) {
  const items = timeFilter(state.processing_history, rangeSec);
  const normalize = (role) => items.filter(x => x.role === role).map((x,i)=>({x:i, y:x.lag || 0}));
  renderLineChart(el, [
    {label:'mic', color:'#7c9cff', values: normalize('mic')},
    {label:'remote', color:'#2ecc71', values: normalize('remote')},
  ], {forceMin:0});
}
function renderCumChart(state, rangeSec, el) {
  const bucket = rangeSec <= 3600 ? 60 : 1800;
  const rows = aggregate(state.history, rangeSec, bucket, x => x.words);
  let mic = 0, remote = 0;
  const micVals = [], remoteVals = [];
  rows.forEach((r, i) => { mic += r.mic; remote += r.remote; micVals.push({x:i,y:mic}); remoteVals.push({x:i,y:remote}); });
  renderLineChart(el, [
    {label:'mic', color:'#7c9cff', values: micVals},
    {label:'remote', color:'#2ecc71', values: remoteVals},
  ], {forceMin:0});
}
function renderLangs(target, data) {
  const entries = Object.entries(data || {});
  if (!entries.length) { target.innerHTML = '<div class="muted">Пока нет данных</div>'; return; }
  target.innerHTML = entries.map(([k,v]) => `<div class="lang-tag"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('');
}
function renderLogs(target, logs) {
  if (!logs.length) { target.innerHTML = '<div class="muted">Пока нет фраз</div>'; return; }
  target.innerHTML = logs.slice().reverse().map(item => `
    <div class="log-item">
      <div class="log-head"><span>${esc(item.at)}</span><span>${esc(item.role)}</span><span>${esc(item.language)}</span><span>${item.words} слов</span></div>
      <div class="log-text">${esc(item.text)}</div>
    </div>`).join('');
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
    api('/api/config'), api('/api/devices'), api('/api/models/status'),
  ]);
  const cfg = cfgRes.config, devices = devRes.devices;
  const fill = (sel, selected) => {
    const options = ['<option value="">— не использовать —</option>'].concat(devices.map(d => `<option value="${d.id}">${d.id}: ${esc(d.name)} (${d.default_samplerate} Hz)</option>`));
    sel.innerHTML = options.join('');
    sel.value = selected == null ? '' : String(selected);
  };
  fill($('#mic-device'), cfg.mic_device);
  fill($('#remote-device'), cfg.remote_device);
  setupModelSelector(modRes.groups || [], cfg.model || '');
  renderModels(modRes.groups || []);
  $('#threads').value = cfg.threads || 6;
  $('#quantization').value = cfg.quantization || 'none';
  $('#out-dir').value = cfg.out_dir || './transcripts';
  $('#full-audio-enabled').value = String(cfg.full_audio_enabled !== false);
  $('#full-audio-retention-days').value = cfg.full_audio_retention_days || 1;
  $('#full-audio-dir').value = cfg.full_audio_dir || './audio_archive';
  $('#model-select').dispatchEvent(new Event('change'));
}
function currentForm() {
  const parseId = (v) => v === '' ? null : Number(v);
  return {
    mic_device: parseId($('#mic-device').value),
    remote_device: parseId($('#remote-device').value),
    model: $('#model').value.trim(),
    quantization: $('#quantization').value,
    threads: Number($('#threads').value || 6),
    out_dir: $('#out-dir').value.trim(),
    full_audio_enabled: $('#full-audio-enabled').value === 'true',
    full_audio_retention_days: Math.max(1, Number($('#full-audio-retention-days').value || 1)),
    full_audio_dir: $('#full-audio-dir').value.trim() || './audio_archive',
  };
}
async function refreshState() {
  try {
    await refreshModels();
    const {state} = await api('/api/state');
    const session = state.session, system = state.system, mic = state.sources.mic, remote = state.sources.remote;
    $('#server-status').textContent = session.loading ? 'loading' : (session.running ? 'running' : 'stopped');
    kv($('#session-metrics'), [
      ['Запущено', session.running ? 'да' : 'нет'],
      ['Загрузка модели', session.loading ? 'да' : 'нет'],
      ['Модель загружена', session.model_loaded ? 'да' : 'нет'],
      ['Архив аудио', state.archive?.enabled ? 'да' : 'нет'],
      ['Хранить суток', state.archive?.retention_days ?? '—'],
      ['Архив mic.wav', fmtBytes(state.archive?.files?.mic)],
      ['Архив remote.wav', fmtBytes(state.archive?.files?.remote)],
      ['Фраз всего', session.total_utterances],
      ['Слов всего', session.total_words],
      ['Слов/час mic', session.words_per_hour.mic],
      ['Слов/час remote', session.words_per_hour.remote],
      ['Последняя запись', session.last_write_at],
      ['mic.jsonl', fmtBytes(session.log_sizes.mic)],
      ['remote.jsonl', fmtBytes(session.log_sizes.remote)],
      ['combined.jsonl', fmtBytes(session.log_sizes.combined)],
      ['Ошибка', session.server_error || '—', session.server_error ? 'bad' : '']
    ]);
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
      ['Буфер, мс', fmtNum(src.current_buffer_ms)],
      ['Последний аудиокусок, с', fmtNum(src.last_audio_sec, 2)],
      ['Последняя обработка, с', fmtNum(src.last_processing_sec, 2)],
      ['RTF', fmtNum(src.last_rtf, 2), statusClass(src.last_rtf)],
      ['Оценка задержки, с', fmtNum(src.lag_estimate_sec, 2), statusClass(src.lag_estimate_sec > 1 ? 2 : 0)],
      ['Потерянные чанки', src.dropped_chunks],
      ['Фраз', src.utterances],
      ['Слов', src.words],
      ['Последний язык', src.last_language || '—'],
      ['Последняя запись', src.last_commit_at || '—'],
      ['Ошибка', src.last_error || '—', src.last_error ? 'bad' : ''],
    ];
    kv($('#mic-metrics'), sourceItems(mic));
    kv($('#remote-metrics'), sourceItems(remote));
    renderLangs($('#langs-mic'), state.languages.mic);
    renderLangs($('#langs-remote'), state.languages.remote);
    renderLogs($('#logs'), state.logs || []);
    renderWordsChart(state, Number($('#range-words').value), $('#chart-words'));
    renderRTFChart(state, Number($('#range-rtf').value), $('#chart-rtf'));
    renderLagChart(state, Number($('#range-lag').value), $('#chart-lag'));
    renderCumChart(state, Number($('#range-cum').value), $('#chart-cumulative'));
  } catch (e) {
    $('#server-status').textContent = 'error';
  }
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
          method:'POST',
          body:JSON.stringify({model, quantization: $('#quantization').value}),
        });
        $('#action-result').textContent = data.message || 'Загрузка модели запущена';
      } else if (action === 'delete') {
        const data = await api('/api/models/delete', {
          method:'POST',
          body:JSON.stringify({model}),
        });
        $('#action-result').textContent = data.message || 'Модель удалена';
      }
      await refreshModels();
    } catch (e) {
      $('#action-result').textContent = e.message;
    }
  };
  $('#save-config-btn').onclick = async () => {
    try { await api('/api/config', {method:'POST', body:JSON.stringify(currentForm())}); $('#action-result').textContent = 'Настройки сохранены'; }
    catch (e) { $('#action-result').textContent = e.message; }
  };
  $('#start-btn').onclick = async () => {
    try { const data = await api('/api/start', {method:'POST', body:JSON.stringify(currentForm())}); $('#action-result').textContent = data.message || 'Запущено'; }
    catch (e) { $('#action-result').textContent = e.message; }
  };
  $('#stop-btn').onclick = async () => {
    try { const data = await api('/api/stop', {method:'POST'}); $('#action-result').textContent = data.message || 'Остановлено'; }
    catch (e) { $('#action-result').textContent = e.message; }
  };
  $('#logout-btn').onclick = async () => { await api('/api/logout', {method:'POST'}); location.href='/login'; };
  document.querySelectorAll('select[id^="range-"]').forEach(el => el.addEventListener('change', refreshState));
  await refreshState();
  pollTimer = setInterval(refreshState, 1200);
}
boot();
