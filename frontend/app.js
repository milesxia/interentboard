const state = { apiKey: localStorage.getItem('internetboard_api_key') || '', dashboard: null, system: null };
const $ = (s) => document.querySelector(s);
const esc = (v='') => String(v).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));

function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3000);
}

function ensureKey() {
  if (state.apiKey) return true;
  const value = prompt('请输入 .env 中的 INTERNETBOARD_API_KEY');
  if (!value) return false;
  state.apiKey = value.trim(); localStorage.setItem('internetboard_api_key', state.apiKey); return true;
}

async function api(path, options={}) {
  if (!ensureKey()) throw new Error('未设置 API Key');
  const headers = { 'X-API-Key': state.apiKey, ...(options.headers || {}) };
  if (options.body && !(options.body instanceof FormData)) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {...options, headers});
  if (res.status === 401) {
    localStorage.removeItem('internetboard_api_key'); state.apiKey = '';
    throw new Error('API Key 无效，请重新设置');
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

const statusClass = s => s === 'COMPLETED' ? 'good' : s === 'FAILED' ? 'bad' : ['WAITING'].includes(s) ? 'muted' : 'warn';
const formatDate = v => v ? new Date(v).toLocaleString() : '-';

function renderStats() {
  const c = state.system?.counts || {};
  $('#stats').innerHTML = [
    ['专题', c.topics || 0], ['运行中', c.active_runs || 0], ['证据', c.sources || 0], ['知识 Claim', c.claims || 0], ['未解决冲突', c.open_conflicts || 0]
  ].map(([label,value]) => `<div class="stat"><strong>${value}</strong><span>${label}</span></div>`).join('');

  const m = state.system?.model || {};
  const b = $('#modelBadge');
  if (m.ok && m.model_ready) { b.className='badge good'; b.textContent=`${m.model} 已就绪`; }
  else if (m.ok) { b.className='badge warn'; b.textContent='Ollama 在线 / 模型拉取中'; }
  else { b.className='badge bad'; b.textContent='Ollama 未就绪'; }
}

function renderTopics() {
  const topics = state.dashboard?.topics || [];
  $('#topics').innerHTML = topics.length ? topics.map(t => `
    <div class="item">
      <div class="item-head">
        <div><div class="item-title">${esc(t.name)}</div><div class="meta">${esc(t.query)} · 优先级 ${t.priority} · ${t.enabled?'启用':'停用'}</div></div>
        <div class="actions"><button onclick="runTopic(${t.id})">立即刷新</button></div>
      </div>
    </div>`).join('') : '<div class="meta">还没有专题。先新增一个研究专题。</div>';
  const options = topics.map(t => `<option value="${t.id}">${esc(t.name)}</option>`).join('');
  $('#noteTopic').innerHTML = options; $('#watchTopic').innerHTML = options;
}

function renderRuns() {
  const runs = state.dashboard?.runs || [];
  $('#runs').innerHTML = runs.length ? runs.map(r => `
    <div class="item">
      <div class="item-head"><div class="item-title">Run #${r.id} · Topic ${r.topic_id}</div><span class="badge ${statusClass(r.status)}">${r.status}</span></div>
      <div class="meta">${esc(r.message || '')}</div>
      <div class="progress"><i style="width:${r.progress}%"></i></div>
      ${r.summary ? `<pre class="summary">${esc(r.summary)}</pre>` : ''}
      ${r.trend ? `<div class="insight"><b>Trend</b><span>${esc(r.trend)}</span></div>` : ''}
      ${r.prediction ? `<div class="insight"><b>Prediction</b><span>${esc(r.prediction)}</span></div>` : ''}
      ${r.confidence ? `<div class="meta">Synthesis confidence ${(r.confidence*100).toFixed(0)}%</div>` : ''}
      ${r.error ? `<div class="meta" style="color:var(--bad)">${esc(r.error.slice(0,500))}</div>` : ''}
    </div>`).join('') : '<div class="meta">暂无任务。</div>';
}

function renderClaims() {
  const claims = state.dashboard?.claims || [];
  $('#claims').innerHTML = claims.length ? claims.map(c => `
    <div class="claim">
      <p>${esc(c.claim_text)}</p>
      <div class="rank">${c.origin==='manual'?'人工':'AI'} · ${c.claim_type} · P${c.priority} · 重要度 ${c.importance} · 置信度 ${(c.confidence*100).toFixed(0)}% · 证据出现 ${c.occurrence_count} 次</div>
      <div class="actions">${c.priority < 100 ? `<button class="ghost" onclick="confirmClaim(${c.id})">\u4eba\u5de5\u786e\u8ba4</button>` : ''}<button class="ghost" onclick="editClaim(${c.id})">\u4eba\u5de5\u4fee\u6539</button></div>
    </div>`).join('') : '<div class="meta">暂无知识。运行专题后会在这里出现。</div>';
}

function renderConflicts() {
  const items = state.dashboard?.conflicts || [];
  $('#conflicts').innerHTML = items.length ? items.map(c => `
    <div class="item">
      <div class="item-title">${esc(c.reason)}</div>
      <div class="meta">A：${esc(c.claim_a_text)}</div>
      <div class="meta">B：${esc(c.claim_b_text)}</div>
      <div class="meta">置信度 ${(c.confidence*100).toFixed(0)}% · ${c.status}</div>
      <div class="actions">${c.claim_a_id ? `<button class="ghost" onclick="resolveConflict(${c.id},${c.claim_a_id},'A')">\u91c7\u7528 A</button>` : ''}${c.claim_b_id ? `<button class="ghost" onclick="resolveConflict(${c.id},${c.claim_b_id},'B')">\u91c7\u7528 B</button>` : ''}<button class="ghost" onclick="resolveConflict(${c.id},null,'manual')">\u6807\u8bb0\u5df2\u5904\u7406</button></div>
    </div>`).join('') : '<div class="meta">当前没有未解决冲突。</div>';
}

function renderSources(items) {
  $('#sources').innerHTML = items.length ? items.slice(0,30).map(s => `
    <div class="item">
      <div class="item-title">${esc(s.title || s.url)}</div>
      <div class="meta">${esc(s.mime_type)} · ${formatDate(s.retrieved_at)} · seen ${s.seen_count}</div>
      ${s.url.startsWith('http') ? `<div class="meta"><a href="${esc(s.url)}" target="_blank" rel="noreferrer">打开来源</a></div>` : '<div class="meta">人工输入证据</div>'}
    </div>`).join('') : '<div class="meta">暂无证据。</div>';
}

function renderWatches(items) {
  $('#watches').innerHTML = items.length ? items.map(w => `
    <div class="item"><div class="item-title">${esc(w.url)}</div><div class="meta">Topic ${w.topic_id} · 上次检查 ${formatDate(w.last_checked_at)} · 上次变化 ${formatDate(w.last_changed_at)}</div></div>`).join('') : '<div class="meta">暂无网页监测。</div>';
}

function renderGraph(data) {
  const svg = $('#graph'); const nodes = data.nodes || [], edges = data.edges || [];
  if (!nodes.length) { svg.innerHTML='<text x="40" y="60" class="graph-node-text">暂无关系数据</text>'; return; }
  const cx=500, cy=260, rx=390, ry=190;
  const pos = new Map(nodes.map((n,i) => [n.id, {x: cx + rx*Math.cos((Math.PI*2*i/nodes.length)-Math.PI/2), y: cy + ry*Math.sin((Math.PI*2*i/nodes.length)-Math.PI/2)}]));
  const edgeSvg = edges.map(e => {
    const a=pos.get(e.source), b=pos.get(e.target); if(!a||!b) return '';
    const mx=(a.x+b.x)/2, my=(a.y+b.y)/2;
    return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="graph-edge"/><text x="${mx}" y="${my}" class="graph-label">${esc(e.label)}</text>`;
  }).join('');
  const nodeSvg = nodes.map(n => { const p=pos.get(n.id); return `<circle cx="${p.x}" cy="${p.y}" r="24" class="graph-node"/><text x="${p.x}" y="${p.y+42}" text-anchor="middle" class="graph-node-text">${esc(n.name.slice(0,18))}</text>`; }).join('');
  svg.innerHTML=edgeSvg+nodeSvg;
}

async function load() {
  try {
    const [system,dashboard,sources,watches,graph] = await Promise.all([
      api('/api/system/status'), api('/api/dashboard'), api('/api/sources?limit=40'), api('/api/watches'), api('/api/graph?limit=120')
    ]);
    state.system=system; state.dashboard=dashboard;
    renderStats(); renderTopics(); renderRuns(); renderClaims(); renderConflicts(); renderSources(sources); renderWatches(watches); renderGraph(graph);
  } catch (e) { toast(e.message); }
}

async function runTopic(id) {
  try { await api(`/api/topics/${id}/run`, {method:'POST'}); toast('任务已进入队列'); await load(); } catch(e) { toast(e.message); }
}
window.runTopic = runTopic;

async function confirmClaim(id) {
  const c=(state.dashboard?.claims||[]).find(x=>x.id===id); if(!c) return;
  try {
    await api('/api/claims/manual',{method:'POST',body:JSON.stringify({topic_id:c.topic_id,claim_text:c.claim_text,category:c.category,event_time:c.event_time,importance:c.importance,confidence:Math.max(c.confidence,0.95)})});
    toast('\u5df2\u4eba\u5de5\u786e\u8ba4\uff0c\u4f18\u5148\u7ea7\u5347\u4e3a 100'); await load();
  } catch(e) { toast(e.message); }
}
window.confirmClaim=confirmClaim;

async function editClaim(id) {
  const c=(state.dashboard?.claims||[]).find(x=>x.id===id); if(!c) return;
  const text=prompt('\u4fee\u6539 Claim',c.claim_text); if(!text || text.trim()===c.claim_text) return;
  try { await api(`/api/claims/${id}`,{method:'PATCH',body:JSON.stringify({claim_text:text.trim()})}); toast('\u5df2\u4eba\u5de5\u4fee\u6539\uff0c\u4f18\u5148\u7ea7\u81f3\u5c11 80'); await load(); } catch(e) { toast(e.message); }
}
window.editClaim=editClaim;

async function resolveConflict(id,winner,label) {
  try {
    const payload={resolution:winner?`Manual resolution: selected claim ${label}`:'Manual resolution without selecting a winning claim',winning_claim_id:winner};
    await api(`/api/conflicts/${id}/resolve`,{method:'POST',body:JSON.stringify(payload)});
    toast('\u51b2\u7a81\u5df2\u4eba\u5de5\u5904\u7406'); await load();
  } catch(e) { toast(e.message); }
}
window.resolveConflict=resolveConflict;

$('#apiKeyBtn').addEventListener('click', () => {
  const value = prompt('输入新的 INTERNETBOARD_API_KEY', state.apiKey || '');
  if (value) { state.apiKey=value.trim(); localStorage.setItem('internetboard_api_key', state.apiKey); load(); }
});
$('#refreshBtn').addEventListener('click', load);

$('#topicForm').addEventListener('submit', async e => {
  e.preventDefault(); const f=new FormData(e.currentTarget);
  try { await api('/api/topics',{method:'POST',body:JSON.stringify({name:f.get('name'),query:f.get('query'),description:'',enabled:true,priority:50})}); e.currentTarget.reset(); toast('专题已创建'); await load(); } catch(err){ toast(err.message); }
});

$('#noteForm').addEventListener('submit', async e => {
  e.preventDefault(); const f=new FormData(e.currentTarget);
  try { await api('/api/manual-notes',{method:'POST',body:JSON.stringify({topic_id:Number(f.get('topic_id')),title:f.get('title')||'',content:f.get('content')})}); e.currentTarget.reset(); toast('人工输入已保存；下次研究会按优先级100整理'); await load(); } catch(err){ toast(err.message); }
});

$('#watchForm').addEventListener('submit', async e => {
  e.preventDefault(); const f=new FormData(e.currentTarget);
  try { await api('/api/watches',{method:'POST',body:JSON.stringify({topic_id:Number(f.get('topic_id')),url:f.get('url'),enabled:true})}); e.currentTarget.reset(); toast('网页监测已添加'); await load(); } catch(err){ toast(err.message); }
});

load();
setInterval(() => {
  const active = (state.dashboard?.runs || []).some(r => !['COMPLETED','FAILED'].includes(r.status));
  if (active) load();
}, 10000);
