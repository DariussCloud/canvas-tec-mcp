import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

script, uv = Path(sys.argv[1]), sys.argv[2]
os.environ["CANVAS_CONFIG_DIR"] = tempfile.mkdtemp()
os.environ["CANVAS_AUTH"] = "session"
spec = importlib.util.spec_from_file_location("canvas", script)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
green, red, *_ , reset = m.palette()
results = []


def check(label: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print(f"{green + '✓' if ok else red + '✗'} {label}{reset}  {detail}", flush=True)


check("zona horaria (tzdata)", m.local("2026-11-08T18:00:00Z").startswith("2026-11-08 12:00"), m.local("2026-11-08T18:00:00Z"))

data = {"saved_at": "2026-11-08T18:00:00+00:00", "user": {"id": 1, "name": "Prueba"},
        "cookies": [{"name": "canvas_session", "value": "z" * 800, "domain": m.HOST, "path": "/"}]}
where = m.save_session(data)
raw = m.SESSION_FILE.read_bytes()
check("session.json cifrado", (raw.startswith(b"DPAPI:") if sys.platform == "win32" else not raw.startswith(b"{")) and b"zzzzzzzz" not in raw, where)
check("session.json se descifra", m.load_session() == data)

from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content("<p>ok</p>")
    check("Chromium de Playwright", page.inner_text("p") == "ok", browser.version)
    browser.close()

env = {**os.environ, "CANVAS_CONFIG_DIR": tempfile.mkdtemp()}
r = subprocess.run([uv, "run", "--script", str(script), "--test"], capture_output=True, env=env)
out = r.stdout.decode("utf-8", "replace")
check("--test sin sesión pide --login", r.returncode == 1 and "Sin sesión" in out and "--login" in out, out.strip().splitlines()[-1] if out.strip() else r.stderr.decode()[-200:])

proc = subprocess.Popen([uv, "run", "--script", str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
for msg in ({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}):
    proc.stdin.write((json.dumps(msg) + "\n").encode())
proc.stdin.flush()
tools = []
for line in proc.stdout:
    reply = json.loads(line)
    if reply.get("id") == 2:
        tools = [t["name"] for t in reply["result"]["tools"]]
        break
proc.stdin.close()
proc.wait(timeout=30)
check("servidor MCP por stdio", "session_status" in tools and "download_file" in tools, f"{len(tools)} herramientas")

sys.exit(0 if all(results) else 1)
