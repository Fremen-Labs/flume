import http.server
import time
import threading
import urllib.request
import sys

class SlowHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        time.sleep(6)
        try:
            self.send_response(200)
            self.end_headers()
        except:
            pass
    def log_message(self, format, *args):
        pass

server = http.server.HTTPServer(('localhost', 9292), SlowHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()

try:
    print("Executing OpenBao security audit payload natively against Elasticsearch boundary...")
    req = urllib.request.Request("http://localhost:9292/agent-security-audits/_doc", data=b'{}', method="POST")
    # Native Flume flume_secrets.py defines `timeout=5`!
    with urllib.request.urlopen(req, timeout=5) as r:
        print("Success! Audit logged.")
except Exception as audit_e:
    # Flume uses a bare exception catch and a basic logger.warning
    print(f"\n[VULNERABILITY CONFIRMED]: {type(audit_e).__name__} caught -> {audit_e}")
    print("Silent failure executed. Native execution continued, but the Audit Event was completely DROPPED with no upstream propagation or retry logic.")
