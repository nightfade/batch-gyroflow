#!/usr/bin/env python3
"""Local web UI for batch_gyroflow.py -- standard library only.

Starts a loopback HTTP server, opens the default browser at it, and drives
batch_gyroflow.py as a subprocess while streaming its output back to the page.

Why a browser and not a native window: this machine has no tkinter (Homebrew's
python@3.14 omits it) and no Xcode, and a web page needs nothing installed.
Folder and file pickers are the real macOS ones -- the browser cannot see local
paths, so the server shells out to `osascript` and returns the chosen path.

Everything is bound to 127.0.0.1 and every API call must carry a per-run token,
so another page in the same browser cannot drive this server.
"""

from __future__ import annotations

import http.server
import json
import os
import queue
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parent / "batch_gyroflow.py"
TOKEN = secrets.token_urlsafe(16)

# `[abc1234] Rendering progress: 55/60 frames (91.7%) ETA 0.1s`
PROGRESS_RE = re.compile(r"progress:\s*(\d+)/(\d+)\s*frames", re.IGNORECASE)
QUEUED_RE = re.compile(r"queued\s+(\d+)")
STAGE_RE = re.compile(r"^(Rendering|Applying LUT):\s*(.+)$")


class Job:
    """The single running batch, plus the state the page renders."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.process: subprocess.Popen[bytes] | None = None
        self.listeners: list[queue.Queue[str]] = []
        self.state: dict[str, Any] = self._idle()

    @staticmethod
    def _idle() -> dict[str, Any]:
        return {
            "running": False,
            "total": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
            "file": "",
            "stage": "",
            "percent": None,
            "exit": None,
        }

    def publish(self, kind: str, payload: Any) -> None:
        message = json.dumps({"kind": kind, "payload": payload})
        with self.lock:
            listeners = list(self.listeners)
        for listener in listeners:
            try:
                listener.put_nowait(message)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue[str]:
        listener: queue.Queue[str] = queue.Queue(maxsize=2000)
        with self.lock:
            self.listeners.append(listener)
        listener.put_nowait(json.dumps({"kind": "state", "payload": self.state}))
        return listener

    def unsubscribe(self, listener: queue.Queue[str]) -> None:
        with self.lock:
            if listener in self.listeners:
                self.listeners.remove(listener)

    def update(self, **changes: Any) -> None:
        self.state.update(changes)
        self.publish("state", self.state)

    def start(self, command: list[str]) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("a batch is already running")
        self.state = self._idle()
        self.update(running=True)
        self.publish("log", "$ " + " ".join(command))
        # Own process group so Stop can take Gyroflow/ffmpeg down with it.
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        buffer = b""
        while True:
            chunk = self.process.stdout.read(1)
            if not chunk:
                break
            # Gyroflow redraws progress with \r, so split on both terminators.
            if chunk in (b"\n", b"\r"):
                if buffer:
                    self._consume(buffer.decode("utf-8", "replace"))
                    buffer = b""
            else:
                buffer += chunk
        if buffer:
            self._consume(buffer.decode("utf-8", "replace"))
        code = self.process.wait()
        self.update(running=False, exit=code, stage="", percent=None)
        self.publish("log", f"— finished, exit code {code} —")

    def _consume(self, line: str) -> None:
        text = line.rstrip()
        if not text:
            return

        match = PROGRESS_RE.search(text)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            if total:
                self.update(percent=round(current * 100 / total, 1))
            return  # too noisy for the log

        self.publish("log", text)

        match = STAGE_RE.match(text)
        if match:
            self.update(stage=match.group(1), file=match.group(2), percent=None)
            return
        match = QUEUED_RE.search(text)
        if match:
            self.update(total=int(match.group(1)))
            return
        if text.startswith("OK:"):
            self.update(done=self.state["done"] + 1, percent=None)
        elif text.startswith("FAILED"):
            self.update(failed=self.state["failed"] + 1, percent=None)
        elif text.startswith("SKIP"):
            self.update(skipped=self.state["skipped"] + 1)

    def stop(self) -> bool:
        process = self.process
        if process is None or process.poll() is not None:
            return False
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            process.terminate()
        self.publish("log", "— stop requested —")
        return True


JOB = Job()


def choose_path(kind: str) -> str:
    """Native macOS picker. The browser cannot hand us a real local path."""
    prompt = {
        "folder": 'choose folder with prompt "Select a folder"',
        "cube": 'choose file with prompt "Select a .cube LUT" of type {"public.item"}',
    }[kind]
    completed = subprocess.run(
        ["osascript", "-e", f"POSIX path of ({prompt})"],
        capture_output=True,
        text=True,
    )
    # A cancelled dialog exits non-zero; treat it as "no selection".
    return completed.stdout.strip() if completed.returncode == 0 else ""


def build_command(config: dict[str, Any]) -> list[str]:
    source = str(config.get("source") or "").strip()
    if not source:
        raise ValueError("Choose a source folder first")

    mode = config.get("mode") or "stabilize"
    lut = str(config.get("lut") or "").strip()
    if mode in ("stabilize_lut", "lut_only") and not lut:
        raise ValueError("This mode needs a .cube LUT")

    command = [sys.executable, "-u", str(SCRIPT), source]

    output = str(config.get("output") or "").strip()
    if output:
        command += ["--output-dir", output]
    if lut and mode != "stabilize":
        command += ["--lut", lut]
    if mode == "lut_only":
        command.append("--lut-only")

    crop = config.get("crop")
    if crop not in (None, ""):
        percent = float(crop)
        if not 0 <= percent < 100:
            raise ValueError("Max crop must be between 0 and 99%")
        # max_zoom = 100 / (1 - crop); 15% -> 117.6
        command += ["--max-zoom", f"{100 / (1 - percent / 100):.3f}"]
    zoom_window = config.get("zoom_window")
    if zoom_window not in (None, ""):
        command += ["--zoom-window", str(zoom_window)]

    date_position = config.get("date_position") or ""
    if date_position:
        command += ["--add-date", date_position]
        if config.get("date_format"):
            command += ["--date-format", str(config["date_format"])]
        if config.get("date_utc"):
            command.append("--date-utc")

    for key, flag in (
        ("codec", "--codec"),
        ("bitrate", "--bitrate"),
        ("jobs", "--jobs"),
        ("timeout", "--timeout"),
        ("suffix", "--suffix"),
        ("readout_time", "--readout-time"),
        ("readout_direction", "--readout-direction"),
    ):
        value = config.get(key)
        if value not in (None, ""):
            command += [flag, str(value)]

    for key, flag in (
        ("overwrite", "--overwrite"),
        ("keep_intermediate", "--keep-intermediate"),
        ("no_recursive", "--no-recursive"),
        ("require_gyro", "--require-gyro-metadata"),
        ("dry_run", "--dry-run"),
    ):
        if config.get(key):
            command.append(flag)
    return command


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "BatchGyroflowGUI/1.0"

    def log_message(self, *_args: Any) -> None:  # keep the console clean
        pass

    # -- helpers -----------------------------------------------------------
    def _authorised(self) -> bool:
        query = urllib.parse.urlparse(self.path).query
        supplied = urllib.parse.parse_qs(query).get("token", [""])[0]
        if not supplied:
            supplied = self.headers.get("X-Token", "")
        return secrets.compare_digest(supplied, TOKEN)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: Any, code: int = 200) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    # -- routes ------------------------------------------------------------
    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        if route == "/":
            if not self._authorised():
                self._send(403, b"Invalid or missing token", "text/plain")
                return
            self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        elif route == "/api/events":
            if not self._authorised():
                self._send(403, b"forbidden", "text/plain")
                return
            self._stream()
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        if not self._authorised():
            self._json({"error": "forbidden"}, 403)
            return
        try:
            if route == "/api/choose":
                self._json({"path": choose_path(self._body().get("kind", "folder"))})
            elif route == "/api/start":
                JOB.start(build_command(self._body()))
                self._json({"ok": True})
            elif route == "/api/stop":
                self._json({"stopped": JOB.stop()})
            elif route == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, RuntimeError, KeyError) as error:
            self._json({"error": str(error)}, 400)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        listener = JOB.subscribe()
        try:
            while True:
                try:
                    message = listener.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")  # keep proxies/browsers happy
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {message}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            JOB.unsubscribe(listener)


PAGE = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Batch Gyroflow</title>
<style>
:root{color-scheme:light dark;--bg:#fbfbfd;--fg:#1d1d1f;--mut:#6e6e73;--line:#d2d2d7;
--card:#fff;--accent:#0071e3;--ok:#1d8a4e;--bad:#c9372c;--warn:#8a6100}
@media (prefers-color-scheme:dark){:root{--bg:#1c1c1e;--fg:#f5f5f7;--mut:#98989d;
--line:#3a3a3c;--card:#2c2c2e;--accent:#0a84ff;--ok:#30d158;--bad:#ff453a;--warn:#ffd60a}}
*{box-sizing:border-box}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif}
.wrap{max-width:760px;margin:0 auto}
h1{font-size:20px;margin:0 0 18px;font-weight:600}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:14px}
.row{display:grid;grid-template-columns:96px 1fr auto;gap:10px;align-items:center;margin-bottom:10px}
.row:last-child{margin-bottom:0}
label{color:var(--mut)}
input[type=text],input[type=number],select{width:100%;padding:7px 9px;border:1px solid var(--line);
border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
button{padding:7px 15px;border:1px solid var(--line);border-radius:7px;background:var(--card);
color:var(--fg);font:inherit;cursor:pointer}
button:hover{border-color:var(--accent)}
button.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:500}
button:disabled{opacity:.4;cursor:not-allowed}
.modes{display:flex;gap:8px;margin-bottom:16px}
.modes button{flex:1}
.modes button[aria-pressed=true]{background:var(--accent);color:#fff;border-color:var(--accent)}
.inline{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.inline > div{display:flex;gap:8px;align-items:center}
.inline input[type=number]{width:88px}
details summary{cursor:pointer;color:var(--mut);margin-bottom:12px}
.adv{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.adv label{display:block;margin-bottom:3px;font-size:12px}
.bar{height:7px;background:var(--line);border-radius:4px;overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;background:var(--accent);width:0;transition:width .2s}
.bar>i.indet{width:35%;animation:sweep 1.1s ease-in-out infinite}
@keyframes sweep{0%{margin-left:-35%}100%{margin-left:100%}}
.status{display:flex;justify-content:space-between;color:var(--mut);font-size:12px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:11px;
height:230px;overflow:auto;margin:0;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;
white-space:pre-wrap;word-break:break-all}
.ok{color:var(--ok)}.bad{color:var(--bad)}.warn{color:var(--warn)}
.err{color:var(--bad);margin-top:10px;min-height:18px;font-size:13px}
.actions{display:flex;gap:9px;margin-top:16px;align-items:center}
.actions .sp{flex:1}
</style></head><body><div class="wrap">
<h1>Batch Gyroflow</h1>

<div class="card">
  <div class="modes">
    <button data-mode="stabilize" aria-pressed="true">增稳</button>
    <button data-mode="stabilize_lut" aria-pressed="false">增稳 + LUT</button>
    <button data-mode="lut_only" aria-pressed="false">只套 LUT</button>
  </div>
  <div class="row"><label>素材</label><input type="text" id="source" placeholder="必填">
    <button data-pick="source" data-kind="folder">选择…</button></div>
  <div class="row"><label>输出</label><input type="text" id="output" placeholder="留空 = 素材目录下 gyroflow_stabilized/">
    <button data-pick="output" data-kind="folder">选择…</button></div>
  <div class="row" id="lutRow"><label>LUT</label><input type="text" id="lut" placeholder=".cube 文件">
    <button data-pick="lut" data-kind="cube">选择…</button></div>
</div>

<div class="card">
  <div class="inline">
    <div id="cropWrap"><label>最大裁切</label>
      <input type="number" id="crop" min="0" max="99" step="0.5" placeholder="默认">
      <span style="color:var(--mut)">%</span></div>
    <div><label>日期命名</label><select id="date_position">
      <option value="">不加</option><option value="prefix">前缀</option><option value="suffix">后缀</option>
    </select></div>
  </div>
</div>

<details class="card"><summary>高级选项</summary><div class="adv">
  <div><label>编码</label><select id="codec">
    <option>H.265/HEVC</option><option>H.264/AVC</option><option>ProRes</option><option>DNxHD</option>
  </select></div>
  <div><label>码率 Mbps（0 = 自动）</label><input type="number" id="bitrate" value="100" min="0"></div>
  <div><label>并行数</label><input type="number" id="jobs" value="1" min="1"></div>
  <div><label>单文件超时 秒</label><input type="number" id="timeout" value="7200" min="0"></div>
  <div><label>输出后缀</label><input type="text" id="suffix" placeholder="_stabilized"></div>
  <div><label>裁切窗口 秒（-1 = 恒定）</label><input type="number" id="zoom_window" step="0.5" placeholder="默认"></div>
  <div><label>果冻校正 读出时间 ms</label><input type="number" id="readout_time" step="0.1" placeholder="关闭"></div>
  <div><label>读出方向</label><select id="readout_direction">
    <option value="">默认 TopToBottom</option><option>TopToBottom</option><option>BottomToTop</option>
    <option>LeftToRight</option><option>RightToLeft</option></select></div>
  <div><label><input type="checkbox" id="overwrite"> 覆盖已存在的输出</label>
       <label><input type="checkbox" id="no_recursive"> 不递归子目录</label></div>
  <div><label><input type="checkbox" id="keep_intermediate"> 保留 ProRes 中间层</label>
       <label><input type="checkbox" id="require_gyro"> 无陀螺数据则跳过</label></div>
</div></details>

<div class="card">
  <div class="bar"><i id="bar"></i></div>
  <div class="status"><span id="now">就绪</span><span id="tally"></span></div>
  <div class="actions">
    <button class="primary" id="start">开始</button>
    <button id="dry">试跑</button>
    <button id="stop" disabled>停止</button>
    <span class="sp"></span>
    <button id="clear">清空日志</button>
    <button id="quit">退出</button>
  </div>
  <div class="err" id="err"></div>
</div>

<pre id="log"></pre>
</div><script>
const T="__TOKEN__", $=id=>document.getElementById(id);
let mode="stabilize", running=false;

const api=async(path,body)=>{
  const r=await fetch(path+"?token="+T,{method:"POST",headers:{"Content-Type":"application/json","X-Token":T},
    body:JSON.stringify(body||{})});
  const d=await r.json().catch(()=>({error:"bad response"}));
  if(!r.ok) throw new Error(d.error||("HTTP "+r.status));
  return d;
};

document.querySelectorAll(".modes button").forEach(b=>b.onclick=()=>{
  mode=b.dataset.mode;
  document.querySelectorAll(".modes button").forEach(o=>o.setAttribute("aria-pressed",o===b));
  $("lutRow").style.opacity = mode==="stabilize" ? .45 : 1;
  $("lut").disabled = mode==="stabilize";
  // Crop only exists when Gyroflow actually stabilises.
  $("cropWrap").style.opacity = mode==="lut_only" ? .45 : 1;
  $("crop").disabled = mode==="lut_only";
});

document.querySelectorAll("[data-pick]").forEach(b=>b.onclick=async()=>{
  b.disabled=true;
  try{ const {path}=await api("/api/choose",{kind:b.dataset.kind}); if(path) $(b.dataset.pick).value=path; }
  catch(e){ $("err").textContent=e.message; }
  finally{ b.disabled=false; }
});

const val=id=>{const el=$(id); return el.type==="checkbox"?el.checked:el.value.trim();};
const config=dry=>({mode,dry_run:dry,
  source:val("source"),output:val("output"),lut:val("lut"),
  crop:$("crop").disabled?"":val("crop"), zoom_window:val("zoom_window"),
  date_position:val("date_position"), codec:val("codec"), bitrate:val("bitrate"),
  jobs:val("jobs"), timeout:val("timeout"), suffix:val("suffix"),
  readout_time:val("readout_time"), readout_direction:val("readout_direction"),
  overwrite:val("overwrite"), keep_intermediate:val("keep_intermediate"),
  no_recursive:val("no_recursive"), require_gyro:val("require_gyro")});

const run=async dry=>{ $("err").textContent="";
  try{ await api("/api/start",config(dry)); }catch(e){ $("err").textContent=e.message; } };
$("start").onclick=()=>run(false);
$("dry").onclick=()=>run(true);
$("stop").onclick=()=>api("/api/stop").catch(e=>$("err").textContent=e.message);
$("clear").onclick=()=>$("log").textContent="";
$("quit").onclick=async()=>{ await api("/api/quit").catch(()=>{}); document.body.innerHTML=
  '<div class="wrap"><h1>已退出</h1><p style="color:var(--mut)">可以关闭这个标签页了。</p></div>'; };

const cls=t=>t.startsWith("OK:")?"ok":(t.startsWith("FAILED")?"bad":
  (t.startsWith("WARNING")||t.startsWith("NOTE")||t.startsWith("SKIP")?"warn":""));

const es=new EventSource("/api/events?token="+T);
es.onmessage=e=>{
  const {kind,payload}=JSON.parse(e.data);
  if(kind==="log"){
    const box=$("log"), atEnd=box.scrollTop+box.clientHeight>=box.scrollHeight-30;
    const s=document.createElement("span"); s.className=cls(payload); s.textContent=payload+"\n";
    box.appendChild(s); if(atEnd) box.scrollTop=box.scrollHeight;
    return;
  }
  const st=payload; running=st.running;
  $("start").disabled=running; $("dry").disabled=running; $("stop").disabled=!running;
  const bar=$("bar");
  if(st.percent!==null&&st.percent!==undefined){ bar.className=""; bar.style.width=st.percent+"%"; }
  else if(running&&st.stage){ bar.className="indet"; bar.style.width=""; }
  else { bar.className=""; bar.style.width=(st.total?100*st.done/st.total:0)+"%"; }
  $("now").textContent = running
    ? (st.stage?`${st.stage==="Rendering"?"渲染":"套 LUT"} ${st.file}`:"准备中…")
    : (st.exit===null?"就绪":(st.exit===0?"完成":`结束，退出码 ${st.exit}`));
  $("tally").textContent = st.total||st.done||st.failed||st.skipped
    ? `${st.done} 成功 · ${st.failed} 失败 · ${st.skipped} 跳过 / 共 ${st.total}` : "";
};
</script></body></html>
"""


def main() -> int:
    if not SCRIPT.is_file():
        print(f"batch_gyroflow.py not found next to this file: {SCRIPT}", file=sys.stderr)
        return 2

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    # Unbuffered: when launched from the .app the output goes to a pipe, and a
    # buffered URL that never appears looks like a hung launcher.
    print(f"Batch Gyroflow UI: {url}", flush=True)
    print("Use the Quit button in the page, or press Ctrl-C here.", flush=True)

    def open_browser() -> None:
        try:
            webbrowser.open(url)
        except Exception as error:  # headless or no default browser
            print(f"Could not open a browser ({error}); visit the URL above.", flush=True)

    threading.Timer(0.4, open_browser).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        JOB.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
