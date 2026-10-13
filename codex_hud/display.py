from __future__ import annotations

import curses
from dataclasses import dataclass
from pathlib import Path
import unicodedata

LABEL = {"running": "运行", "idle": "待命", "completed": "完成", "failed": "失败", "unknown": "未知"}
ICONS = {"running": "●", "idle": "○", "completed": "✓", "failed": "✕", "unknown": "?"}
STATE_COLOR = {"running": "good", "idle": "muted", "completed": "good", "failed": "bad", "unknown": "warn"}
COLORS = {"text": 0, "model": 1, "good": 2, "warn": 3, "bad": 4, "muted": 5, "agent": 6, "dim": 7}


def _safe(text):
    return "".join(" " if c in "\n\t" else c for c in str(text)
                   if not unicodedata.category(c).startswith("C") or c in "\n\t")


def clean(text):
    return " ".join(_safe(text).split())


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in text)


def clip(text, width):
    text = _safe(text)
    if width <= 0:
        return ""
    if cell_width(text) <= width:
        return text
    result, used = "", 0
    for char in text:
        size = cell_width(char)
        if used + size > width - 1:
            break
        result += char
        used += size
    return result + "…"


def number(value):
    if not isinstance(value, (int, float)):
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}m"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(int(value))


def elapsed(item, now):
    duration = item.get("duration")
    if duration is None and item.get("started"):
        end = now if item.get("status") == "running" else item.get("ended")
        if end:
            duration = max(0, end - item["started"])
    if duration is None:
        return "耗时未知"
    seconds = int(duration)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02}s" if seconds >= 60 else f"{seconds}s"


def progress(fraction, width=16):
    """Terminal-cell bar; percentages retain the actual value above capacity."""
    units = round(max(0, min(1, fraction)) * width * 8)
    full, partial = divmod(units, 8)
    filled = "█" * full + ("▏▎▍▌▋▊▉"[partial - 1] if partial else "")
    return filled, "░" * (width - full - bool(partial))


def capacity_color(fraction):
    return "bad" if fraction >= .9 else "warn" if fraction >= .7 else "good"


@dataclass
class Row:
    spans: list[tuple[str, str]]
    pinned: bool = False

    @property
    def text(self):
        return "".join(text for text, _ in self.spans)


