"""Read-only agent inspection. Opening a view never resumes a thread."""
from __future__ import annotations

try:
    import curses
except ImportError:
    curses = None
import json
from pathlib import Path
import sqlite3
import subprocess
import time

from .display import clean, clip, elapsed, ICONS, LABEL, number, cell_width, init_colors, COLORS, STATE_COLOR
from .sources import database, read_json, Rollout, timestamp


def entries(snapshot):
    result = []
    for kind, items in (("native", snapshot.get("agents", [])),
                        ("relay", snapshot.get("offload", {}).get("jobs", []))):
        for item in items:
            identity = item.get("id")
            if not identity:
                continue
            result.append({**item, "kind": kind, "key": (kind, identity),
                           "thread": identity if kind == "native" else item.get("worker_id")})
    return result


def transcript(home, entry):
    """The caller selects only entries correlated to the current snapshot."""
    identity = entry.get("thread")
    if not identity:
        return [], [], "暂无 worker 会话记录 / no worker session record"
    db = database(home, "state")
    if db is None:
        return [], [], "会话数据库不可用 / session database unavailable"
    try:
        row = db.execute("SELECT rollout_path FROM threads WHERE id=?", (identity,)).fetchone()
    finally:
        db.close()
    if not row or not row["rollout_path"]:
        return [], [], "会话日志尚未保存 / transcript not saved yet"
    path = Path(row["rollout_path"]).resolve()
    if not path.is_relative_to((Path(home) / "sessions").resolve()) or path.suffix != ".jsonl":
        return [], [], "会话日志路径不属于当前 Codex / transcript outside Codex sessions"
    reader = Rollout(path)
    metadata = reader.refresh()
    if metadata.get("id") != identity:
        return [], [], "会话日志身份不匹配 / transcript identity mismatch"
    tools = [t for t in metadata.get("tools") or [] if metadata.get("created_at") is None
             or t.get("started") is None or t["started"] >= metadata["created_at"]]
    messages = []
    with path.open("rb") as handle:
        start = max(0, path.stat().st_size - 128 * 1024)
        handle.seek(start)
        if start:
            handle.readline()
        for line in handle:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(row, dict):
                continue
            payload = row.get("payload") or {}
            if row.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            when = timestamp(row.get("timestamp"))
            if when is not None and metadata.get("created_at") is not None and when < metadata["created_at"]:
                continue  # Copied parent context is not this agent's output.
            if payload.get("type") != "agent_message" and not (payload.get("type") == "message" and payload.get("role") == "assistant"):
                continue
            content = payload.get("content") or []
            if not isinstance(content, list):
                continue
            text = "\n".join(x["text"] for x in content if isinstance(x, dict) and isinstance(x.get("text"), str))
            if text:
                messages.append(text)
    return tools[-5:], messages[-5:], "" if messages else "读取尾部 128 KB 未找到完整可读输出；可能尚未产生、已加密或超出读取范围 / no readable output in the 128 KB tail"


def details(home, item, now, en=False):
    state = item.get("status", "unknown")
    title = item.get("name") or item.get("model") or item["id"]
    output = [f"{ICONS.get(state, '?')} {clean(title)} · {state if en else LABEL.get(state, state)} · {elapsed(item, now)}",
              ("Model: " if en else "模型：") + clean(item.get("model") or "?"),
              ("Task: " if en else "任务：") + clean(item.get("task") or "?"),
              ("Thread: " if en else "会话：") + clean(item.get("thread") or "?")]
    usage = item.get("usage")
    if isinstance(usage, dict):
        output.append("Tokens: " + " · ".join(f"{label} {number(usage.get(key))}" for key, label in
                                             (("input_tokens", "in"), ("cached_input_tokens", "cache"), ("output_tokens", "out"))))
    else:
        output.append("Tokens: unavailable" if en else "Token：未返回用量")
    if item.get("error"):
        output += ["Error: " + clean(item["error"])]
    if item.get("kind") == "relay" and isinstance(item.get("output"), str) and item["output"]:
        tools, messages, notice = [], [item["output"]], ""
    else:
        try:
            tools, messages, notice = transcript(home, item)
        except (OSError, ValueError, sqlite3.Error) as error:
            tools, messages, notice = [], [], "日志不可读取 / transcript unavailable: " + clean(error)
    output += ["", "Recent tools" if en else "最近工具"]
    output += [f"{ICONS.get(t['status'], '?')} {clean(t['name'])}" for t in tools] or ["—"]
    output += ["", "Latest output" if en else "最新输出"]
    if notice:
        output += [notice]
    for message in messages:
        output += [clean(line) for line in message.splitlines()] + [""]
    return output


def wrapped(lines, width):
    result = []
    for line in lines:
        part, used = "", 0
        for char in line:
            size = cell_width(char)
            if used+size > width and part:
                result.append(part)
                part, used = "", 0
            part += char
            used += size
        result.append(part)
    return result


