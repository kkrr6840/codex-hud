from __future__ import annotations

import argparse
from contextlib import contextmanager
try:
    import curses
except ImportError:  # Native Windows renders through ConPTY, not curses.
    curses = None
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from .display import paint, init_colors, render_rows, clip
from .sources import Collector, atomic_json, read_json
from . import __version__

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "codex-hud"


def tmux(socket, *args, check=True):
    return subprocess.run(["tmux", "-L", socket, *map(str, args)], check=check,
                          capture_output=True, text=True, timeout=5)


def runtime_entry(runtime):
    """Keep this session's observer and key helpers outside disposable caches."""
    runtime = Path(runtime)
    target = runtime / ("code-" + __version__)
    if not (target / "codex-hud").is_file():
        staging = Path(tempfile.mkdtemp(prefix=".code-", dir=runtime))
        try:
            package = staging / "codex_hud"
            package.mkdir()
            for source in (ROOT / "codex_hud").glob("*.py"):
                shutil.copy2(source, package / source.name)
            shutil.copy2(ENTRY, staging / "codex-hud")
            try:
                staging.rename(target)
            except OSError:
                if not (target / "codex-hud").is_file():
                    raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    return target / "codex-hud"


def command(*args, runtime=None):
    entry = runtime_entry(runtime) if runtime is not None else ENTRY
    return shlex.join([sys.executable, str(entry), *map(str, args)])


def configure_terminal(spec):
    """Keep frame/cursor changes together, including an outer tmux terminal."""
    socket = spec["socket"]
    # Explicitly request a steady cursor rather than a terminal-dependent
    # default. This affects only the CLI pane in this private HUD server.
    tmux(socket, "set-option", "-p", "-t", spec["top"], "cursor-style", "block")
    features = tmux(socket, "show-options", "-s", "terminal-features").stdout
    if "tmux*:sync" not in features:
        indices = [int(i) for i in re.findall(r"terminal-features\[(\d+)\]", features)]
        tmux(socket, "set-option", "-s", f"terminal-features[{max(indices, default=-1) + 1}]", "tmux*:sync")


def attach_client_command(socket, session, flags, sync=True):
    argv = [shutil.which("tmux"), "-L", socket]
    if sync:
        argv += ["-T", "sync"]
    argv += ["attach-session", "-t", session]
    saved = [flag for flag in flags.split(",") if flag in ("active-pane", "ignore-size", "no-output", "wait-exit") or re.fullmatch(r"pause-after=\d+", flag)]
    if saved:
        argv += ["-f", ",".join(saved)]
    return "exec " + shlex.join(argv)


def configure_lifetime(spec):
    """Arm terminal-owned sessions only after a client actually attaches.

    Setup itself creates a detached session. Explicit --detach launches retain
    their background lifetime; older interactive runtimes adopt the new policy.
    tmux owns disconnect handling, so even a killed launcher cannot orphan it.
    """
    if not spec.get("close_on_disconnect", True):
        return
    socket = spec["socket"]
    tmux(socket, "set-hook", "-t", "codex", "client-attached[90]",
         "set-option -t codex destroy-unattached on")
    tmux(socket, "if-shell", "-F", "-t", "codex", "#{session_attached}",
         "set-option -t codex destroy-unattached on")


@contextmanager
def preserve_display_reconnect(socket):
    """A capability refresh briefly detaches the only display client."""
    closing = tmux(socket, "show-options", "-A", "-v", "-t", "codex", "destroy-unattached").stdout.strip() == "on"
    if closing:
        tmux(socket, "set-option", "-t", "codex", "destroy-unattached", "off")
    try:
        yield
    finally:
        if closing:
            tmux(socket, "set-option", "-t", "codex", "destroy-unattached", "on", check=False)


