"""Native terminal launcher and read-only JSONL data bridge (no tmux)."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .sources import Collector, atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]


def launch(argv, height=11, detach=False):
    from .integration import resolve_codex
    real = resolve_codex()
    if detach:
        print("原生终端后端需要交互终端，不支持 tmux 的 --detach", file=sys.stderr)
        return 2
    node = shutil.which("node")
    settings_home = Path(os.environ.get("CODEX_HUD_CONFIG_HOME", Path.home()/".config/codex-hud"))
    settings = read_json(settings_home/"settings.json", {})
    prefix = os.environ.get("CODEX_HUD_NODE_HOME") or settings.get("native_node") or str(settings_home/"native_node")
    env = dict(os.environ, CODEX_HUD_NODE_HOME=prefix, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    ready = False
    if node:
        try:
            ready = subprocess.run([node, "-e", "require(process.argv[1]).loadDependencies()", str(ROOT/"native/terminal.cjs")],
                                   env=env, capture_output=True, timeout=10).returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    if not ready:
        print("codex-hud: 原生终端依赖未就绪，先使用官方 CLI；请通过 hud skill 重新开启", file=sys.stderr)
        if os.name == "nt":
            return subprocess.call([real, *argv])
        os.execv(real, [real, *argv])
    runtime = Path(tempfile.mkdtemp(prefix="codex-hud-native-", dir=os.environ.get("CODEX_HUD_RUNTIME_DIR")))
    runtime.chmod(0o700)
    diagnostic = runtime/"diagnostics"
    diagnostic.mkdir(mode=0o700)
    spec = {"runtime": str(runtime), "home": str(Path(os.environ.get("CODEX_HOME", Path.home()/".codex")).resolve()),
            "cwd": os.getcwd(), "hud_height": height,
            "argv": [real, "--no-daemon", "-c", f"log_dir={json.dumps(str(diagnostic))}", *argv],
            "diagnostic": str(diagnostic/"codex-tui.log"),
            "bridge_argv": [sys.executable, str(ROOT/"codex-hud"), "_native_bridge", str(runtime)]}
    atomic_json(runtime/"spec.json", spec)
    host = None
    try:
        host = subprocess.Popen([node, str(ROOT/"native/terminal.cjs"), str(runtime)], env=env)
        return host.wait()
    except KeyboardInterrupt:
        return 130
    finally:
        if host and host.poll() is None:
            host.terminate()
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait()
        shutil.rmtree(runtime, ignore_errors=True)


class Bridge:
    def __init__(self, runtime):
        self.runtime = Path(runtime)
        self.spec = read_json(self.runtime/"spec.json", {})
        self.collector = Collector(home=self.spec["home"], diagnostic=self.spec["diagnostic"])
        self.width = 100
        self.snapshot = {}

    def collect(self):
        from .config import load
        from .display import render_rows
        cfg, error = load({"height": self.spec.get("hud_height", 11)})
        runner = read_json(self.runtime/"runner.json", {})
        self.collector.launched_at = runner.get("started")
        snapshot = self.collector.collect(cfg)
        snapshot["display"] = cfg
        if not snapshot["root"].get("cwd"):
            snapshot["root"]["cwd"] = self.spec["cwd"]
        if error:
            snapshot["errors"].append(error)
        self.snapshot = snapshot
        atomic_json(self.runtime/"snapshot.json", snapshot)
        def rows(config):
            return [{"spans": r.spans, "pinned": r.pinned, "key": r.key} for r in render_rows(snapshot, self.width, config)]
        full = {**cfg, "agents_limit": 0}
        history = {**full, "agents_show_completed": True, "agents_show_failed": True}
        return {"type": "snapshot", "snapshot": snapshot, "rows": rows(cfg), "all_rows": rows(full), "history_rows": rows(history)}

    def request(self, data):
        from .agents import details, entries
        from .config import preset, save
        kind = data.get("type")
        result = {"type": kind, "request_id": data.get("request_id")}
        if kind == "size":
            value = data.get("cols")
            if type(value) is int and 20 <= value <= 1000:
                self.width = value
            return None
        try:
            if kind == "action":
                from .management import request_text
                result.update(root=data.get("root"), key=data.get("key"))
                # Explicit requests rebind immediately rather than waiting for
                # the normal display polling interval after /new.
                self.collector.last_bind = 0
                snapshot = self.collect()["snapshot"]
                result["draft"] = request_text(snapshot, data.get("root"), data.get("key") or [], data.get("action"), data.get("stamp"))
            elif kind == "detail":
                root = self.snapshot.get("root", {}).get("id")
                key = tuple(data.get("key") or [])
                result.update(root=data.get("root"), key=key)
                item = next((x for x in entries(self.snapshot) if x["key"] == key), None)
                if not root or data.get("root") != root or item is None:
                    raise ValueError("所选 agent 已不属于当前会话 / selection is no longer in this conversation")
                result.update(root=root, key=key, lines=details(self.spec["home"], item, self.snapshot["time"], self.snapshot["display"]["language"] == "en"))
            elif kind == "config":
                changes = deepcopy(data.get("changes"))
                if not isinstance(changes, dict):
                    raise ValueError("changes must be an object")
                name = changes.pop("preset", None)
                if name is not None:
                    if name not in ("minimal", "standard", "full", "focus"):
                        raise ValueError("unknown preset")
                    changes = {**preset(name), **changes}
                save(changes)
            else:
                raise ValueError("unknown bridge request")
        except (OSError, ValueError, TypeError) as error:
            result["error"] = str(error)
        return result


def bridge(runtime):
    state = Bridge(runtime)
    incoming = queue.Queue()
    def reader():
        for line in sys.stdin:
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    incoming.put(value)
            except ValueError:
                continue
        incoming.put({"type": "quit"})
    threading.Thread(target=reader, daemon=True).start()
    def emit(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    refreshed = 0
    while state.runtime.exists():
        if time.monotonic()-refreshed >= 1:
            emit(state.collect())
            refreshed = time.monotonic()
        try:
            value = incoming.get(timeout=.1)
        except queue.Empty:
            continue
        if value.get("type") == "quit":
            return 0
        reply = state.request(value)
        if reply is not None:
            emit(reply)
    return 0
