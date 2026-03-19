#!/usr/bin/env python3
import json
import sys
from pathlib import Path
from datetime import datetime, timezone

if len(sys.argv) < 3:
    raise SystemExit("usage: promote_memory.py <memory_json_file> <target_markdown_file>")

src = Path(sys.argv[1])
target = Path(sys.argv[2])
entry = json.loads(src.read_text())

stamp = datetime.now(timezone.utc).isoformat()
section = []
section.append(f"\n## {entry.get('title', 'Promoted memory')}\n")
section.append(f"- Type: {entry.get('type', 'unknown')}\n")
section.append(f"- Scope: {entry.get('scope', 'unknown')}\n")
section.append(f"- Confidence: {entry.get('confidence', 'unknown')}\n")
if entry.get('project'):
    section.append(f"- Project: {entry['project']}\n")
if entry.get('repo'):
    section.append(f"- Repo: {entry['repo']}\n")
section.append(f"- Promoted at: {stamp}\n")
section.append(f"- Statement: {entry.get('statement', '')}\n")
if entry.get('summary'):
    section.append(f"- Summary: {entry.get('summary')}\n")
if entry.get('source_ref'):
    section.append(f"- Source: {entry.get('source_ref')}\n")

existing = target.read_text() if target.exists() else "# Promoted Memory\n"
target.write_text(existing.rstrip() + "\n" + "".join(section) + "\n")
print(f"Promoted memory into {target}")
