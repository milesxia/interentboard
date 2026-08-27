const $=(id)=>document.getElementById(id);
const esc=(v)=>String(v??'').replace(/[&<>"']/g,(c)=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const toast=(msg,bad=false)=>{const t=$('toast');t.textContent=msg;t.className=`toast show${bad?' bad':''}`;setTimeout(()=>t.className='toast',3600)};
async function api(path,options={}){const r=await fetch(path,{cache:'no-store',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});let b={};try{b=await r.json()}catch{}if(!r.ok)throw new Error(b.detail||`${r.status} ${r.statusText}`);return b;}
const statusText=(row)=>String(row?.status||'unknown').toLowerCase();
const rowMessage=(row)=>row?.message||row?.status_message||row?.error_message||row?.error||'';
const taskText=(row)=>row?.label || row?.topic_name || row?.topic || (row?.topic_id!=null?`专题 #${row.topic_id}`:'未标注专题');
const taskId=(row)=>row?.job_id ? `AI-${String(row.job_id).slice(0,8)}` : `#${row?.id??'—'}`;
const fmtDateTime=(value)=>{if(!value)return'—';const d=new Date(value);if(Number.isNaN(d.getTime()))return String(value);return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});};
const fmtClock=(value)=>{if(!value)return'—';const d=new Date(value);if(Number.isNaN(d.getTime()))return'—';return d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});};
const fmtDuration=(seconds)=>{const s=Math.max(0,Number(seconds)||0);if(s<60)return`${Math.round(s)} 秒`;if(s<3600)return`${Math.round(s/60)} 分钟`;const h=Math.floor(s/3600),m=Math.round((s%3600)/60);return m?`${h} 小时 ${m} 分`:`${h} 小时`;};

async function loadBuild(){try{const b=await api('/api/build');$('buildBadge').textContent=`BUILD ${(b.build_sha||'unknown').slice(0,12)} · ${b.release||''}`;}catch{$('buildBadge').textContent='BUILD unknown';}}
function failedCard(row){const id=Number(row.id);return `<div class="failed-task"><div class="failed-top"><strong>Run #${esc(row.id)} · ${esc(taskText(row))}</strong><span class="status failed">FAILED</span></div><div class="failed-meta">${esc(rowMessage(row)||'没有记录错误信息')}<br>自动恢复：${esc(row.auto_recovery_attempts??0)} 次</div><div class="inline-actions" style="margin-top:9px"><button class="button" onclick="resumeRun(${Number.isFinite(id)?id:0})">从断点重新入队</button></div></div>`;}

