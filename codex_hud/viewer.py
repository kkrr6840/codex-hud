"""Swap a read-only viewer into the conversation area, retaining the CLI PTY."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import re
import time
import uuid

from .sources import Diagnostic, atomic_json, read_json


@contextmanager
def layout_lock(runtime):
    import fcntl
    with (Path(runtime) / "layout.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def state(runtime):
    view = read_json(Path(runtime) / "view.json", {})
    return view if re.fullmatch(r"%\d+", str(view.get("pane", ""))) else {}


def close_view(runtime, token=None, locked=False, keep_navigation=False):
    from .cli import tmux
    from .keys import clear
    runtime = Path(runtime)
    if not runtime.exists():
        return True
    with nullcontext() if locked else layout_lock(runtime):
        view = state(runtime)
        if not view or (token and view.get("token") != token):
            return True
        spec = read_json(runtime / "spec.json", {})
        if spec.get("socket") != runtime.name:
            return False
        socket = spec["socket"]
        main_window = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{window_id}", check=False).stdout.strip()
        view_window = tmux(socket, "display-message", "-p", "-t", view["pane"], "#{window_id}", check=False).stdout.strip()
        if main_window == view["hidden_window"]:
            if view_window == view["main_window"]:
                tmux(socket, "swap-pane", "-d", "-s", spec["top"], "-t", view["pane"], check=False)
            else:
                # A crashed viewer may already have lost its pane. Rejoin the
                # still-running CLI above its own HUD, never respawn the CLI.
                tmux(socket, "join-pane", "-b", "-v", "-s", spec["top"], "-t", spec["hud"], check=False)
            restored = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{window_id}", check=False).stdout.strip()
            if restored != view["main_window"]:
                # Keep the recovery record; the observer retries after a
                # transient layout failure or a terminal that is too small.
                return False
            main_window = restored
        if not main_window:
            return False
        (runtime / "view.json").unlink(missing_ok=True)
        clear(runtime, spec)
        tmux(socket, "select-window", "-t", main_window, check=False)
        root = (read_json(runtime / "snapshot.json", {}).get("root") or {}).get("id")
        if keep_navigation and root == view["root"]:
            tmux(socket, "select-pane", "-t", spec["hud"], check=False)
            atomic_json(runtime / "navigation.json", {"root": root, "key": ["main", root]})
            tmux(socket, "set-option", "-w", "-t", spec["top"], "@hud-nav", "1", check=False)
        else:
            tmux(socket, "select-pane", "-t", spec["top"], check=False)
    # The viewer exits naturally after restoring the CLI. External recovery
    # also terminates the owned viewer pane; the original CLI is never killed.
    if os.environ.get("TMUX_PANE") != view["pane"]:
        tmux(socket, "kill-pane", "-t", view["pane"], check=False)
    return True


def open_view(runtime, focus=None, root=None, detail=False):
    from .agents import entries
    from .cli import command, tmux
    runtime = Path(runtime)
    spec = read_json(runtime / "spec.json", {})
    snapshot = read_json(runtime / "snapshot.json", {})
    identity = (snapshot.get("root") or {}).get("id")
    if spec.get("socket") != runtime.name or not identity or (root and root != identity):
        return 0
    actual = Diagnostic(spec["diagnostic"]).bind(spec["home"]) if spec.get("diagnostic") else None
    if actual is not None and actual[0] != identity:
        return 0
    if focus and tuple(focus) not in [item["key"] for item in entries(snapshot)]:
        return 0
    socket = spec["socket"]
    if focus and tuple(focus) == ("main", identity):
        if close_view(runtime):
            from .keys import clear
            clear(runtime, spec)
            tmux(socket, "select-pane", "-t", spec["top"], check=False)
        return 0
    try:
        with layout_lock(runtime):
            view = state(runtime)
            if view and view.get("root") == identity and tmux(socket, "display-message", "-p", "-t", view["pane"], "#{pane_id}", check=False).stdout.strip() == view["pane"]:
                atomic_json(runtime / "view.json", {**view, "focus": focus, "detail": detail, "request": uuid.uuid4().hex})
                tmux(socket, "select-pane", "-t", view["pane"])
                return 0
            if view:
                if not close_view(runtime, locked=True):
                    return 0
            main, width, height = tmux(socket, "display-message", "-p", "-t", spec["top"], "#{window_id} #{pane_width} #{pane_height}").stdout.split()
            created = tmux(socket, "new-window", "-d", "-t", "codex", "-n", "_hud_view", "-P", "-F", "#{pane_id} #{window_id}",
                           command("_agents", runtime, "--embedded", runtime=runtime)).stdout.split()
            pane, hidden = created
            view = {"pane": pane, "hidden_window": hidden, "main_window": main, "root": identity,
                    "focus": focus, "detail": detail, "ready": False, "request": uuid.uuid4().hex, "token": uuid.uuid4().hex}
            atomic_json(runtime / "view.json", view)
            tmux(socket, "set-option", "-p", "-t", pane, "remain-on-exit", "off")
            tmux(socket, "resize-window", "-t", hidden, "-x", width, "-y", height)
            tmux(socket, "swap-pane", "-d", "-s", spec["top"], "-t", pane)
            atomic_json(runtime / "view.json", {**view, "ready": True})
            tmux(socket, "select-pane", "-t", pane)
    except Exception:
        close_view(runtime)
        raise
    return 0


def wait_ready(runtime):
    for _ in range(100):
        view = state(runtime)
        if view.get("ready") and view["pane"] == os.environ.get("TMUX_PANE"):
            return view
        if not Path(runtime).exists():
            break
        time.sleep(.05)
    return {}


def select_entry(runtime, key, root, token):
    from .cli import tmux
    runtime = Path(runtime)
    with layout_lock(runtime):
        view = state(runtime)
        if view.get("token") != token or view.get("root") != root:
            return
        atomic_json(runtime / "view.json", {**view, "focus": key, "detail": True})
        atomic_json(runtime / "navigation.json", {"root": root, "key": key})
        spec = read_json(runtime / "spec.json", {})
        tmux(spec["socket"], "set-option", "-w", "-t", spec["hud"], "@hud-nav", "1", check=False)
