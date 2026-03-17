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
function renderLineChart(target, seriesDefs, opts={}) {
  const w = target.clientWidth || 500, h = 220, pad = 26;
  const allVals = seriesDefs.flatMap(s => s.values.map(v => v.y));
  const maxY = Math.max(opts.minMaxY || 0, ...allVals, opts.forceMax ?? 0);
  const minY = opts.forceMin ?? Math.min(0, ...allVals);
  const ySpan = Math.max(1e-6, maxY - minY);
  const xMax = Math.max(1, ...seriesDefs.flatMap(s => s.values.map(v => v.x)));
  let svg = `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">`;
  svg += `<rect x="0" y="0" width="${w}" height="${h}" fill="transparent"/>`;
  for (let i=0;i<4;i++) {
    const y = pad + ((h - 2*pad) * i / 3);
    svg += `<line x1="${pad}" y1="${y}" x2="${w-pad}" y2="${y}" stroke="#263250" stroke-width="1"/>`;
  }
  if (opts.forceMax != null && opts.forceMax >= 1 && minY <= 1 && maxY >= 1) {
    const y = h - pad - ((1 - minY) / ySpan) * (h - 2*pad);
    svg += `<line x1="${pad}" y1="${y}" x2="${w-pad}" y2="${y}" stroke="#f1c40f" stroke-width="1" stroke-dasharray="4 4"/>`;
  }
  for (const s of seriesDefs) {
    const pts = s.values.map(v => {
      const x = pad + (v.x / xMax) * (w - 2*pad);
      const y = h - pad - ((v.y - minY) / ySpan) * (h - 2*pad);
      return `${x},${y}`;
    }).join(' ');
    svg += `<polyline fill="none" stroke="${s.color}" stroke-width="2.5" points="${pts}"/>`;
  }
  svg += `</svg>`;
  target.innerHTML = svg;
}
function renderWordsChart(state, rangeSec, el) {
  const bucket = rangeSec <= 3600 ? 60 : 1800;
  const rows = aggregate(state.history, rangeSec, bucket, x => x.words);
  const series = [
    {color:'#7c9cff', values: rows.map((r,i)=>({x:i,y:r.mic}))},
    {color:'#2ecc71', values: rows.map((r,i)=>({x:i,y:r.remote}))},
  ];
  renderLineChart(el, series, {forceMin:0});
}
function renderRTFChart(state, rangeSec, el) {
  const items = timeFilter(state.processing_history, rangeSec);
  const normalize = (role) => items.filter(x => x.role === role).map((x,i)=>({x:i, y:x.rtf || 0}));
  renderLineChart(el, [
    {color:'#7c9cff', values: normalize('mic')},
    {color:'#2ecc71', values: normalize('remote')},
  ], {forceMin:0, forceMax:1.2});
}
function renderLagChart(state, rangeSec, el) {
  const items = timeFilter(state.processing_history, rangeSec);
  const normalize = (role) => items.filter(x => x.role === role).map((x,i)=>({x:i, y:x.lag || 0}));
  renderLineChart(el, [
    {color:'#7c9cff', values: normalize('mic')},
    {color:'#2ecc71', values: normalize('remote')},
  ], {forceMin:0});
}
function renderCumChart(state, rangeSec, el) {
  const bucket = rangeSec <= 3600 ? 60 : 1800;
  const rows = aggregate(state.history, rangeSec, bucket, x => x.words);
  let mic = 0, remote = 0;
  const micVals = [], remoteVals = [];
  rows.forEach((r, i) => { mic += r.mic; remote += r.remote; micVals.push({x:i,y:mic}); remoteVals.push({x:i,y:remote}); });
  renderLineChart(el, [
    {color:'#7c9cff', values: micVals},
    {color:'#2ecc71', values: remoteVals},
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
async function loadConfigAndDevices() {
  const [cfgRes, devRes] = await Promise.all([api('/api/config'), api('/api/devices')]);
  const cfg = cfgRes.config, devices = devRes.devices;
  const fill = (sel, selected) => {
    const options = ['<option value="">— не использовать —</option>'].concat(devices.map(d => `<option value="${d.id}">${d.id}: ${esc(d.name)} (${d.default_samplerate} Hz)</option>`));
    sel.innerHTML = options.join('');
    sel.value = selected == null ? '' : String(selected);
  };
  fill($('#mic-device'), cfg.mic_device);
  fill($('#remote-device'), cfg.remote_device);
  $('#model').value = cfg.model || '';
  $('#threads').value = cfg.threads || 6;
  $('#out-dir').value = cfg.out_dir || './transcripts';
}
function currentForm() {
  const parseId = (v) => v === '' ? null : Number(v);
  return {
    mic_device: parseId($('#mic-device').value),
    remote_device: parseId($('#remote-device').value),
    model: $('#model').value.trim(),
    threads: Number($('#threads').value || 6),
    out_dir: $('#out-dir').value.trim(),
  };
}
async function refreshState() {
  try {
    const {state} = await api('/api/state');
    const session = state.session, system = state.system, mic = state.sources.mic, remote = state.sources.remote;
    $('#server-status').textContent = session.loading ? 'loading' : (session.running ? 'running' : 'stopped');
    kv($('#session-metrics'), [
      ['Запущено', session.running ? 'да' : 'нет'],
      ['Загрузка модели', session.loading ? 'да' : 'нет'],
      ['Модель загружена', session.model_loaded ? 'да' : 'нет'],
      ['Фраз всего', session['фраз_всего']],
      ['Слов всего', session['слов_всего']],
      ['Слов/час mic', session['слов_за_час'].mic],
      ['Слов/час remote', session['слов_за_час'].remote],
      ['Последняя запись', session['последняя_запись']],
      ['mic.jsonl', fmtBytes(session['размер_логов'].mic)],
      ['remote.jsonl', fmtBytes(session['размер_логов'].remote)],
      ['combined.jsonl', fmtBytes(session['размер_логов'].combined)],
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
