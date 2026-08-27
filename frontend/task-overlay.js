(() => {
  const link=document.getElementById('intelligenceCenterLink');
  if(!link)return;
  const clock=(value)=>{if(!value)return'';const d=new Date(value);return Number.isNaN(d.getTime())?'':d.toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',hour12:false});};
  async function refresh(){
    try{
      const response=await fetch('/api/intelligence/tasks?limit=120',{cache:'no-store'});if(!response.ok)return;
      const data=await response.json();const q=data.queue||{};const failed=Number(data.counts?.failed||0);const queued=Number(q.queued_count||0);const current=q.current;
      const parts=['运行看板'];
      if(current)parts.push(`#${current.id} ${Math.round(Number(current.progress_percent)||0)}%`);
      if(queued)parts.push(`排队 ${queued}`);
      if(q.all_complete_at)parts.push(`约 ${clock(q.all_complete_at)} 完成`);
      if(failed)parts.push(`失败 ${failed}`);
      link.textContent=parts.join(' · ');
      link.title='单 AI 串行队列、ETA、卡死恢复、当日总结、本地知识库问答';
    }catch(_){}
  }
  refresh();setInterval(refresh,15000);
})();
