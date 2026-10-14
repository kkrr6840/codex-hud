from __future__ import annotations

try:
    import curses
except ImportError:
    curses = None
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
    key: tuple[str, str] | None = None

    @property
    def text(self):
        return "".join(text for text, _ in self.spans)


def render_rows(snapshot, width=100, config=None):
    from .config import DEFAULT
    from .views import render
    return render(snapshot, width, config or snapshot.get("display") or DEFAULT)


def lines(snapshot):
    """Plain view for inspection and source-contract tests."""
    return [(row.text, row.spans[0][1]) for row in render_rows(snapshot)]


def init_colors(config=None):
    if not curses.has_colors():
        return
    curses.start_color()
    curses.use_default_colors()
    palette = [81, 114, 220, 203, 245, 141, 238] if curses.COLORS >= 256 else [curses.COLOR_CYAN, curses.COLOR_GREEN, curses.COLOR_YELLOW, curses.COLOR_RED, curses.COLOR_WHITE, curses.COLOR_MAGENTA, curses.COLOR_WHITE]
    if config and curses.COLORS >= 256:
        palette = [color_index(config["colors"][key]) for key in ("model", "good", "warn", "bad", "muted", "agent", "dim")]
    for index, color in enumerate(palette, 1):
        curses.init_pair(index, color, -1)


def paint(screen, snapshot, scroll=0, expanded=False, selected_key=None, frame_cache=None):
    height, width = screen.getmaxyx()
    width = min(width, (snapshot.get("display") or {}).get("max_width") or width)
    rows = render_rows(snapshot, width)
    # Expanded view scrolls every row; normal view preserves configured order.
    pinned_count = 0
    if not expanded:
        for row in rows:
            if not row.pinned:
                break
            pinned_count += 1
    pinned = rows[:pinned_count]
    details = rows[pinned_count:]
    overflow = len(rows) > height
    footer = expanded or overflow or selected_key is not None
    max_pinned = max(0, height - int(footer) - int(bool(details)))
    details = pinned[max_pinned:] + details
    pinned = pinned[:max_pinned]
    room = max(0, height - len(pinned) - int(footer))
    scroll = max(0, min(scroll, max(0, len(details) - room)))
    if selected_key is not None:
        target = next((i for i, row in enumerate(details) if row.key == selected_key), None)
        if target is not None and (target < scroll or target >= scroll+room):
            scroll = max(0, target-room+1) if target >= scroll+room else target
    visible = pinned + details[scroll:scroll + room]
    if footer:
        remaining = max(0, len(details) - scroll - room)
        language = (snapshot.get("display") or {}).get("language")
        if selected_key is not None:
            hint = " ↑↓ select · Enter inspect · Esc back to input" if language == "en" else " ↑↓ 選擇 · Enter 檢視 · Esc 返回輸入" if language == "zh-Hant" else " ↑↓ 选择 · Enter 查看 · Esc 返回输入"
        elif language == "en":
            hint = " ↑↓ scroll · Esc back to CLI" if expanded else f" … {remaining} more rows · Ctrl-b H expand"
        elif language == "zh-Hant":
            hint = " ↑↓ 捲動 · Esc 返回 CLI" if expanded else f" … 另有 {remaining} 行 · Ctrl-b H 展開"
        else:
            hint = " ↑↓ 滚动 · Esc 返回 CLI" if expanded else f" … 另有 {remaining} 行 · Ctrl-b H 展开"
        visible += [Row([(hint, "dim")])]
    palette = tuple(sorted(((snapshot.get("display") or {}).get("colors") or {}).items()))
    signature = (height, width, palette, selected_key, tuple((tuple(row.spans), row.key) for row in visible[:height]))
    if frame_cache is not None and frame_cache.get("signature") == signature:
        return scroll
    screen.erase()
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
                if row.key == selected_key and selected_key is not None:
                    attr = curses.A_REVERSE | curses.A_BOLD
                screen.addstr(y, x, rendered, attr)
            except curses.error:
                pass
            x += cell_width(rendered)
            if rendered != text:
                break
    screen.refresh()
    if frame_cache is not None:
        frame_cache["signature"] = signature
    return scroll


def color_index(value):
    if type(value) is int:
        return value
    names = {"red": 203, "green": 114, "yellow": 220, "cyan": 81, "magenta": 141, "white": 255, "blue": 75, "dim": 245}
    if value in names:
        return names[value]
    rgb = tuple(int(value[i:i+2], 16) for i in (1, 3, 5))
    levels = [0, 95, 135, 175, 215, 255]
    candidates = [(16+36*r+6*g+b, (levels[r],levels[g],levels[b])) for r in range(6) for g in range(6) for b in range(6)]
    candidates += [(232+i, (8+10*i,)*3) for i in range(24)]
    return min(candidates, key=lambda entry: sum((a-b)**2 for a,b in zip(rgb, entry[1])))[0]
