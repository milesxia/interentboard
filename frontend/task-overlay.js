(() => {
  const link = document.getElementById('intelligenceCenterLink');
  if (!link) return;
  async function refresh() {
    try {
      const response = await fetch('/api/intelligence/tasks?limit=60', {cache:'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      const active = Number(data.counts?.active || 0);
      const failed = Number(data.counts?.failed || 0);
      if (active > 0 && failed > 0) link.textContent = `情报中心 · 运行 ${active} / 失败 ${failed}`;
      else if (active > 0) link.textContent = `情报中心 · 运行 ${active}`;
      else if (failed > 0) link.textContent = `情报中心 · 失败 ${failed}（已终止）`;
      else link.textContent = '情报中心';
      link.title = failed > 0 ? 'FAILED 为终态，不占用活跃任务；点击进入可查看原因并从原 Run 重新入队。' : '当日总结、本地知识库 AI 问答';
    } catch (_) {}
  }
  refresh();
  setInterval(refresh, 15000);
})();