def render_rows(snapshot, width=100):
    root, agents, offload = snapshot["root"], snapshot["agents"], snapshot["offload"]
    usage = root.get("usage") or {}
    jobs, git = offload["jobs"], snapshot["git"]
    used, window = root.get("context_used"), root.get("context_window")
    has_context = isinstance(used, int) and isinstance(window, int) and window > 0
    has_usage = any(isinstance(value, (int, float)) and value > 0 for value in usage.values())
    has_core = bool(root.get("model") or has_context or has_usage or git.get("branch"))
    rows = []
    if has_core:
        header = [(f" [{clean(root.get('model') or 'Codex')}]", "model")]
        if root.get("effort"):
            header.append((f" {clean(root['effort'])}", "muted"))
        status = root.get("status")
        if status in LABEL:
            header.append((f"  {ICONS[status]} {LABEL[status]}", STATE_COLOR[status]))
        if root.get("cwd"):
            cwd = str(root["cwd"])
            user_home = str(Path.home())
            if cwd == user_home or cwd.startswith(user_home + "/"):
                cwd = "~" + cwd[len(user_home):]
            if width < 100 and len(cwd) > 22:
                cwd = Path(cwd).name
            header += [("  │  ", "dim"), (clip(cwd, 28 if width >= 100 else 22), "muted")]
        if git.get("branch"):
            header += [("  git:", "dim"), (clean(git["branch"]), "agent")]
            if git.get("changes"):
                header.append((f" +{git['changes']}", "warn"))
        rows.append(Row(header, pinned=True))
    if has_context:
        fraction = used / window
        filled, empty = progress(fraction, 20 if width >= 100 else 14 if width >= 70 else 8)
        color = capacity_color(fraction)
        rows.append(Row([(" 上下文 ", "muted"), (filled, color), (empty, "dim"),
                         (f" {fraction:.0%}", color), (f"  {number(used)} / {number(window)} tokens", "muted")], pinned=True))
    if has_usage:
        spans = [(" Token ", "muted")]
        for key, label in (("input_tokens", "↑"), ("output_tokens", "↓")):
            if isinstance(usage.get(key), (int, float)):
                spans += [(f" {label} ", "muted"), (number(usage[key]), "text")]
        if isinstance(usage.get("reasoning_output_tokens"), int) and usage["reasoning_output_tokens"] > 0:
            spans += [("  推理 ", "muted"), (number(usage["reasoning_output_tokens"]), "text")]
        if isinstance(usage.get("total_tokens"), int):
            spans += [("  │  累计 ", "dim"), (number(usage["total_tokens"]), "text")]
        rows.append(Row(spans, pinned=True))
        cached, input_tokens = usage.get("cached_input_tokens"), usage.get("input_tokens")
        if isinstance(cached, int) and isinstance(input_tokens, int) and 0 <= cached <= input_tokens and input_tokens > 0:
            fraction = cached / input_tokens
            filled, empty = progress(fraction, 20 if width >= 100 else 14 if width >= 70 else 8)
            rows.append(Row([(" 缓存   ", "muted"), (filled, "model"), (empty, "dim"),
                             (f" {fraction:.0%}", "model"), (f"  {number(cached)} / {number(input_tokens)} tokens", "muted")], pinned=True))
    if agents:
        counts = [(key, sum(a["status"] == key for a in agents)) for key in LABEL]
        spans = [(" Agents", "model")]
        for key, count in counts:
            if count:
                spans += [("  ", "dim"), (f"{count} {LABEL[key]}", STATE_COLOR[key])]
        rows.append(Row(spans))
        for agent in agents:
            status = agent["status"]
            rows.append(Row([(f" {ICONS.get(status, '?')} ", STATE_COLOR.get(status, "warn")),
                             (clean(agent["name"].removeprefix("/root/")), "agent"),
                             (f"  {elapsed(agent, snapshot['time'])}", "muted"),
                             ("  ·  ", "dim"), (clean(agent["task"]).removesuffix(" (任务名；详细指令加密)"), "muted")]))
    if jobs:
        enabled = "ON" if offload["enabled"] is True else "OFF" if offload["enabled"] is False else "未知"
        running = sum(j["status"] == "running" for j in jobs)
        completed = sum(j["status"] == "completed" for j in jobs)
        rows.append(Row([(" codex-x-codex ", "model"), (enabled, "good" if enabled == "ON" else "muted"),
                         (f"  │  {running} 运行 · {completed} 完成", "muted")]))
        unknown = sum(j["status"] in ("completed", "failed") and j.get("usage") is None for j in jobs)
        if unknown:
            rows.append(Row([(f" ! 分流未知用量 {unknown} 项", "warn")]))
        for job in jobs:
            status, job_usage = job["status"], job.get("usage")
            spans = [(f" {ICONS.get(status, '?')} ", STATE_COLOR.get(status, "warn")),
                     (clean(job.get("model") or "Relay"), "agent"),
                     (f"  {elapsed(job, snapshot['time'])}", "muted")]
            if job_usage:
                for key, label in (("input_tokens", "↑"), ("cached_input_tokens", "缓存"), ("output_tokens", "↓")):
                    if isinstance(job_usage.get(key), int):
                        spans.append((f"  {label} {number(job_usage[key])}", "muted"))
            else:
                spans.append(("  用量待回传" if status == "running" else "  用量未知", "muted" if status == "running" else "warn"))
            spans += [("  ·  ", "dim"), (clean(job["task"]), "muted")]
            rows.append(Row(spans))
    for error in snapshot.get("errors", []):
        rows.append(Row([(f" ! {clean(error)}", "warn")]))
    if git.get("error"):
        rows.append(Row([(f" ! {clean(git['error'])}", "warn")]))
    return rows


def lines(snapshot):
    """Plain view for inspection and source-contract tests."""
    return [(row.text, row.spans[0][1]) for row in render_rows(snapshot)]


def init_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    palette = [81, 114, 220, 203, 245, 141, 238] if curses.COLORS >= 256 else [curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_WHITE, curses.COLOR_MAGENTA, curses.COLOR_WHITE]
    for index, color in enumerate(palette, 1):
        curses.init_pair(index, color, -1)


def paint(screen, snapshot, scroll=0, expanded=False):
    height, width = screen.getmaxyx()
    rows = render_rows(snapshot, width)
    screen.erase()
    pinned = [row for row in rows if row.pinned]
    details = [row for row in rows if not row.pinned]
    overflow = len(rows) > height
    footer = expanded or overflow
    pinned = pinned[:max(0, height - int(footer))]
    room = max(0, height - len(pinned) - int(footer))
    scroll = max(0, min(scroll, max(0, len(details) - room)))
    visible = pinned + details[scroll:scroll + room]
    if footer:
        remaining = max(0, len(details) - scroll - room)
        hint = " ↑↓ 滚动 · Esc 返回 CLI" if expanded else f" … 另有 {remaining} 行 · Ctrl-b H 展开"
        visible += [Row([(hint, "dim")])]
    for y, row in enumerate(visible[:height]):
        x = 0
        for text, kind in row.spans:
            rendered = clip(text, width - x - 1)
            if not rendered:
                break
            try:
                attr = curses.color_pair(COLORS.get(kind, 0))
                if kind == "model":
                    attr |= curses.A_BOLD
                screen.addstr(y, x, rendered, attr)
            except curses.error:
                pass
            x += cell_width(rendered)
            if rendered != text:
                break
    screen.refresh()
    return scroll
