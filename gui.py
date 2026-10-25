#!/usr/bin/env python3
"""Local web UI for batch_gyroflow.py -- standard library only.

Starts a loopback HTTP server and opens the default browser at it. The page
collects the options and writes a bash script that runs batch_gyroflow.py; the
batch itself runs in Terminal, so progress and errors are visible as they
happen and the full output is also saved to a log next to the script.

Why a browser and not a native window: this machine has no tkinter (Homebrew's
python@3.14 omits it) and no Xcode, and a web page needs nothing installed.
Folder and file pickers are the real macOS ones -- the browser cannot see local
paths, so the server shells out to `osascript` and returns the chosen path.

Everything is bound to 127.0.0.1 and every API call must carry a per-run token,
so another page in the same browser cannot drive this server.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT = Path(__file__).resolve().parent / "batch_gyroflow.py"
TOKEN = secrets.token_urlsafe(16)

RUNS_DIR = "_runs"
# Scripts this server wrote; only these may be opened in Terminal or Finder.
GENERATED: set[str] = set()

# Remembered between launches; the page's own storage is lost because every
# run gets a new port and therefore a new origin.
SETTINGS = Path.home() / "Library" / "Application Support" / "BatchGyroflow" / "gui.json"
PREVIEW_DIR = Path(tempfile.mkdtemp(prefix="batch-gyroflow-preview-"))
PREVIEW_WIDTHS = (360, 1280)
_BASE_LOCK = threading.Lock()


def _load_batch_module() -> Any:
    spec = importlib.util.spec_from_file_location("batch_gyroflow", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


BATCH = _load_batch_module()


def load_settings() -> dict[str, Any]:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(changes: dict[str, Any]) -> dict[str, Any]:
    settings = {**load_settings(), **{k: v for k, v in changes.items() if k in ("lut_dir",)}}
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings


def list_clips(source: str) -> list[str]:
    """Videos the batch would pick up, so the preview frame is real footage."""
    root = Path(source.strip()).expanduser()
    if not source.strip() or not root.is_dir():
        return []
    videos = BATCH.discover_videos(
        root, root / "gyroflow_stabilized", recursive=True, suffix="_stabilized"
    )
    return [str(video) for video in videos if not video.name.startswith("._")]


def list_luts(folder: str) -> list[str]:
    root = Path(folder.strip()).expanduser()
    if not folder.strip() or not root.is_dir():
        return []
    # `._name.cube` are AppleDouble stubs that macOS leaves on exFAT cards.
    luts = [
        path
        for path in root.rglob("*")
        if path.suffix.lower() == ".cube" and path.is_file() and not path.name.startswith(".")
    ]
    return [str(path) for path in sorted(luts, key=lambda path: str(path).lower())]


def _probe_duration(clip: Path) -> float:
    completed = subprocess.run(
        [str(BATCH.DEFAULT_FFPROBE), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(clip)],
        capture_output=True,
        text=True,
    )
    try:
        return max(float(completed.stdout.strip()), 0.0)
    except ValueError:
        return 0.0


def _cache_key(*parts: object) -> str:
    return hashlib.sha1("\0".join(map(str, parts)).encode()).hexdigest()[:20]


def _run_ffmpeg(args: list[str]) -> None:
    completed = subprocess.run(
        [str(BATCH.DEFAULT_FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "ffmpeg failed")


def _base_frame(clip: Path, position: float, width: int) -> Path:
    """One 16-bit RGB still per clip/position/width, shared by every LUT.

    The conversion to yuv444p16le then RGB is exactly what the bake's
    `format=yuv444p16le,lut3d` chain does before the LUT sees a pixel, so the
    LUT applied to this still matches the LUT applied during the render.
    """
    stat = clip.stat()
    base = PREVIEW_DIR / f"base_{_cache_key(clip, stat.st_mtime, stat.st_size, position, width)}.png"
    with _BASE_LOCK:
        if not base.is_file():
            duration = _probe_duration(clip)
            seconds = min(duration * position / 100, max(duration - 0.1, 0.0))
            _run_ffmpeg([
                "-ss", f"{seconds:.3f}", "-i", str(clip), "-frames:v", "1",
                "-vf", f"scale={width}:-2:flags=lanczos,format=yuv444p16le,format=rgb48le",
                str(base),
            ])
    return base


def render_preview(clip: str, position: float, lut: str, width: int) -> bytes:
    """JPEG of one frame of `clip`, with `lut` applied when it is not empty."""
    video = Path(clip).expanduser()
    if not video.is_file() or video.suffix.lower() not in BATCH.DEFAULT_EXTENSIONS:
        raise ValueError("Not a video clip")
    lut_path = Path(lut).expanduser() if lut else None
    if lut_path is not None and (not lut_path.is_file() or lut_path.suffix.lower() != ".cube"):
        raise ValueError("LUT must be an existing .cube file")
    if shutil.which(str(BATCH.DEFAULT_FFMPEG)) is None and not BATCH.DEFAULT_FFMPEG.is_file():
        raise RuntimeError("ffmpeg not found; brew install ffmpeg")

    width = min(PREVIEW_WIDTHS, key=lambda allowed: abs(allowed - width))
    position = min(max(position, 0.0), 100.0)
    base = _base_frame(video, position, width)
    lut_stamp = lut_path.stat().st_mtime if lut_path else ""
    out = PREVIEW_DIR / f"view_{_cache_key(base.name, lut_path or '', lut_stamp)}.jpg"
    if not out.is_file():
        chain = "format=rgb24"
        if lut_path is not None:
            chain = f"lut3d=file={BATCH._escape_filter_path(lut_path.resolve())},{chain}"
        partial = out.with_suffix(".tmp.jpg")
        _run_ffmpeg(["-i", str(base), "-vf", chain, "-q:v", "3", str(partial)])
        partial.replace(out)
    return out.read_bytes()


def runs_dir(config: dict[str, Any]) -> Path:
    """`<output>/_runs`. batch_gyroflow.py never scans its own output tree."""
    output = str(config.get("output") or "").strip()
    root = Path(output) if output else Path(str(config["source"]).strip()) / "gyroflow_stabilized"
    return root.expanduser() / RUNS_DIR


def render_script(command: list[str], log: Path, summary: str) -> str:
    return f"""#!/bin/bash
