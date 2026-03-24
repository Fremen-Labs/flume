import time
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
