#!/usr/bin/env python3
from pathlib import Path
import sys
ROOT=Path.cwd(); errors=[]
def req(rel,*needles):
    p=ROOT/rel
    if not p.exists(): errors.append(f'missing {rel}'); return ''
    s=p.read_text(encoding='utf-8')
    for n in needles:
        if n not in s: errors.append(f'{rel}: missing {n!r}')
    return s
req('backend/app/intelligence.py','APIRouter(prefix="/api/intelligence"','@router.get("/tasks")','@router.post("/runs/{run_id}/resume"','@router.get("/daily/{day}")','@router.post("/daily/{day}/generate"','@router.post("/ask"','@router.get("/jobs/{job_id}")','daily_summaries','knowledge_chat','celery_app.send_task')
req('backend/app/main.py','app.include_router(intelligence_router)','v4.10.2-production-reconcile')
req('frontend/insights.html','AI 串行任务队列','失败任务','当日总结','本地知识库 AI 问答','/insights.js')
req('frontend/insights.js','/api/intelligence/tasks','/api/intelligence/runs/','/api/intelligence/daily/','/api/intelligence/ask','/api/intelligence/jobs/','pollJob')
req('frontend/Dockerfile','COPY insights.html','COPY insights.js','COPY insights.css','COPY task-overlay.js')
req('frontend/task-overlay.js','/api/intelligence/tasks','失败','all_complete_at')
req('frontend/index.html','/insights.html','/task-overlay.js','运行看板')
if errors:
    print('INTELLIGENCE CENTER VALIDATION FAILED')
    for e in errors: print(' -',e)
    sys.exit(1)
print('INTELLIGENCE CENTER VALIDATION PASSED')
print('failed-run recovery + async daily summary + async local KB QA + serial AI queue UI present')
