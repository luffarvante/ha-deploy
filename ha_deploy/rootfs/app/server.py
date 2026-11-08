import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

CONFIG_DIR = "/config"
TEST_FILE = os.path.join(CONFIG_DIR, "ha-deploy-test.txt")
PORT = 8099

INDEX_HTML = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>HA Deploy</title></head>
<body>
  <h1>HA Deploy v0.1</h1>
  <p>Click to create /config/ha-deploy-test.txt with "OHAI".</p>
  <button id="btn">Create test file</button>
  <pre id="out"></pre>
  <script>
    document.getElementById('btn').onclick = async () => {
      const out = document.getElementById('out');
      out.textContent = 'Working...';
      try {
        const r = await fetch('create', { method: 'POST' });
        const j = await r.json();
        out.textContent = JSON.stringify(j, null, 2);
      } catch (e) {
        out.textContent = 'Error: ' + e;
      }
    };
  </script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if self.path == "/":
            data = INDEX_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/create":
            try:
                with open(TEST_FILE, "w") as f:
                    f.write("OHAI\n")
                result = {"ok": True, "path": TEST_FILE}
            except Exception as e:
                result = {"ok": False, "error": str(e)}
            data = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, fmt, *args):
        # keep logs quiet
        pass


if __name__ == "__main__":
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"HA Deploy listening on {PORT}", flush=True)
    httpd.serve_forever()
