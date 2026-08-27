#!/usr/bin/env python3
from pathlib import Path
import re
import sys

ROOT = Path.cwd()
errors=[]

def text(rel):
    p=ROOT/rel
    if not p.exists():
        errors.append(f'missing {rel}')
        return ''
    return p.read_text(encoding='utf-8')

def need(rel,*needles):
    s=text(rel)
    for n in needles:
        if n not in s:
            errors.append(f'{rel}: missing {n!r}')
    return s

compose=need('docker-compose.yml','monitor:','--concurrency=1','--queues=research','--queues=control','QUEUE_WATCHDOG_SECONDS','AI_JOB_TTL_SECONDS')
try:
    import yaml
    _cfg=yaml.safe_load(compose) or {}
    _svcs=_cfg.get("services") or {}
    _worker=_svcs.get("worker") or {}
    _monitor=_svcs.get("monitor") or {}
    if _worker.get("networks") != _monitor.get("networks"):
        errors.append(f'docker-compose.yml: monitor networks must match worker; worker={_worker.get("networks")!r} monitor={_monitor.get("networks")!r}')
    if _worker.get("network_mode") != _monitor.get("network_mode"):
        errors.append(f'docker-compose.yml: monitor network_mode must match worker; worker={_worker.get("network_mode")!r} monitor={_monitor.get("network_mode")!r}')
except Exception as exc:
    errors.append(f'docker-compose.yml: YAML parse failed during network contract check: {exc}')
if compose.count('--concurrency=1') < 2:
    errors.append('docker-compose.yml: worker and monitor must both declare concurrency=1')
for forbidden in ('OLLAMA_NUM_PARALLEL','OLLAMA_MAX_QUEUE','OLLAMA_MAX_LOADED_MODELS'):
    if forbidden in compose:
        errors.append(f'docker-compose.yml: {forbidden} must not be overridden')

tasks=need('backend/app/tasks.py','INTERNETBOARD V4.10 AI SERIAL QUEUE','internetboard.run_research','internetboard.intelligence_daily_summary_v410','internetboard.intelligence_qa_v410','internetboard.queue_watchdog_v410','internetboard.requeue_stalled_v410','_v410_routes.update','_v410_record_enqueued_task')
if 'celery_app.conf.task_routes = {' in tasks:
    errors.append('backend/app/tasks.py: task routes must merge, not replace existing routes')

queue=need('backend/app/queue_runtime.py','build_queue_view','monitor_stalled_runs','create_ai_job','list_ai_jobs','_query_running_rows','celery_app.control.revoke','AI_JOB_ACTIVE')
if 'order_by(Run.id.asc()).limit(300)' in queue:
    errors.append('backend/app/queue_runtime.py: stale oldest-300 monitor query still present')

intel=need('backend/app/intelligence.py','/api/intelligence','@router.get("/tasks")','@router.post("/runs/{run_id}/resume"','@router.post("/daily/{day}/generate"','@router.post("/ask"','@router.get("/jobs/{job_id}")','create_ai_job','celery_app.send_task','execute_daily_summary_job','execute_knowledge_qa_job')
# HTTP handlers must enqueue heavy AI work, not call Ollama directly.
for fn in ('enqueue_daily_summary','enqueue_knowledge_question'):
    m=re.search(rf'def {fn}\([^)]*\):(?P<body>.*?)(?=\n\ndef |\Z)', intel, re.S)
    if m and '_ollama_chat(' in m.group('body'):
        errors.append(f'backend/app/intelligence.py: {fn} must not call Ollama synchronously')
if 'Manual resume enqueue failed' not in intel:
    errors.append('backend/app/intelligence.py: failed-run resume rollback is missing')

need('frontend/insights.html','AI 串行任务队列','全部预计完成','当日总结','本地知识库 AI 问答')
need('frontend/insights.js','pollJob','/api/intelligence/jobs/','queue_position','eta_complete_at','统一 AI 串行队列')
need('frontend/task-overlay.js','/api/intelligence/tasks','排队','all_complete_at')
need('backend/app/main.py','app.include_router(intelligence_router)','v4.10.2-production-reconcile')

prod=text('scripts/validate_production.py')
legacy_serial_markers = (
    "Celery worker must not be pinned to one process",
    "worker must not be pinned",
    "assert '--concurrency=1' not in",
    'assert "--concurrency=1" not in',
)
if any(x in prod for x in legacy_serial_markers):
    errors.append('scripts/validate_production.py: old anti-serial production rule still present')
if 'INTERNETBOARD V4.10.2 SERIAL PRODUCTION CONTRACT' not in prod:
    errors.append('scripts/validate_production.py: V4.10.2 serial production contract missing')

if errors:
    print('V4.10.2 PRODUCTION RECONCILIATION FAILED')
    for e in errors: print(' -',e)
    sys.exit(1)
print('V4.10.2 PRODUCTION RECONCILIATION PASSED')
print('one AI queue -> concurrency=1 research worker; independent control monitor; async daily summary/KB QA; ETA + stalled recovery; legacy anti-serial contract retired')
