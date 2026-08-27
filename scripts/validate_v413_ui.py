#!/usr/bin/env python3
from pathlib import Path
import re,sys
root=Path('.')
checks=[]
def need(rel, token):
    p=root/rel
    ok=p.exists() and token in p.read_text(encoding='utf-8')
    checks.append((ok,f'{rel}: {token}'))
need('frontend/index.html','V4.13 DARK COMMAND CENTER')
need('frontend/index.html','今日情报态势')
need('frontend/index.html','上海重点区域')
need('frontend/index.html','静安区 / 江宁路街道 / 三乐')
need('frontend/index.html','处理流水线')
need('frontend/index.html','任务监控')
need('frontend/index.html','系统资源负载')
need('frontend/command-center.css','--bg:#070b10')
need('frontend/command-center.css','--blue:#00b8ff')
need('frontend/command-center.js','/api/intelligence/tasks?limit=240')
need('frontend/command-center.js','/api/intelligence/local/coverage')
need('frontend/command-center.js','/api/intelligence/daily/')
need('frontend/command-center.js','/api/system/status')
need('frontend/command-center.js','/api/sources')
need('frontend/classic.html','返回黑色指挥舱')
need('frontend/insights.html','INTELLIGENCE OPERATIONS')
need('frontend/insights.html','上海本地采集')
need('frontend/insights.css','--bg:#070b10')
need('frontend/Dockerfile','COPY classic.html /usr/share/nginx/html/classic.html')
need('frontend/Dockerfile','COPY command-center.css /usr/share/nginx/html/command-center.css')
need('frontend/Dockerfile','COPY command-center.js /usr/share/nginx/html/command-center.js')
# Ensure this UI patch did not introduce any scheduling/deadline logic.
for rel in ('frontend/index.html','frontend/command-center.js','frontend/insights.html','frontend/insights.js'):
    s=(root/rel).read_text(encoding='utf-8')
    if re.search(r'09:00|6\s*小时.*(?:截止|硬收口)|硬截止',s): checks.append((False,f'{rel}: contains removed SLA/deadline concept'))
failed=[msg for ok,msg in checks if not ok]
if failed:
    print('V4.13 DARK UI VALIDATION FAILED')
    for x in failed: print(' -',x)
    sys.exit(1)
print('V4.13 DARK UI VALIDATION PASSED')
print('dark command center -> Shanghai focus -> serial AI/task monitor -> report/QA -> classic advanced management preserved')