function renderQueue(data){
  const queue=data.queue||{},current=queue.current||null,queued=queue.queued||[],failed=data.failed||[],est=queue.estimate||{},monitor=queue.monitor||{};
  $('metricQueued').textContent=queued.length;$('metricFailed').textContent=failed.length;$('metricEta').textContent=queue.all_complete_at?fmtClock(queue.all_complete_at):'—';
  $('metricEtaSub').textContent=queue.all_complete_at?`${fmtDateTime(queue.all_complete_at)} · 置信度 ${est.confidence||'low'}`:'当前没有待执行任务';
  $('metricFailedSub').textContent=`自动恢复上限 ${monitor.max_auto_recoveries??2} 次`;$('stallThreshold').textContent=fmtDuration(monitor.stall_timeout_seconds??1200);$('recoveryLimit').textContent=`${monitor.max_auto_recoveries??2} 次`;
  if(queue.serial_violation){$('runtimeWarning').classList.remove('hidden');$('runtimeWarning').textContent=`检测到 ${1+(queue.extra_running?.length||0)} 个重 AI 任务同时运行，请检查旧 Worker 是否仍在运行。`;$('serialBadge').className='badge';$('serialBadge').textContent='串行异常';}else{$('runtimeWarning').classList.add('hidden');$('serialBadge').className='badge good';$('serialBadge').textContent='AI 串行 1×';}
  if(current){const pct=Math.max(0,Math.min(100,Number(current.progress_percent)||0));$('metricCurrent').textContent=taskId(current);$('metricCurrentSub').textContent=`${taskText(current)} · ${statusText(current)}`;$('currentTitle').textContent=`${taskId(current)} · ${taskText(current)}`;$('currentStatus').textContent=statusText(current).toUpperCase();$('currentProgress').style.width=`${pct}%`;$('currentProgressText').textContent=`${pct.toFixed(pct%1?1:0)}%`;$('currentEta').textContent=`预计完成：${fmtDateTime(current.eta_complete_at)}`;const idle=current.no_progress_seconds!=null?` · 无实际进度 ${fmtDuration(current.no_progress_seconds)}`:'';const recoveries=current.kind==='research'?` · 自动恢复 ${current.auto_recovery_attempts??0}/${monitor.max_auto_recoveries??2}`:'';$('currentMeta').textContent=`状态：${statusText(current)} · ETA依据：${current.eta_basis||'history'}${idle}${recoveries}${rowMessage(current)?` · ${rowMessage(current)}`:''}`;}else{$('metricCurrent').textContent='空闲';$('metricCurrentSub').textContent=queued.length?'等待 AI Worker 领取队首任务':'没有任务';$('currentTitle').textContent=queued.length?'等待领取队首任务':'当前没有运行任务';$('currentStatus').textContent=queued.length?'QUEUED':'IDLE';$('currentProgress').style.width='0%';$('currentProgressText').textContent='0%';$('currentEta').textContent='预计完成：—';$('currentMeta').textContent=queued.length?`队首 ${taskId(queued[0])} 即将开始。`:'等待任务进入队列。';}
  $('estimateNote').textContent=est.sample_count?`最近 ${est.sample_count} 个完成 Research Run：中位耗时 ${fmtDuration(est.median_seconds)}，P75 ${fmtDuration(est.p75_seconds)}；ETA 置信度 ${est.confidence}。`:`暂无足够历史耗时样本，Research 暂按 ${fmtDuration(est.median_seconds||3600)} / 个估算；完成后会自动校准。`;
  $('queueBody').innerHTML=queued.length?queued.map((row)=>`<tr><td><span class="queue-pos">${esc(row.queue_position)}</span></td><td>${esc(taskId(row))}</td><td>${esc(taskText(row))}</td><td><span class="status queued">${esc(statusText(row))}</span></td><td class="eta-time">${esc(fmtDateTime(row.eta_start_at))}</td><td class="eta-time">${esc(fmtDateTime(row.eta_complete_at))}</td></tr>`).join(''):'<tr><td colspan="6" class="empty-cell">当前没有排队任务。</td></tr>';
  $('failedSummary').textContent=`${failed.length} 个`;$('failedTasks').innerHTML=failed.length?failed.slice(0,40).map(failedCard).join(''):'<div class="empty-cell">没有失败任务。</div>';
}
async function loadTasks(){renderQueue(await api('/api/intelligence/tasks?limit=240'));}
window.resumeRun=async(runId)=>{if(!runId||!confirm(`确认将 Run #${runId} 从原断点重新入队？`))return;try{const result=await api(`/api/intelligence/runs/${runId}/resume`,{method:'POST'});toast(`Run #${runId} 已重新入队：${result.enqueue_result||'queued'}`);await loadTasks();}catch(err){toast(err.message,true);}};

async function pollJob(jobId,onStatus){for(let i=0;i<260;i++){const job=await api(`/api/intelligence/jobs/${encodeURIComponent(jobId)}`);if(onStatus)onStatus(job);if(job.status==='completed')return job.result||{};if(job.status==='failed')throw new Error(job.error||'AI任务失败');await new Promise(r=>setTimeout(r,5000));}throw new Error('AI任务等待超时，请在运行看板查看状态');}
async function loadDaily(){const day=$('summaryDate').value;if(!day)return;$('dailySummary').textContent='读取中…';try{const data=await api(`/api/intelligence/daily/${encodeURIComponent(day)}`);$('summaryMeta').textContent=data.exists?`模型：${data.model||'-'} · 生成：${data.generated_at||'-'} · ${data.elapsed_seconds||'-'} 秒`:`尚未生成。当前可用数据：${JSON.stringify(data.snapshot_counts||{})}`;$('dailySummary').textContent=data.summary||'当天尚未生成总结。点击“生成 / 重新生成”。';}catch(err){$('dailySummary').textContent=`读取失败：${err.message}`;toast(err.message,true);}}
async function generateDaily(){const day=$('summaryDate').value;if(!day)return;const btn=$('generateSummary');btn.disabled=true;btn.textContent='已加入 AI 队列…';$('dailySummary').textContent='任务已进入统一 AI 串行队列。';try{const queued=await api(`/api/intelligence/daily/${encodeURIComponent(day)}/generate`,{method:'POST'});const data=await pollJob(queued.job_id,(job)=>{btn.textContent=job.status==='running'?'本地 Qwen 正在生成…':'排队等待 Qwen…';});$('summaryMeta').textContent=`模型：${data.model||'-'} · 生成：${data.generated_at||'-'} · ${data.elapsed_seconds||'-'} 秒 · 数据：${JSON.stringify(data.snapshot_counts||{})}`;$('dailySummary').textContent=data.summary||'没有可总结的数据。';toast('当日总结已生成');await loadTasks();}catch(err){$('dailySummary').textContent=`生成失败：${err.message}`;toast(err.message,true);}finally{btn.disabled=false;btn.textContent='生成 / 重新生成';}}
async function askKnowledge(){const question=$('question').value.trim();if(question.length<2)return toast('请输入问题。',true);const btn=$('askButton');btn.disabled=true;btn.textContent='已加入 AI 队列…';$('answer').textContent='正在等待统一 AI 串行队列…';$('evidence').innerHTML='';try{const queued=await api('/api/intelligence/ask',{method:'POST',body:JSON.stringify({question,max_evidence:60})});const data=await pollJob(queued.job_id,(job)=>{$('answer').textContent=job.status==='running'?'本地 Qwen 正在检索和生成回答…':'已排队，等待前面的 AI 任务完成…';btn.textContent=job.status==='running'?'本地 Qwen 分析中…':'排队等待 Qwen…';});$('askMeta').textContent=`模型：${data.model||'-'} · ${data.elapsed_seconds||'-'} 秒 · 证据 ${data.evidence?.length||0} 条`;$('answer').textContent=data.answer||'没有回答。';$('evidence').innerHTML=(data.evidence||[]).map((item)=>`<div class="evidence-item"><strong>[${esc(item.ref)}] ${esc(item.table)} · score ${esc(item.score)}</strong><br><code>${esc(JSON.stringify(item.row,null,2))}</code></div>`).join('')||'<div class="empty-cell">没有证据。</div>';await loadTasks();}catch(err){$('answer').textContent=`问答失败：${err.message}`;toast(err.message,true);}finally{btn.disabled=false;btn.textContent='询问知识库';}}

