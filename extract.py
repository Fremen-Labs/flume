import ast

with open('src/worker-manager/worker_handlers.py', 'r') as f:
    source = f.read()

tree = ast.parse(source)

def extract_fn(name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = node.lineno - 1
            end = node.end_lineno
            return "\n".join(source.split('\n')[start:end])
    return ""

for fn in ['handle_pm_dispatcher_worker', 'handle_implementer_worker', 'handle_tester_worker', 'handle_reviewer_worker']:
    content = extract_fn(fn)
    with open(f"src/worker-manager/handlers/{fn.split('_')[1]}.py", "w") as f:
        f.write(content + "\n")