def unwrap_owned_parent(spec):
    """Connect a visible parent HUD client directly to this HUD's session.

    Only a private HUD created by this plugin is eligible. User tmux servers,
    other windows, read-only/control clients and all CLI processes are kept.
    """
    socket = spec["socket"]
    def environment(key):
        value = tmux(socket, "show-environment", "-g", key, check=False).stdout.strip()
        return value[len(key)+1:] if value.startswith(key + "=") else ""
    parent_name = Path(environment("TMUX").split(",", 1)[0]).name
    pane = environment("TMUX_PANE")
    if not parent_name.startswith("codex-hud-") or parent_name == socket or not re.fullmatch(r"%\d+", pane):
        return []
    parent_dir = Path(spec["runtime"]).parent / parent_name
    parent = read_json(parent_dir / "spec.json", {})
    if parent.get("socket") != parent_name or parent.get("runtime") != str(parent_dir) or parent.get("home") != spec["home"] or parent_dir.stat().st_uid != os.getuid():
        return []
    # Removing the last display would close a terminal-owned parent and its
    # nested client. Only persistent parents can safely shed this display layer.
    if tmux(parent_name, "show-options", "-A", "-v", "-t", "codex", "destroy-unattached", check=False).stdout.strip() == "on":
        return []
    parent_tty = tmux(parent_name, "display-message", "-p", "-t", pane, "#{pane_tty}", check=False).stdout.strip()
    if not parent_tty.startswith("/dev/"):
        return []
    fields = "#{client_name}\t#{client_termfeatures}\t#{client_flags}\t#{client_readonly}\t#{client_control_mode}\t#{pane_id}"
    def clients(name):
        return [line.split("\t") for line in tmux(name, "list-clients", "-F", fields).stdout.splitlines() if len(line.split("\t")) == 6]
    nested = [row for row in clients(socket) if row[0] == parent_tty and row[3:5] == ["0", "0"] and "suspended" not in row[2].split(",")]
    if not nested:
        return []
    session = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{session_name}").stdout.strip()
    updated = []
    for name, features, flags, readonly, control, active in clients(parent_name):
        if active != pane or readonly != "0" or control != "0" or "suspended" in flags.split(","):
            continue
        command = attach_client_command(socket, session, flags, "sync" in features.split(","))
        tmux(parent_name, "detach-client", "-t", name, "-E", command)
        for _ in range(40):
            if any(row[0] == name for row in clients(socket)):
                updated.append(name)
                break
            time.sleep(.05)
        else:
            raise RuntimeError("直接显示连接未恢复；两个 HUD 的 CLI 和会话仍保留")
    if updated:
        # The replacement is attached before the redundant nested client leaves.
        for row in nested:
            tmux(socket, "detach-client", "-t", row[0])
    return updated


def reconnect_unsynced_clients(spec):
    """Apply newly configured capabilities to old nested terminal clients.

    tmux caches these when a client attaches. Replacing only that client keeps
    the CLI PTY, draft, processes and session alive; no keys are injected.
    """
    socket = spec["socket"]
    fields = "#{client_name}\t#{client_termname}\t#{client_termfeatures}\t#{client_session}\t#{client_readonly}\t#{client_control_mode}\t#{client_flags}"
    clients = tmux(socket, "list-clients", "-F", fields).stdout.splitlines()
    updated = []
    for line in clients:
        values = line.split("\t")
        if len(values) != 7:
            continue
        name, terminal, features, session, readonly, control, flags = values
        if not terminal.startswith("tmux") or "sync" in features.split(",") or readonly != "0" or control != "0" or not session or "suspended" in flags.split(","):
            continue
        with preserve_display_reconnect(socket):
            tmux(socket, "detach-client", "-t", name, "-E", attach_client_command(socket, session, flags))
            for _ in range(40):
                current = tmux(socket, "list-clients", "-F", "#{client_name}\t#{client_termfeatures}").stdout.splitlines()
                if any(row.split("\t", 1)[0] == name and "sync" in row.split("\t", 1)[-1].split(",") for row in current):
                    updated.append(name)
                    break
                time.sleep(.05)
            else:
                raise RuntimeError("终端显示连接未恢复")
    return updated


def bind_settings(spec):
    popup = ("display-popup", "-E", "-w", "85%", "-h", "85%", command("config", "--menu", runtime=spec["runtime"]))
    tmux(spec["socket"], "bind-key", "C", *popup)
    bindings = tmux(spec["socket"], "list-keys", "-T", "root").stdout.splitlines()
    existing = "\n".join(line for line in bindings if re.search(r"(?:^|\s)-T\s+root\s+F10(?:\s|$)", line))
    if existing and not (spec["runtime"] in existing and "config --menu" in existing):
        return False
    tmux(spec["socket"], "bind-key", "-n", "F10", *popup)
    return True