document.querySelectorAll('.tab').forEach((button)=>button.addEventListener('click',()=>{document.querySelectorAll('.tab').forEach((b)=>b.classList.toggle('active',b===button));document.querySelectorAll('.workspace').forEach((el)=>el.classList.remove('active'));$(`tab-${button.dataset.tab}`).classList.add('active');}));
$('refreshTasks').addEventListener('click',()=>loadTasks().catch((e)=>toast(e.message,true)));$('loadSummary').addEventListener('click',loadDaily);$('generateSummary').addEventListener('click',generateDaily);$('askButton').addEventListener('click',askKnowledge);$('question').addEventListener('keydown',(event)=>{if((event.ctrlKey||event.metaKey)&&event.key==='Enter')askKnowledge();});
const now=new Date();$('summaryDate').value=`${now.getFullYear()}-${String(now.getMonth()+1).padStart(2,'0')}-${String(now.getDate()).padStart(2,'0')}`;loadBuild();loadTasks().catch((e)=>toast(e.message,true));loadDaily();setInterval(()=>loadTasks().catch(()=>{}),15000);


async function loadLocalCoverage(){
  try{
    const day=$('summaryDate')?.value||new Date().toISOString().slice(0,10);
    const d=await api(`/api/intelligence/local/coverage?day=${encodeURIComponent(day)}`);
    $('localTotal').textContent=d.total??0;
    $('localDistricts').textContent=`${Object.keys(d.by_district||{}).length}/16`;
    $('localSanle').textContent=d.sanle_jiangning_count??0;
    if(d.collector){
      $('localCollector').textContent='已完成';
      $('localCollectorSub').textContent=`查询 ${d.collector.queries??0} · 新增 ${d.collector.inserted??0} · 去重 ${d.collector.duplicates??0}`;
    }else{
      $('localCollector').textContent='等待';
      $('localCollectorSub').textContent='每日 03:00 自动采集，也可手动触发';
    }
  }catch(err){
    $('localCollector').textContent='异常';
    $('localCollectorSub').textContent=err.message;
  }
}
async function runLocalCollect(){
  const btn=$('runLocalCollect');btn.disabled=true;btn.textContent='已加入采集队列…';
  try{
    const day=$('summaryDate')?.value||new Date().toISOString().slice(0,10);
    const r=await api(`/api/intelligence/local/collect?day=${encodeURIComponent(day)}`,{method:'POST'});
    toast(`上海本地采集已排队：${r.task_id||'queued'}`);
    setTimeout(loadLocalCoverage,3000);
  }catch(err){toast(err.message,true);}finally{btn.disabled=false;btn.textContent='立即采集上海本地源';}
}
$('runLocalCollect')?.addEventListener('click',runLocalCollect);
loadLocalCoverage();
setInterval(loadLocalCoverage,60000);

// V4.13 dark command-center deep-link support.
(function(){
  function activateFromHash(){
    const h=(location.hash||'').replace('#','');
    if(h==='qa'||h==='daily'){
      const btn=document.querySelector(`.tab[data-tab="${h}"]`); if(btn) btn.click();
      document.getElementById(`tab-${h}`)?.scrollIntoView({block:'start'});
    }else if(h==='queue'||h==='local'){
      document.getElementById(h)?.scrollIntoView({block:'start'});
    }
  }
  setTimeout(activateFromHash,120);window.addEventListener('hashchange',activateFromHash);
})();
