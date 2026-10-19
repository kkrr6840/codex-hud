"""Route plain arrows only from a verified empty native CLI composer."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

from .display import render_rows
from .sources import Diagnostic, atomic_json, read_json


def empty_composer(styled_line, cursor_x, visible=True, in_mode=False):
    if cursor_x != 2 or not visible or in_mode:
        return False
    # Track SGR faint state per character; RGB's `2` is not a faint flag.
    text, faint, styles, end = "", False, [], 0
    for match in re.finditer(r"\x1b\[([0-9;]*)m", styled_line):
        segment = styled_line[end:match.start()]
        text += segment
        styles += [faint] * len(segment)
        codes = [int(x or "0") for x in match[1].split(";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code in (38, 48, 58) and i+1 < len(codes):
                i += 5 if codes[i+1] == 2 else 3 if codes[i+1] == 5 else 1
                continue
            if code in (0, 22):
                faint = False
            elif code == 2:
                faint = True
            i += 1
        end = match.end()
    text += styled_line[end:]
    styles += [faint] * len(styled_line[end:])
    prompt = "› Ask Codex to do anything"
    return text.rstrip() == prompt and all(styles[2:len(prompt)])


def clear(runtime, spec=None):
    from .cli import tmux
    runtime = Path(runtime)
    (runtime / "navigation.json").unlink(missing_ok=True)
    spec = spec or read_json(runtime / "spec.json", {})
    if spec.get("socket") and spec.get("top"):
        tmux(spec["socket"], "set-option", "-w", "-t", spec["top"], "@hud-nav", "0", check=False)


def down(runtime, pane):
    from .cli import tmux
    runtime = Path(runtime)
    spec = read_json(runtime / "spec.json", {})
    if spec.get("socket") != runtime.name or not re.fullmatch(r"%\d+", pane):
        return 1
    socket = spec["socket"]
    def forward(target=pane):
        tmux(socket, "send-keys", "-t", target, "Down", check=False)
        return 0
    if pane != spec.get("top"):
        from .viewer import state
        if pane == spec.get("hud") and not state(runtime).get("ready") and tmux(socket, "display-message", "-p", "-t", spec["top"], "#{pane_active}").stdout.strip() == "1":
            return forward(spec["top"])
        return forward()
    try:
        snapshot = read_json(runtime / "snapshot.json", {})
        root = (snapshot.get("root") or {}).get("id")
        rows = render_rows(snapshot)
        choices = [row.key for row in rows if row.key]
        if not root or not choices:
            return forward()
        if spec.get("diagnostic"):
            actual = Diagnostic(spec["diagnostic"]).bind(spec["home"])
            if actual is not None and actual[0] != root:
                return forward()
        nav = read_json(runtime / "navigation.json", {})
        hud_active = tmux(socket, "display-message", "-p", "-t", spec["hud"], "#{pane_active}").stdout.strip() == "1"
        if nav.get("root") == root and hud_active:
            return forward(spec["hud"])
        fields = tmux(socket, "display-message", "-p", "-t", pane,
                      "#{pane_active} #{pane_in_mode} #{cursor_x} #{cursor_y} #{cursor_flag}").stdout.split()
        if len(fields) != 5 or fields[0] != "1":
            return forward()
        line = tmux(socket, "capture-pane", "-p", "-e", "-t", pane, "-S", fields[3], "-E", fields[3]).stdout
        if not empty_composer(line, int(fields[2]), fields[4] == "1", fields[1] != "0"):
            return forward()
        # Focus first so the observer cannot mistake pending focus for a click
        # back into the composer. Matched key commands stay in the tmux queue.
        tmux(socket, "select-pane", "-t", spec["hud"])
        atomic_json(runtime / "navigation.json", {"root": root, "key": choices[0]})
        tmux(socket, "set-option", "-w", "-t", spec["top"], "@hud-nav", "1")
        return 0
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        return forward()