def run(runtime, focus=None, root=None, detail=False):
    runtime = Path(runtime)
    spec = read_json(runtime / "spec.json", {})
    if not spec.get("home"):
        return 1
    def ui(screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(100)
        initial = read_json(runtime / "snapshot.json", {})
        identity = root or (initial.get("root") or {}).get("id")
        inline = bool(focus and detail)
        selection, selected_key, expanded, scroll = 0, tuple(focus) if focus else None, detail, 0
        refreshed, snapshot, detail_key, body, palette = 0, {}, None, [], None
        while runtime.exists() and not (runtime / "exit.json").exists():
            if time.monotonic()-refreshed >= 1:
                snapshot = read_json(runtime / "snapshot.json", {})
                refreshed = time.monotonic()
            current = (snapshot.get("root") or {}).get("id")
            if current != identity:
                identity, selected_key, selection, expanded, scroll = current, None, 0, False, 0
                inline = False
            items = entries(snapshot)
            if selected_key:
                match = next((i for i, item in enumerate(items) if item["key"] == selected_key), None)
                if match is None:
                    selected_key, expanded, scroll = None, False, 0
                else:
                    selection = match
            selection = min(selection, max(0, len(items)-1))
            en = snapshot.get("display", {}).get("language") == "en"
            config = snapshot.get("display") or {}
            if config.get("colors") != palette:
                init_colors(config)
                palette = config.get("colors")
            height, width = screen.getmaxyx()
            screen.erase()
            def line(y, value, attr=0):
                if 0 <= y < height:
                    try:
                        screen.addstr(y, 1, clip(value, width-2), attr)
                    except curses.error:
                        pass
            line(0, "Codex HUD · Agents · read-only" if en else "Codex HUD · Agent 选择器 · 只读", curses.A_BOLD)
            line(1, ("↑↓ scroll · Esc back to HUD · q back to input" if en else "↑↓ 滚动 · Esc 返回 HUD · q 返回输入") if inline else ("↑↓ scroll · Esc list · q close" if en else "↑↓ 滚动 · Esc 返回列表 · q 关闭") if expanded else ("↑↓ select · Enter inspect · Esc/q close" if en else "↑↓ 选择 · Enter 查看 · Esc/q 返回主会话"))
            room = max(1, height-4)
            if expanded and items:
                current_detail = (identity, items[selection]["key"], snapshot.get("time"), en, width)
                if current_detail != detail_key:
                    body = wrapped(details(spec["home"], items[selection], snapshot.get("time", time.time()), en), max(1, width-2))
                    detail_key = current_detail
                scroll = min(scroll, max(0, len(body)-room))
                for index, value in enumerate(body[scroll:scroll+room]):
                    line(index+3, value)
            elif items:
                offset = max(0, selection-room+1)
                for index, item in enumerate(items[offset:offset+room], offset):
                    title = item.get("name") or item.get("model") or item["id"]
                    kind = item["kind"] if en else "原生" if item["kind"] == "native" else "分流"
                    state = item.get("status", "unknown")
                    value = f"{ICONS.get(state, '?')} {kind} · {clean(title)} · {state if en else LABEL.get(state, state)} · {elapsed(item, snapshot.get('time', time.time()))}"
                    attr = curses.A_REVERSE | curses.A_BOLD if index == selection else curses.color_pair(COLORS[STATE_COLOR.get(state, "muted")])
                    line(index-offset+3, value, attr)
            else:
                line(3, "No agents in this conversation yet." if en else "当前会话暂无子 agent 或分流任务；出现后自动列出。")
            screen.refresh()
            key = screen.getch()
            if key == ord("q") or (key == 27 and not expanded):
                return "main"
            if key == 27 and inline:
                return "hud"
            if key == 27:
                expanded, scroll = False, 0
            elif key in (10, 13, curses.KEY_ENTER) and items:
                expanded, scroll = True, 0
            elif key in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k")):
                delta = 1 if key in (curses.KEY_DOWN, ord("j")) else -1
                if expanded:
                    scroll = max(0, scroll+delta)
                elif items:
                    selection = (selection+delta) % len(items)
            elif expanded and key in (curses.KEY_NPAGE, curses.KEY_PPAGE, curses.KEY_HOME, curses.KEY_END):
                scroll = max(0, scroll+(room if key == curses.KEY_NPAGE else -room)) if key in (curses.KEY_NPAGE, curses.KEY_PPAGE) else 0 if key == curses.KEY_HOME else 10**9
            if items:
                selected_key = items[selection]["key"]
    try:
        destination = curses.wrapper(ui)
    finally:
        if runtime.exists() and spec.get("socket") and spec.get("top"):
            nav = read_json(runtime / "navigation.json", {})
            target = spec["hud"] if locals().get("destination") == "hud" and nav.get("root") == root and tuple(nav.get("key") or []) == tuple(focus or []) else spec["top"]
            if target == spec["top"]:
                from .keys import clear
                clear(runtime, spec)
            subprocess.run(["tmux", "-L", spec["socket"], "select-pane", "-t", target],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    return 0
