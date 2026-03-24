with open('/tmp/server_new_small.py', 'r') as f:
    lines = f.readlines()

byob_routes = lines[32:118]

with open('src/dashboard/server.py', 'r') as f:
    content = f.read()

if "from pydantic import BaseModel" not in content:
    byob_routes.insert(0, "from pydantic import BaseModel\n")

content = content.replace('if __name__ == "__main__":', ''.join(byob_routes) + '\n\nif __name__ == "__main__":')

with open('src/dashboard/server.py', 'w') as f:
    f.write(content)
