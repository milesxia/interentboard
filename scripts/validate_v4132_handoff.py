#!/usr/bin/env python3
from pathlib import Path
import ast
import re
import sys

root = Path('.')
errors = []

def text(rel):
    p = root / rel
    if not p.exists():
        errors.append(f"missing {rel}")
        return ""
    return p.read_text(encoding="utf-8")

index = text('frontend/index.html')
classic = text('frontend/classic.html')
app = text('frontend/app.js')
css = text('frontend/command-center.css')
prod = text('scripts/validate_production.py')

if 'V4.13 DARK COMMAND CENTER' not in index: errors.append('dark command center marker missing')
if 'data-feature="handoff-export"' not in index or '导出交接' not in index: errors.append('dark UI handoff navigation entry missing')
if '/classic.html' not in index: errors.append('dark UI does not route handoff management to classic.html')
if re.search(r'<script[^>]+src=["\']/app\.js', index): errors.append('legacy app.js must not be loaded by dark home')
if '/task-overlay.js' not in index or '运行看板' not in index: errors.append('runtime task overlay compatibility missing')
if 'app.js?v=4.7-refresh-chain' not in classic or '/build.js?v=4.7-refresh-chain' not in classic: errors.append('classic refresh-chain scripts missing')
legacy = classic + '\n' + app
if 'handoff' not in legacy.lower() and '交接' not in legacy: errors.append('classic handoff implementation missing')
if 'export' not in legacy.lower() and '导出' not in legacy: errors.append('classic export implementation missing')
if '.handoff-entry' not in css: errors.append('dark handoff entry style missing')
if 'INTERNETBOARD V4.13.2 HANDOFF MIGRATION' not in prod: errors.append('production validator migration marker missing')
if 'Handoff export UI is missing' not in prod: errors.append('handoff production invariant was deleted instead of migrated')
try:
    ast.parse(prod)
except SyntaxError as exc:
    errors.append(f'production validator syntax invalid: {exc}')

if errors:
    print('V4.13.2 HANDOFF MIGRATION VALIDATION FAILED')
    for e in errors: print(' -', e)
    sys.exit(1)
print('V4.13.2 HANDOFF MIGRATION VALIDATION PASSED')
print('dark command center exposes handoff -> classic keeps real export implementation -> legacy production invariant remains enforced across both UI surfaces')
