from __future__ import annotations

import argparse
import curses
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .display import paint, init_colors, render_rows, clip
from .sources import Collector, atomic_json, read_json

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "codex-hud"


def tmux(socket, *args, check=True):
    return subprocess.run(["tmux", "-L", socket, *map(str, args)], check=check,
                          capture_output=True, text=True, timeout=5)


def command(*args):
    return shlex.join([sys.executable, str(ENTRY), *map(str, args)])


def launch(argv):
    parser = argparse.ArgumentParser(prog="codex-hud", description="官方 Codex CLI + 独立 tmux 固定底部 HUD。CLI 参数可直接传入，或放在 -- 后。")
    parser.add_argument("--detach", action="store_true", help="后台启动，打印附加命令")
    parser.add_argument("--hud-height", type=int, default=11, help="底部最大行数，默认 11；按内容收缩，Ctrl-b H 可展开")
    parser.add_argument("--width", type=int, default=140, help="后台启动时的终端列数")
    parser.add_argument("--height", type=int, default=40, help="后台启动时的终端行数")
    # parse only HUD options; all other tokens remain official CLI arguments.
    if "--" in argv:
        split = argv.index("--")
        options, extra = parser.parse_known_args(argv[:split])
        cli_args = extra + argv[split + 1:]
    else:
        options, cli_args = parser.parse_known_args(argv)
    if not shutil.which("tmux"):
        parser.error("缺少 tmux，请运行 brew install tmux")
    from .integration import resolve_codex
    try:
        executable = resolve_codex()
    except RuntimeError as error:
        parser.error(str(error))
    if not options.detach and not sys.stdin.isatty():
        parser.error("请在终端中运行；自动化测试可加 --detach")
    if not 6 <= options.hud_height <= 30 or options.width < 40 or options.height < 16:
        parser.error("--hud-height 需为 6–30；终端尺寸至少 40×16")
    runtime = Path(tempfile.mkdtemp(prefix="codex-hud-", dir=os.environ.get("CODEX_HUD_RUNTIME_DIR")))
    runtime.chmod(0o700)
    socket = runtime.name
    home = str(Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve())
    # Only this invocation is standalone. Official binary, auth and config stay intact.
    log_dir = runtime / "diagnostics"
    log_dir.mkdir(mode=0o700)
    arguments = [executable, "--no-daemon", "-c", f"log_dir={json.dumps(str(log_dir))}", *cli_args]
    spec = {"socket": socket, "runtime": str(runtime), "home": home,
            "cwd": os.getcwd(), "argv": arguments, "hud_height": options.hud_height,
            "diagnostic": str(log_dir / "codex-tui.log")}
    atomic_json(runtime / "spec.json", spec)
    attached = False
    watcher = None
    try:
        created = tmux(socket, "-f", "/dev/null", "new-session", "-d", "-s", "codex",
                       "-x", options.width, "-y", options.height, "-c", os.getcwd(),
                       "-P", "-F", "#{pane_id}", command("_run", runtime))
        spec["top"] = created.stdout.strip()
        tmux(socket, "set-option", "-g", "status", "off")
        tmux(socket, "set-option", "-g", "mouse", "on")
        tmux(socket, "set-option", "-g", "exit-empty", "on")
        tmux(socket, "set-option", "-g", "remain-on-exit", "on")
        tmux(socket, "set-option", "-g", "set-clipboard", "on")
        tmux(socket, "set-option", "-g", "default-terminal", "tmux-256color")
        tmux(socket, "set-option", "-g", "pane-border-style", "fg=colour238")
        tmux(socket, "set-option", "-g", "pane-active-border-style", "fg=colour238")
        # Start the observer off-screen. It joins the CLI only when it has data.
        split = tmux(socket, "new-window", "-d", "-t", "codex", "-n", "_hud",
                     "-c", os.getcwd(), "-P", "-F", "#{pane_id}", command("_panel", runtime))
        spec["hud"] = split.stdout.strip()
        atomic_json(runtime / "spec.json", spec)
        tmux(socket, "bind-key", "H", "run-shell", command("_toggle", runtime))
        tmux(socket, "bind-key", "C", "display-popup", "-E", "-w", "85%", "-h", "85%", command("config", "--menu"))
        tmux(socket, "select-pane", "-t", spec["top"])
        watcher = subprocess.Popen([sys.executable, str(ENTRY), "_watch", str(runtime)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        atomic_json(runtime / "ready.json", {"ready": True})
        # Wait until the panel owns cleanup before returning a detached handle.
        for _ in range(60):
            if (runtime / "panel.json").exists() or (runtime / "exit.json").exists():
                break
            time.sleep(0.05)
        if options.detach:
            print(json.dumps({**spec, "attach": shlex.join(["tmux", "-L", socket, "attach-session", "-t", "codex"])}, ensure_ascii=False))
            return 0
        attached = True
        # Nested tmux users attach through a fresh client without modifying their server.
        env = dict(os.environ)
        env.pop("TMUX", None)
        if env.get("TERM", "dumb") in ("", "dumb"):
            env["TERM"] = "xterm-256color"
        result = subprocess.run(["tmux", "-L", socket, "attach-session", "-t", "codex"], env=env)
        outcome = read_json(runtime / "exit.json", {})
        if outcome:
            return outcome.get("code", result.returncode)
        print(f"HUD 已分离，重新进入：tmux -L {socket} attach-session -t codex")
        return result.returncode
    except (OSError, subprocess.SubprocessError) as error:
        tmux(socket, "kill-server", check=False)
        shutil.rmtree(runtime, ignore_errors=True)
        print(f"HUD 启动失败：{error}", file=sys.stderr)
        return 1
    finally:
        # Ctrl-b d intentionally leaves the owned session running for reattachment.
        if attached and (runtime / "exit.json").exists():
            tmux(socket, "kill-server", check=False)
            shutil.rmtree(runtime, ignore_errors=True)


def runner(runtime):
    runtime = Path(runtime)
    spec = read_json(runtime / "spec.json")
    # Do not let fast CLI commands close the only pane before setup finishes.
    deadline = time.monotonic() + 15
    while not (runtime / "ready.json").exists():
        if time.monotonic() >= deadline:
            atomic_json(runtime / "exit.json", {"code": 1, "time": time.time()})
            return 1
        time.sleep(0.05)
    env = dict(os.environ)
    # Never accidentally claim the launching Codex chat's thread.
    for key in ("CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        env.pop(key, None)
    env["CODEX_HUD_ACTIVE"] = "1"
    env["CODEX_HUD_RUNTIME"] = str(runtime)
    # This exact official span identifies session creation before any prompt.
    env["RUST_LOG"] = env.get("RUST_LOG", "warn") + ",codex_core::session=info"
    child = None
    code = 1
    def stop(signum, frame):
        # The runner and its CLI share this pane's process group. tmux's pane
        # teardown also sends HUP; forward it to the direct child promptly.
        if child and child.poll() is None:
            child.send_signal(signum)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, start_new_session=False)
        atomic_json(runtime / "runner.json", {"pid": child.pid, "started": time.time()})
        code = child.wait()
    except KeyboardInterrupt:
        code = 130
    except OSError as error:
        print(f"官方 CLI 启动失败: {error}", file=sys.stderr)
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        atomic_json(runtime / "exit.json", {"code": code, "time": time.time()})
    return code


def toggle(runtime):
    spec = read_json(Path(runtime) / "spec.json")
    snapshot = read_json(Path(runtime) / "snapshot.json")
    if not snapshot or not render_rows(snapshot):
        return 0
    sync_layout(spec, snapshot)
    zoomed = tmux(spec["socket"], "display-message", "-p", "-t", spec["top"], "#{window_zoomed_flag}").stdout.strip() == "1"
    if zoomed:
        tmux(spec["socket"], "resize-pane", "-Z")
        tmux(spec["socket"], "select-pane", "-t", spec["top"])
    else:
        tmux(spec["socket"], "select-pane", "-t", spec["hud"])
        tmux(spec["socket"], "resize-pane", "-Z")
    return 0


def sync_layout(spec, snapshot):
    """Keep empty HUDs off-screen and size visible panels to their content."""
    if not spec.get("hud"):
        return False
    socket = spec["socket"]
    main = tmux(socket, "display-message", "-p", "-t", spec["top"],
                "#{window_id} #{window_height} #{window_width} #{window_zoomed_flag}", check=False).stdout.split()
    panel_info = tmux(socket, "display-message", "-p", "-t", spec["hud"],
                      "#{window_id} #{pane_height}", check=False).stdout.split()
    if len(main) != 4 or len(panel_info) != 2:
        return False
    rows = render_rows(snapshot, int(main[2]))
    docked = main[0] == panel_info[0]
    if not rows:
        if docked:
            if main[3] == "1":
                tmux(socket, "resize-pane", "-t", spec["top"], "-Z", check=False)
            tmux(socket, "break-pane", "-d", "-s", spec["hud"], "-n", "_hud", check=False)
            tmux(socket, "select-pane", "-t", spec["top"], check=False)
        return False
    desired = min(snapshot.get("display", {}).get("height", spec["hud_height"]), max(1, int(main[1]) // 2), len(rows))
    if not docked:
        tmux(socket, "join-pane", "-d", "-v", "-l", desired, "-s", spec["hud"], "-t", spec["top"], check=False)
    elif main[3] == "0" and int(panel_info[1]) != desired:
        tmux(socket, "resize-pane", "-t", spec["hud"], "-y", desired, check=False)
    return docked and main[3] == "1"


def panel(runtime):
    runtime = Path(runtime)
    atomic_json(runtime / "panel.json", {"pid": os.getpid()})
    spec = read_json(runtime / "spec.json")
    collector = Collector(home=spec["home"], diagnostic=spec.get("diagnostic"))
    from .config import load
    def ui(screen):
        nonlocal spec
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.nodelay(True)
        screen.keypad(True)
        config, config_error = load({"height": spec["hud_height"]})
        init_colors(config)
        scroll, refresh, snapshot, expanded = 0, 0, None, False
        while True:
            if (runtime / "exit.json").exists():
                break
            key = screen.getch()
            if key in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif key in (curses.KEY_HOME, ord("g")):
                scroll = 0
            elif key in (curses.KEY_END, ord("G")):
                scroll = 10**6
            elif key == 27:
                spec = read_json(runtime / "spec.json", spec)
                zoomed = tmux(spec["socket"], "display-message", "-p", "#{window_zoomed_flag}").stdout.strip()
                if zoomed == "1":
                    tmux(spec["socket"], "resize-pane", "-Z")
                tmux(spec["socket"], "select-pane", "-t", spec["top"])
            if time.monotonic() - refresh >= 1 or snapshot is None:
                spec = read_json(runtime / "spec.json", spec)
                runner_info = read_json(runtime / "runner.json", {})
                collector.runner = runner_info.get("pid")
                collector.launched_at = runner_info.get("started")
                new_config, config_error = load({"height": spec["hud_height"]})
                if new_config != config:
                    config = new_config
                    init_colors(config)
                snapshot = collector.collect(config)
                snapshot["display"] = config
                if config_error:
                    snapshot["errors"].append(config_error)
                if not snapshot["root"].get("cwd"):
                    snapshot["root"]["cwd"] = spec["cwd"]
                atomic_json(runtime / "snapshot.json", snapshot)
                expanded = sync_layout(spec, snapshot)
                refresh = time.monotonic()
            scroll = paint(screen, snapshot, scroll, expanded)
            time.sleep(0.1)
    try:
        curses.wrapper(ui)
    finally:
        # Exiting the official CLI closes the entire private tmux server.
        # A panel crash must not kill a still-running official CLI.
        if not (runtime / "exit.json").exists():
            atomic_json(runtime / "panel-error.json", {"message": "HUD 面板异常退出，CLI 仍可继续使用"})
    return 0


def watch(runtime):
    """Cleanup outside tmux, so killing panes cannot interrupt cleanup itself."""
    runtime = Path(runtime)
    spec = read_json(runtime / "spec.json")
    while runtime.exists():
        if (runtime / "exit.json").exists():
            tmux(spec["socket"], "kill-server", check=False)
            time.sleep(0.5)
            shutil.rmtree(runtime, ignore_errors=True)
            break
        if tmux(spec["socket"], "has-session", "-t", "codex", check=False).returncode:
            shutil.rmtree(runtime, ignore_errors=True)
            break
        dead = tmux(spec["socket"], "display-message", "-p", "-t", spec["top"], "#{pane_dead}", check=False)
        if dead.stdout.strip() == "1":
            atomic_json(runtime / "exit.json", {"code": 1, "time": time.time(), "error": "CLI runner exited"})
        time.sleep(0.5)
    return 0


def observe(argv):
    """Read-only, pinned-session HUD for a terminal pane outside the launcher."""
    parser = argparse.ArgumentParser(prog="codex-hud watch", description="只读监视已有会话，不启动或恢复 Codex")
    parser.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID"), help="固定监视的会话 ID")
    parser.add_argument("--home", help="该会话所在的 Codex 状态目录")
    parser.add_argument("--once", action="store_true", help="输出一次纯文本，便于核对数据来源")
    args = parser.parse_args(argv)
    if not args.session:
        parser.error("需要 --session 会话ID")
    collector = Collector(home=args.home, root_id=args.session)
    from .config import load
    def collect():
        config, error = load()
        snapshot = collector.collect(config)
        snapshot["display"] = config
        if error:
            snapshot["errors"].append(error)
        return snapshot
    if args.once:
        print(f"只读监视会话 {args.session}")
        for row in render_rows(collect()):
            print(row.text)
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        parser.error("请在终端分屏中运行；数据核对可用 --once")
    def ui(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.nodelay(True)
        screen.keypad(True)
        init_colors()
        scroll, refreshed, snapshot = 0, 0, None
        while True:
            key = screen.getch()
            if key in (ord('q'), 27, 3):
                return
            if key in (curses.KEY_DOWN, ord('j')):
                scroll += 1
            elif key in (curses.KEY_UP, ord('k')):
                scroll = max(0, scroll - 1)
            if snapshot is None or time.monotonic() - refreshed >= 1:
                snapshot, refreshed = collect(), time.monotonic()
                init_colors(snapshot["display"])
            scroll = paint(screen, snapshot, scroll, expanded=True)
            height, width = screen.getmaxyx()
            try:
                screen.move(height - 1, 0)
                screen.clrtoeol()
                screen.addstr(height - 1, 0, clip(f" 只读监视 {args.session[:8]} · ↑↓ 滚动 · q 退出面板", width - 1), curses.color_pair(5))
                screen.refresh()
            except curses.error:
                pass
            time.sleep(.1)
    try:
        curses.wrapper(ui)
    except KeyboardInterrupt:
        pass
    return 0


def refresh_panels(argv):
    parser = argparse.ArgumentParser(prog="codex-hud refresh", description="只更新当前会话的 HUD 进程，保留正在运行的 Codex")
    parser.add_argument("--session", default=os.environ.get("CODEX_THREAD_ID"))
    args = parser.parse_args(argv)
    if not args.session:
        parser.error("需要 --session 会话 ID")
    updated = []
    base = Path(os.environ.get("CODEX_HUD_RUNTIME_DIR", tempfile.gettempdir()))
    for runtime in base.glob("codex-hud-*"):
        snapshot = read_json(runtime / "snapshot.json", {})
        if (snapshot.get("root") or {}).get("id") != args.session:
            continue
        spec = read_json(runtime / "spec.json", {})
        if spec.get("socket") != runtime.name or not spec.get("hud") or not spec.get("top"):
            continue
        socket = spec["socket"]
        before = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout.strip()
        tmux(socket, "respawn-pane", "-k", "-t", spec["hud"], command("_panel", runtime))
        tmux(socket, "bind-key", "H", "run-shell", command("_toggle", runtime))
        tmux(socket, "bind-key", "C", "display-popup", "-E", "-w", "85%", "-h", "85%", command("config", "--menu"))
        after = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout.strip()
        if before != after:
            raise RuntimeError("CLI pane identity changed while refreshing HUD")
        updated.append({"runtime": str(runtime), "cli_pid": before})
    print(json.dumps({"updated": updated}, ensure_ascii=False))
    return 0 if updated else 1


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "watch":
        return observe(argv[1:])
    if argv and argv[0] == "config":
        from .config import configure
        return configure(argv[1:])
    if argv and argv[0] == "refresh":
        return refresh_panels(argv[1:])
    if argv and argv[0] in ("setup", "configure"):
        from .integration import configure
        return configure(argv[1:])
    if argv and argv[0] in ("_run", "_panel", "_toggle", "_watch"):
        return {"_run": runner, "_panel": panel, "_toggle": toggle, "_watch": watch}[argv[0]](argv[1])
    if argv and argv[0] == "inspect":
        parser = argparse.ArgumentParser(prog="codex-hud inspect", description="只读输出指定会话的 HUD 数据")
        parser.add_argument("--session", required=True)
        parser.add_argument("--home")
        args = parser.parse_args(argv[1:])
        print(json.dumps(Collector(home=args.home, root_id=args.session).collect(), ensure_ascii=False, indent=2))
        return 0
    return launch(argv)
