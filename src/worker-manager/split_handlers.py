import ast
import os

source_file = 'worker_handlers.py'
with open(source_file, 'r') as f:
    source = f.read()
    lines = source.split('\n')

tree = ast.parse(source)

handler_names = [
    'handle_pm_dispatcher_worker',
    'handle_implementer_worker',
    'handle_tester_worker',
    'handle_reviewer_worker'
]

os.makedirs('handlers', exist_ok=True)
with open('handlers/__init__.py', 'w') as f:
    f.write("")

imports = """
import os
import json
import time
import subprocess
import tempfile
import asyncio
from pathlib import Path
from worker_handlers import *
from agent_runner import (
    run_pm_dispatcher,
    run_implementer,
    run_tester,
    run_reviewer,
    _get_active_llm_model,
    _run_with_client
)
"""

handler_nodes = []
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in handler_names:
        handler_nodes.append(node)

# Create the handler files
for node in handler_nodes:
    start = node.lineno - 1
    end = node.end_lineno
    # Some decorators might be there? No.
    content = "\n".join(lines[start:end])
    
    name_parts = node.name.split('_')
    module_name = name_parts[1]
    
    with open(f"handlers/{module_name}.py", "w") as f:
        f.write(imports.strip() + "\n\n" + content + "\n")

# Rewrite worker_handlers.py to remove the handlers
new_lines = []
skip_ranges = []
for node in handler_nodes:
    skip_ranges.append((node.lineno - 1, node.end_lineno))

for i, line in enumerate(lines):
    skip = False
    for start, end in skip_ranges:
        if start <= i < end:
            skip = True
            break
    if not skip:
        new_lines.append(line)

new_source = "\n".join(new_lines)
with open(source_file, 'w') as f:
    f.write(new_source)