# Batch Gyroflow -- generated by gui.py at {datetime.now():%Y-%m-%d %H:%M:%S}
# {summary}
set -o pipefail

LOG={shlex.quote(str(log))}
echo "日志：${{LOG}}"
echo

{shlex.join(command)} 2>&1 | tee "${{LOG}}"
status=${{PIPESTATUS[0]}}

echo
if [ "${{status}}" -eq 0 ]; then
  echo "✅ 完成"
else
  echo "❌ 失败，退出码 ${{status}}。完整输出见：${{LOG}}"
fi
# Keep the Terminal window open so the result stays readable.
if [ -t 0 ]; then read -n 1 -s -r -p "按任意键关闭"; echo; fi
exit "${{status}}"
"""


def write_script(config: dict[str, Any]) -> Path:
    command = build_command(config)
    folder = runs_dir(config)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{'dryrun' if config.get('dry_run') else 'run'}_{stamp}"
    script = folder / f"{name}.sh"
    mode = {"stabilize": "增稳", "stabilize_lut": "增稳 + LUT", "lut_only": "只套 LUT"}.get(
        config.get("mode") or "stabilize", "增稳"
    )
    summary = f"模式：{mode}{'（试跑）' if config.get('dry_run') else ''}；素材：{str(config['source']).strip()}"
    script.write_text(render_script(command, folder / f"{name}.log", summary), encoding="utf-8")
    script.chmod(0o755)
    GENERATED.add(str(script))
    return script


def open_generated(path: str, reveal: bool) -> None:
    if path not in GENERATED:
        raise ValueError("Unknown script")
    args = ["open", "-R", path] if reveal else ["open", "-a", "Terminal", path]
    subprocess.run(args, check=True)


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
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        if route not in ("/", "/api/preview"):
            self._send(404, b"not found", "text/plain")
            return
        if not self._authorised():
            self._send(403, b"Invalid or missing token", "text/plain")
            return
        if route == "/":
            self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
            return
        # <img src> can only issue GETs, so the preview takes its options as query parameters.
        query = urllib.parse.parse_qs(parsed.query)
        arg = lambda name, default="": query.get(name, [default])[0]
        try:
            image = render_preview(
                arg("clip"), float(arg("pos", "30")), arg("lut"), int(arg("w", "360"))
            )
        except (ValueError, RuntimeError, OSError) as error:
            self._send(400, str(error).encode(), "text/plain; charset=utf-8")
            return
        self._send(200, image, "image/jpeg")

    def do_POST(self) -> None:
        route = urllib.parse.urlparse(self.path).path
        if not self._authorised():
            self._json({"error": "forbidden"}, 403)
            return
        try:
            if route == "/api/choose":
                self._json({"path": choose_path(self._body().get("kind", "folder"))})
            elif route == "/api/clips":
                self._json({"clips": list_clips(str(self._body().get("source") or ""))})
            elif route == "/api/luts":
                self._json({"luts": list_luts(str(self._body().get("folder") or ""))})
            elif route == "/api/settings":
                body = self._body()
                self._json(save_settings(body) if body else load_settings())
            elif route == "/api/script":
                script = write_script(self._body())
                self._json({"path": str(script), "command": f"bash {shlex.quote(str(script))}"})
            elif route == "/api/terminal":
                open_generated(self._body().get("path", ""), reveal=False)
                self._json({"ok": True})
            elif route == "/api/reveal":
                open_generated(self._body().get("path", ""), reveal=True)
                self._json({"ok": True})
            elif route == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"error": "not found"}, 404)
        except (ValueError, RuntimeError, KeyError, OSError, subprocess.CalledProcessError) as error:
            self._json({"error": str(error)}, 400)


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
.script{display:none;margin-top:14px}
.script code{display:block;background:var(--bg);border:1px solid var(--line);border-radius:8px;
padding:9px 11px;font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;word-break:break-all}
.script .actions{margin-top:10px}
.hint{color:var(--mut);font-size:12px;margin-top:8px}
.err{color:var(--bad);margin-top:10px;min-height:18px;font-size:13px}
.actions{display:flex;gap:9px;margin-top:16px;align-items:center}
.actions .sp{flex:1}
.preview{display:none;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
.preview .row{grid-template-columns:96px 1fr auto}
.frame{display:flex;gap:10px;align-items:center}
.frame select{flex:1;min-width:0}
.frame input[type=range]{width:180px}
.compare{position:relative;margin:4px 0 6px;border-radius:8px;overflow:hidden;background:#000;
aspect-ratio:16/9;cursor:ew-resize;user-select:none;touch-action:none}
.compare img{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;pointer-events:none}
.compare .divider{position:absolute;top:0;bottom:0;width:2px;background:#fff;opacity:.85;pointer-events:none}
.compare .tag{position:absolute;top:8px;padding:2px 7px;border-radius:5px;background:rgba(0,0,0,.55);
color:#fff;font-size:12px;pointer-events:none}
.compare .empty{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
color:#98989d;font-size:13px;text-align:center;padding:20px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:12px;
max-height:520px;overflow:auto;padding:2px}
.tile{border:2px solid transparent;border-radius:8px;cursor:pointer;background:var(--bg);overflow:hidden}
.tile:hover{border-color:var(--line)}
.tile.on{border-color:var(--accent)}
.tile img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}
.tile div{padding:4px 7px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
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
  <div class="preview" id="preview">
    <div class="row"><label>LUT 库</label><input type="text" id="lut_dir" placeholder="放 .cube 的文件夹，含子文件夹">
      <button data-pick="lut_dir" data-kind="folder">选择…</button></div>
    <div class="row"><label>样张</label><div class="frame">
      <select id="clip"><option value="">先选素材文件夹</option></select>
      <input type="range" id="pos" min="0" max="100" value="30" title="在片段中的位置"></div><span></span></div>
    <div class="compare" id="compare">
      <img id="before" alt=""><img id="after" alt="">
      <div class="divider" id="divider"></div>
      <span class="tag" style="left:8px">原片</span><span class="tag" id="afterTag" style="right:8px">LUT</span>
      <div class="empty" id="empty">选好素材文件夹和 LUT 后，这里显示套用前后的对比</div>
    </div>
    <div class="hint">在画面上拖动比较前后。点下面的缩略图直接选用该 LUT。</div>
    <div class="grid" id="grid"></div>
  </div>
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
  <div class="actions">
    <button class="primary" id="start">生成脚本</button>
    <button id="dry">生成试跑脚本</button>
    <span class="sp"></span>
    <button id="quit">退出</button>
  </div>
  <div class="err" id="err"></div>
  <div class="script" id="script">
    <code id="cmd"></code>
    <div class="actions">
      <button class="primary" id="terminal">在终端运行</button>
      <button id="reveal">在 Finder 中显示</button>
      <button id="copy">复制命令</button>
    </div>
    <div class="hint">进度和报错直接显示在终端里，完整输出同时存进脚本旁边的同名 .log。</div>
  </div>
</div>
</div><script>
const T="__TOKEN__", $=id=>document.getElementById(id);
let mode="stabilize", scriptPath="";

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
  $("preview").style.display = mode==="stabilize" ? "none" : "block";
  // Crop only exists when Gyroflow actually stabilises.
  $("cropWrap").style.opacity = mode==="lut_only" ? .45 : 1;
  $("crop").disabled = mode==="lut_only";
});

document.querySelectorAll("[data-pick]").forEach(b=>b.onclick=async()=>{
  b.disabled=true;
  try{
    const {path}=await api("/api/choose",{kind:b.dataset.kind});
    if(path){ $(b.dataset.pick).value=path; $(b.dataset.pick).dispatchEvent(new Event("change")); }
  }
  catch(e){ $("err").textContent=e.message; }
  finally{ b.disabled=false; }
});

// -- LUT preview ------------------------------------------------------------
const frameUrl=(lut,w)=>"/api/preview?"+new URLSearchParams(
  {token:T,clip:$("clip").value,pos:$("pos").value,lut:lut||"",w});
const stem=p=>p.split("/").pop().replace(/\.cube$/i,"");
const parent=p=>p.replace(/\/+$/,"").replace(/\/[^/]*$/,"");

const showCompare=()=>{
  const clip=$("clip").value, lut=$("lut").value.trim();
  $("empty").style.display = clip ? "none" : "flex";
  if(!clip){ $("before").removeAttribute("src"); $("after").removeAttribute("src"); return; }
  $("before").src=frameUrl("",1280);
  if(lut){ $("after").src=frameUrl(lut,1280); $("afterTag").textContent=stem(lut); }
  else { $("after").removeAttribute("src"); $("afterTag").textContent="未选 LUT"; }
  document.querySelectorAll(".tile").forEach(t=>t.classList.toggle("on",t.dataset.lut===lut));
};

let luts=[];
const showGrid=()=>{
  const grid=$("grid"); grid.innerHTML="";
  if(!$("clip").value) return;
  luts.forEach(lut=>{
    const tile=document.createElement("div"); tile.className="tile"; tile.dataset.lut=lut; tile.title=lut;
    const img=document.createElement("img"); img.loading="lazy"; img.src=frameUrl(lut,360);
    img.onerror=()=>{ name.textContent="⚠️ "+stem(lut)+"（无法读取）"; };
    const name=document.createElement("div"); name.textContent=stem(lut);
    tile.append(img,name);
    tile.onclick=()=>{ $("lut").value=lut; showCompare(); };
    grid.append(tile);
  });
  showCompare();
};

const loadLuts=async()=>{
  try{ luts=(await api("/api/luts",{folder:$("lut_dir").value})).luts; }
  catch(e){ luts=[]; $("err").textContent=e.message; }
  showGrid();
};

const loadClips=async()=>{
  const select=$("clip"), source=$("source").value.trim();
  let clips=[];
  try{ if(source) clips=(await api("/api/clips",{source})).clips; }
  catch(e){ $("err").textContent=e.message; }
  select.innerHTML="";
  if(!clips.length){
    select.append(new Option(source?"素材文件夹里没有视频":"先选素材文件夹",""));
  }
  clips.forEach(c=>select.append(new Option(c.startsWith(source)?c.slice(source.replace(/\/+$/,"").length+1):c,c)));
  showGrid();
};

$("source").addEventListener("change",loadClips);
$("clip").addEventListener("change",showGrid);
$("pos").addEventListener("change",showGrid);
$("lut").addEventListener("change",()=>{
  const lut=$("lut").value.trim();
  // A LUT picked on its own seeds the library with its folder.
  if(lut && !$("lut_dir").value.trim()){
    $("lut_dir").value=parent(lut); $("lut_dir").dispatchEvent(new Event("change"));
  } else showCompare();
});
$("lut_dir").addEventListener("change",()=>{
  api("/api/settings",{lut_dir:$("lut_dir").value.trim()}).catch(()=>{});
  loadLuts();
});

let split=50;
const setSplit=x=>{
  split=Math.min(100,Math.max(0,x));
  $("after").style.clipPath=`inset(0 0 0 ${split}%)`;
  $("divider").style.left=`calc(${split}% - 1px)`;
};
setSplit(50);
$("compare").addEventListener("pointerdown",e=>{
  const box=$("compare"), move=ev=>{const r=box.getBoundingClientRect(); setSplit((ev.clientX-r.left)/r.width*100);};
  box.setPointerCapture(e.pointerId); move(e);
  box.onpointermove=move;
  box.onpointerup=()=>{ box.onpointermove=null; };
});

api("/api/settings").then(s=>{ if(s.lut_dir){ $("lut_dir").value=s.lut_dir; loadLuts(); } }).catch(()=>{});

const val=id=>{const el=$(id); return el.type==="checkbox"?el.checked:el.value.trim();};
const config=dry=>({mode,dry_run:dry,
  source:val("source"),output:val("output"),lut:val("lut"),
  crop:$("crop").disabled?"":val("crop"), zoom_window:val("zoom_window"),
  date_position:val("date_position"), codec:val("codec"), bitrate:val("bitrate"),
  jobs:val("jobs"), timeout:val("timeout"), suffix:val("suffix"),
  readout_time:val("readout_time"), readout_direction:val("readout_direction"),
  overwrite:val("overwrite"), keep_intermediate:val("keep_intermediate"),
  no_recursive:val("no_recursive"), require_gyro:val("require_gyro")});

const generate=async dry=>{ $("err").textContent="";
  try{
    const d=await api("/api/script",config(dry));
    scriptPath=d.path; $("cmd").textContent=d.command; $("script").style.display="block";
  }catch(e){ $("err").textContent=e.message; } };
$("start").onclick=()=>generate(false);
$("dry").onclick=()=>generate(true);
$("terminal").onclick=()=>api("/api/terminal",{path:scriptPath}).catch(e=>$("err").textContent=e.message);
$("reveal").onclick=()=>api("/api/reveal",{path:scriptPath}).catch(e=>$("err").textContent=e.message);
$("copy").onclick=async()=>{ await navigator.clipboard.writeText($("cmd").textContent);
  $("copy").textContent="已复制"; setTimeout(()=>$("copy").textContent="复制命令",1500); };
$("quit").onclick=async()=>{ await api("/api/quit").catch(()=>{}); document.body.innerHTML=
  '<div class="wrap"><h1>已退出</h1><p style="color:var(--mut)">可以关闭这个标签页了。</p></div>'; };

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
        server.server_close()
        shutil.rmtree(PREVIEW_DIR, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