def bind_agents(spec):
    view = ("run-shell", command("_view", spec["runtime"], runtime=spec["runtime"]))
    tmux(spec["socket"], "bind-key", "Down", *view)
    tmux(spec["socket"], "bind-key", "-n", "M-Down", *view)
    tmux(spec["socket"], "bind-key", "-n", "Down", "run-shell", command("_down", spec["runtime"], "#{pane_id}", runtime=spec["runtime"]))
    active = "#{&&:#{==:#{@hud-nav},1},#{||:#{==:#{pane_id}," + spec["hud"] + "},#{&&:#{==:#{pane_id}," + spec["top"] + "},#{==:#{pane_active},0}}}}"
    for key in ("Up", "Enter", "Escape", "C-c"):
        tmux(spec["socket"], "bind-key", "-n", key, "if-shell", "-F", active,
             shlex.join(["send-keys", "-t", spec["hud"], "Escape" if key == "C-c" else key]), shlex.join(["send-keys", key]))


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
    if os.name == "nt" or os.environ.get("CODEX_HUD_NATIVE_BACKEND") == "1":
        from .native import launch as native_launch
        return native_launch(cli_args, options.hud_height, options.detach)
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
            "close_on_disconnect": not options.detach,
            "diagnostic": str(log_dir / "codex-tui.log")}
    atomic_json(runtime / "spec.json", spec)
    attached = False
    watcher = None
    previous_signals = {}
    if not options.detach:
        def terminal_closed(signum, frame):
            raise SystemExit(128 + signum)
        for signum in (signal.SIGHUP, signal.SIGTERM):
            previous_signals[signum] = signal.signal(signum, terminal_closed)
    try:
        created = tmux(socket, "-f", "/dev/null", "new-session", "-d", "-s", "codex",
                       "-x", options.width, "-y", options.height, "-c", os.getcwd(),
                       "-P", "-F", "#{pane_id}", command("_run", runtime, runtime=runtime))
        spec["top"] = created.stdout.strip()
        configure_terminal(spec)
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
                     "-c", os.getcwd(), "-P", "-F", "#{pane_id}", command("_panel", runtime, runtime=runtime))
        spec["hud"] = split.stdout.strip()
        atomic_json(runtime / "spec.json", spec)
        tmux(socket, "bind-key", "H", "run-shell", command("_toggle", runtime, runtime=runtime))
        bind_settings(spec)
        bind_agents(spec)
        configure_lifetime(spec)
        tmux(socket, "select-pane", "-t", spec["top"])
        watcher = subprocess.Popen([sys.executable, str(runtime_entry(runtime)), "_watch", str(runtime)],
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
        return result.returncode
    except (OSError, subprocess.SubprocessError) as error:
        tmux(socket, "kill-server", check=False)
        shutil.rmtree(runtime, ignore_errors=True)
        print(f"HUD 启动失败：{error}", file=sys.stderr)
        return 1
    finally:
        # Cover terminal closure during setup too, before the attach hook arms.
        cleanup = attached and (runtime / "exit.json").exists()
        if not options.detach and (not attached or not tmux(socket, "list-clients", check=False).stdout.strip()):
            cleanup = True
        if cleanup:
            tmux(socket, "kill-server", check=False)
            shutil.rmtree(runtime, ignore_errors=True)
        for signum, handler in previous_signals.items():
            signal.signal(signum, handler)


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
    stopped = None
    class PaneClosed(Exception):
        pass
    def stop(signum, frame):
        nonlocal stopped
        if stopped is None:
            stopped = signum
            if child and child.poll() is None:
                child.send_signal(signum)
            # Leave wait() even if the CLI ignores HUP, so shutdown is bounded.
            raise PaneClosed
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        child = subprocess.Popen(spec["argv"], cwd=spec["cwd"], env=env, start_new_session=False)
        atomic_json(runtime / "runner.json", {"pid": child.pid, "started": time.time()})
        code = child.wait()
    except PaneClosed:
        code = 128 + stopped
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
        if stopped and os.getpgrp() == os.getpid():
            # The private pane owns this process group. tmux HUP alone can
            # leave ordinary tool children orphaned after their CLI exits.
            os.killpg(os.getpgrp(), signal.SIGTERM)
        try:
            atomic_json(runtime / "exit.json", {"code": code, "time": time.time()})
        except FileNotFoundError:
            if not stopped:
                raise
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
    from .viewer import layout_lock
    with layout_lock(spec["runtime"]):
        return _sync_layout(spec, snapshot)


def _sync_layout(spec, snapshot):
    """Keep empty HUDs off-screen and size visible panels to their content."""
    if not spec.get("hud"):
        return False
    socket = spec["socket"]
    from .viewer import state, close_view
    view = state(spec["runtime"])
    if view and not view.get("ready"):
        # Opening holds layout_lock until ready. Seeing an incomplete swap
        # under this lock means its helper exited and needs recovery.
        if not close_view(spec["runtime"], locked=True):
            return False
        return _sync_layout(spec, snapshot)
    top = view.get("pane") if view.get("ready") else spec["top"]
    geometry = tmux(socket, "display-message", "-p", "-t", top,
                    "#{window_id} #{window_height} #{window_width} #{window_zoomed_flag} #{pane_dead}", ";",
                    "display-message", "-p", "-t", spec["hud"], "#{window_id} #{pane_height}", check=False).stdout.splitlines()
    if len(geometry) != 2:
        if view:
            if not close_view(spec["runtime"], locked=True):
                return False
            return _sync_layout(spec, snapshot)
        return False
    main, panel_info = (line.split() for line in geometry)
    if len(main) != 5 or len(panel_info) != 2:
        if view:
            if not close_view(spec["runtime"], locked=True):
                return False
            return _sync_layout(spec, snapshot)
        return False
    if view and main[4] == "1":
        if not close_view(spec["runtime"], locked=True):
            return False
        return _sync_layout(spec, snapshot)
    rows = render_rows(snapshot, int(main[2]))
    docked = main[0] == panel_info[0]
    if not rows:
        if docked:
            if main[3] == "1":
                tmux(socket, "resize-pane", "-t", top, "-Z", check=False)
            tmux(socket, "break-pane", "-d", "-s", spec["hud"], "-n", "_hud", check=False)
            tmux(socket, "select-pane", "-t", top, check=False)
        return False
    desired = min(snapshot.get("display", {}).get("height", spec["hud_height"]), max(1, int(main[1]) // 2), len(rows))
    if not docked:
        tmux(socket, "join-pane", "-d", "-v", "-l", desired, "-s", spec["hud"], "-t", top, check=False)
    elif main[3] == "0" and int(panel_info[1]) != desired:
        tmux(socket, "resize-pane", "-t", spec["hud"], "-y", desired, check=False)
    return docked and main[3] == "1"


def panel(runtime):
    from .viewer import state as view_state, close_view, open_view
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
        frame_cache = {}
        last_paint = None
        while True:
            if (runtime / "exit.json").exists():
                break
            nav = read_json(runtime / "navigation.json", {})
            viewing = bool(view_state(runtime).get("ready"))
            selected = (tuple(nav.get("key") or []) or None) if snapshot else None
            view = snapshot
            if selected and snapshot:
                from .keys import clear
                view = {**snapshot, "display": {**snapshot["display"], "agents_limit": 0}}
                choices = [row.key for row in render_rows(view) if row.key]
                main_active = not viewing and tmux(spec["socket"], "display-message", "-p", "-t", spec["top"], "#{pane_active}").stdout.strip() == "1"
                if nav.get("root") != snapshot["root"].get("id") or selected not in choices or main_active:
                    clear(runtime, spec)
                    if not main_active and not viewing:
                        tmux(spec["socket"], "select-pane", "-t", spec["top"])
                    selected = None
                    view = snapshot
            try:
                raw_key = screen.get_wch()
            except curses.error:
                raw_key = None
            # Unicode codepoints can overlap curses' integer key constants.
            key = (ord(raw_key) if ord(raw_key) < 128 else -2) if isinstance(raw_key, str) else raw_key if raw_key is not None else -1
            if key != -1 and not selected and not viewing and tmux(spec["socket"], "display-message", "-p", "-t", spec["top"], "#{pane_active}").stdout.strip() == "1":
                # A burst can already be buffered in this pane when typing
                # returns focus to the CLI. Forward the rest instead of losing
                # the second character (including multibyte Unicode input).
                names = {curses.KEY_DOWN: "Down", curses.KEY_UP: "Up", curses.KEY_LEFT: "Left",
                         curses.KEY_RIGHT: "Right", curses.KEY_HOME: "Home", curses.KEY_END: "End",
                         curses.KEY_BACKSPACE: "BSpace", curses.KEY_DC: "DC", curses.KEY_ENTER: "Enter"}
                if isinstance(raw_key, str):
                    tmux(spec["socket"], "send-keys", "-t", spec["top"], "-l", raw_key)
                elif key in names:
                    tmux(spec["socket"], "send-keys", "-t", spec["top"], names[key])
                key, raw_key = -1, None
            if selected and key in (curses.KEY_DOWN, curses.KEY_UP, 9, curses.KEY_BTAB, 10, 13, curses.KEY_ENTER, 27):
                from .keys import clear
                if key == 27:
                    close_view(runtime)
                    clear(runtime, spec)
                    selected = None
                    tmux(spec["socket"], "select-pane", "-t", spec["top"])
                elif key in (10, 13, curses.KEY_ENTER):
                    open_view(runtime, selected, nav["root"], True)
                else:
                    index = choices.index(selected)+(1 if key in (curses.KEY_DOWN, 9) else -1)
                    index = index % len(choices) if key in (9, curses.KEY_BTAB) else max(0, min(len(choices)-1, index))
                    selected = choices[index]
                    atomic_json(runtime / "navigation.json", {"root": nav["root"], "key": selected})
                    if selected[0] != "main":
                        open_view(runtime, selected, nav["root"], True)
            elif selected and (isinstance(raw_key, str) or key == curses.KEY_BACKSPACE):
                from .keys import clear
                close_view(runtime)
                clear(runtime, spec)
                selected = None
                tmux(spec["socket"], "select-pane", "-t", spec["top"])
                if key in (8, 127, curses.KEY_BACKSPACE):
                    tmux(spec["socket"], "send-keys", "-t", spec["top"], "BSpace")
                else:
                    tmux(spec["socket"], "send-keys", "-t", spec["top"], "-l", raw_key)
            elif key == curses.KEY_DOWN and not expanded and snapshot and (snapshot["agents"] or snapshot["offload"]["jobs"]):
                open_view(runtime)
            elif key in (curses.KEY_DOWN, ord("j")):
                scroll += 1
            elif key in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif key in (curses.KEY_HOME, ord("g")):
                scroll = 0
            elif key in (curses.KEY_END, ord("G")):
                scroll = 10**6
            elif key == 27:
                close_view(runtime)
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
            if selected and snapshot:
                view = {**snapshot, "display": {**snapshot["display"], "agents_limit": 0}}
                if nav.get("root") != snapshot["root"].get("id") or selected not in [r.key for r in render_rows(view) if r.key]:
                    from .keys import clear
                    clear(runtime, spec)
                    if not view_state(runtime).get("ready"):
                        tmux(spec["socket"], "select-pane", "-t", spec["top"])
                    selected = None
                    view = snapshot
            else:
                view = snapshot
            paint_state = (id(snapshot), screen.getmaxyx(), scroll, expanded, selected)
            if key != -1 or paint_state != last_paint:
                scroll = paint(screen, view, scroll, expanded, selected, frame_cache)
                last_paint = (id(snapshot), screen.getmaxyx(), scroll, expanded, selected)
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
        # A successful pane query also proves the private session is alive.
        # Only probe the session separately on a failed pane query.
        dead = tmux(spec["socket"], "display-message", "-p", "-t", spec["top"], "#{pane_dead}", check=False)
        if dead.returncode and tmux(spec["socket"], "has-session", "-t", "codex", check=False).returncode:
            shutil.rmtree(runtime, ignore_errors=True)
            break
        if dead.stdout.strip() == "1":
            atomic_json(runtime / "exit.json", {"code": 1, "time": time.time(), "error": "CLI runner exited"})
            continue
        # Terminal disconnect cleanup is immediate in tmux; this poll only
        # handles abnormal pane exits and removes the owned temporary files.
        time.sleep(1)
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
    if curses is None:
        parser.error("此平台的只读数据核对请使用 --once；交互 HUD 使用原生终端后端")
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
        from .viewer import close_view
        close_view(runtime)
        before = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout.strip()
        configure_terminal(spec)
        tmux(socket, "respawn-pane", "-k", "-t", spec["hud"], command("_panel", runtime, runtime=runtime))
        tmux(socket, "bind-key", "H", "run-shell", command("_toggle", runtime, runtime=runtime))
        f10_bound = bind_settings(spec)
        bind_agents(spec)
        unwrapped = unwrap_owned_parent(spec)
        reconnected = reconnect_unsynced_clients(spec)
        configure_lifetime(spec)
        after = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout.strip()
        if before != after:
            raise RuntimeError("CLI pane identity changed while refreshing HUD")
        updated.append({"runtime": str(runtime), "cli_pid": before, "reconnected_clients": reconnected, "unwrapped_clients": unwrapped,
                        "close_on_disconnect": spec.get("close_on_disconnect", True),
                        "settings_shortcut": "F10" if f10_bound else "Ctrl-b C"})
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
    if argv and argv[0] in ("_agents", "_view"):
        from .agents import run
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("runtime")
        parser.add_argument("--focus", type=json.loads)
        parser.add_argument("--root")
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--embedded", action="store_true")
        args = parser.parse_args(argv[1:])
        if argv[0] == "_view":
            from .viewer import open_view
            return open_view(args.runtime, args.focus, args.root, args.detail)
        return run(args.runtime, args.focus, args.root, args.detail, args.embedded)
    if argv and argv[0] == "_down":
        from .keys import down
        return down(argv[1], argv[2])
    if argv and argv[0] == "_native_bridge":
        from .native import bridge
        return bridge(argv[1])
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
