import subprocess
import time
import os
import signal
import sys
import sqlite3

print("Spawning a mock worker subclass to simulate an active Flume Agent Session...")
with open("mock_agent_runner.py", "w") as f:
    f.write("""import time
import sqlite3

print("Agent started! Locking database internally...")
conn = sqlite3.connect("test_queue.db")
cursor = conn.cursor()
cursor.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, status TEXT)")
cursor.execute("INSERT INTO tasks (status) VALUES ('IN_PROGRESS')")
conn.commit()

# No signal.SIGTERM registered! It will die instantly!
try:
    while True:
        time.sleep(1)
except BaseException as e:
    cursor.execute("UPDATE tasks SET status='ABORTED'")
    conn.commit()
""")

try: os.remove("test_queue.db")
except: pass

print("Launching Agent Task...")
proc = subprocess.Popen([sys.executable, "mock_agent_runner.py"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
time.sleep(1.5)

print(f"Sending SIGTERM (kill -15) to Docker container equivalent {proc.pid}...")
os.kill(proc.pid, signal.SIGTERM)
proc.wait()

conn = sqlite3.connect("test_queue.db")
res = conn.execute("SELECT status FROM tasks ORDER BY id DESC LIMIT 1").fetchone()
if res and res[0] == "IN_PROGRESS":
    print(f"\n[VULNERABILITY CONFIRMED]: SQLite database orphaned! Status is permanently stuck at 'IN_PROGRESS' because SIGTERM bypassed Python's garbage collection.")
else:
    print(f"\n[MITIGATED]: Status properly reverted to {res[0] if res else 'None'}.")
