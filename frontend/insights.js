const $ = (id) => document.getElementById(id);
const api = async (url, options = {}) => {
  const response = await fetch(url, {headers: {'Content-Type':'application/json', ...(options.headers || {})}, ...options});
  const text = await response.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = {detail: text}; }
  if (!response.ok) throw new Error(data.detail || `${response.status} ${response.statusText}`);
  return data;
};
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const toast = (message, error = false) => {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  setTimeout(() => { el.className = 'toast'; }, 3500);
};
const fmtTime = (row) => row.updated_at || row.finished_at || row.created_at || row.started_at || '';
const rowMessage = (row) => row.error_message || row.error || row.message || '';

async function loadBuild() {
  try {
    const build = await api('/api/build');
    $('buildBadge').textContent = `BUILD ${(build.build_sha || 'unknown').slice(0,12)} · ${build.release || ''}`;
  } catch { $('buildBadge').textContent = 'BUILD unknown'; }
}

function taskCard(row, failed = false) {
  const id = row.id ?? '?';
  const topic = row.topic_id != null ? `Topic ${row.topic_id}` : '无 Topic';
  const message = rowMessage(row);
  return `<div class="task${failed ? ' failed' : ''}">
    <div class="task-top"><div class="task-title">Run #${esc(id)} · ${esc(topic)}</div><span class="status">${esc(row.status)}</span></div>
    <div class="task-meta">${esc(fmtTime(row))}${message ? `<br>${esc(message)}` : ''}</div>
    ${failed ? `<div class="inline-actions" style="margin-top:10px"><button class="button" onclick="resumeRun(${Number(id)})">从断点重新入队</button></div>` : ''}
  </div>`;
}

async function loadTasks() {
  const data = await api('/api/intelligence/tasks?limit=120');
  const c = data.counts || {};
  $('taskStats').innerHTML = [
    ['活跃', c.active || 0], ['失败', c.failed || 0], ['完成', c.completed || 0], ['其他', c.other || 0]
  ].map(([name, value]) => `<div class="stat"><strong>${value}</strong><span>${name}</span></div>`).join('');
  $('activeTasks').innerHTML = (data.active || []).length ? data.active.map((r) => taskCard(r, false)).join('') : '<div class="empty">当前没有活跃任务。</div>';
  $('failedTasks').innerHTML = (data.failed || []).length ? data.failed.slice(0,30).map((r) => taskCard(r, true)).join('') : '<div class="empty">没有失败任务。</div>';
}

window.resumeRun = async (runId) => {
  if (!confirm(`确认将失败的 Run #${runId} 从原断点重新入队？`)) return;
  try {
    const result = await api(`/api/intelligence/runs/${runId}/resume`, {method:'POST'});
    toast(`Run #${runId} 已重新入队：${result.enqueue_result || 'queued'}`);
    await loadTasks();
  } catch (err) { toast(err.message, true); }
};

async function loadDaily() {
  const day = $('summaryDate').value;
  if (!day) return;
  $('dailySummary').textContent = '读取中…';
  try {
    const data = await api(`/api/intelligence/daily/${encodeURIComponent(day)}`);
    $('summaryMeta').textContent = data.exists ? `模型：${data.model || '-'} · 生成：${data.generated_at || '-'} · ${data.elapsed_seconds || '-'} 秒` : `尚未生成。当前可用数据：${JSON.stringify(data.snapshot_counts || {})}`;
    $('dailySummary').textContent = data.summary || '当天尚未生成总结。点击“生成 / 重新生成”。';
  } catch (err) {
    $('dailySummary').textContent = `读取失败：${err.message}`;
    toast(err.message, true);
  }
}

async function generateDaily() {
  const day = $('summaryDate').value;
  if (!day) return;
  const btn = $('generateSummary');
  btn.disabled = true;
  btn.textContent = '本地 Qwen 正在生成…';
  $('dailySummary').textContent = '正在根据当天入库证据生成总结。27B 模型可能需要一些时间，请以此页面最终返回为准。';
  try {
    const data = await api(`/api/intelligence/daily/${encodeURIComponent(day)}/generate`, {method:'POST'});
    $('summaryMeta').textContent = `模型：${data.model || '-'} · 生成：${data.generated_at || '-'} · ${data.elapsed_seconds || '-'} 秒 · 数据：${JSON.stringify(data.snapshot_counts || {})}`;
    $('dailySummary').textContent = data.summary || '没有可总结的数据。';
    toast('当日总结已生成并持久化到 /data/daily_summaries');
  } catch (err) {
    $('dailySummary').textContent = `生成失败：${err.message}`;
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '生成 / 重新生成';
  }
}

async function askKnowledge() {
  const question = $('question').value.trim();
  if (question.length < 2) return toast('请输入问题。', true);
  const btn = $('askButton');
  btn.disabled = true;
  btn.textContent = '本地 Qwen 分析中…';
  $('answer').textContent = '正在从本地 PostgreSQL 知识库检索证据并生成回答…';
  $('evidence').innerHTML = '';
  try {
    const data = await api('/api/intelligence/ask', {method:'POST', body: JSON.stringify({question, max_evidence:60})});
    $('askMeta').textContent = `模型：${data.model || '-'} · ${data.elapsed_seconds || '-'} 秒 · 证据 ${data.evidence?.length || 0} 条`;
    $('answer').textContent = data.answer || '没有回答。';
    $('evidence').innerHTML = (data.evidence || []).map((item) => `<div class="evidence-item"><strong>[${esc(item.ref)}] ${esc(item.table)} · score ${esc(item.score)}</strong><br><code>${esc(JSON.stringify(item.row, null, 2))}</code></div>`).join('') || '<div class="empty">没有证据。</div>';
  } catch (err) {
    $('answer').textContent = `问答失败：${err.message}`;
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = '询问知识库';
  }
}

$('refreshTasks').addEventListener('click', () => loadTasks().catch((e) => toast(e.message, true)));
$('loadSummary').addEventListener('click', loadDaily);
$('generateSummary').addEventListener('click', generateDaily);
$('askButton').addEventListener('click', askKnowledge);
$('question').addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') askKnowledge();
});

const now = new Date();
$('summaryDate').value = `${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;
loadBuild();
loadTasks().catch((e) => toast(e.message, true));
loadDaily();
setInterval(() => loadTasks().catch(() => {}), 15000);
