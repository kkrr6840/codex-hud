from __future__ import annotations

import curses
import os
from pathlib import Path
import time
import unicodedata

LABEL = {"running": "运行", "idle": "待命", "completed": "完成", "failed": "失败", "unknown": "未知"}


def clean(text):
    # No terminal escapes, bidi overrides, or line breaks from tool/model text.
    return " ".join("".join(c for c in str(text) if not unicodedata.category(c).startswith("C") or c in "\n\t").split())


def clip(text, width):
    text = clean(text)
    result, used = "", 0
    for char in text:
        size = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in ("W", "F") else 1)
        if used + size > width:
            return result[:-1] + "…" if result else ""
        result += char
        used += size
    return result


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
        return "耗时?"
    seconds = int(duration)
    return f"{seconds // 3600}h{seconds % 3600 // 60:02}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02}s" if seconds >= 60 else f"{seconds}s"


def lines(snapshot):
    root, agents, offload = snapshot["root"], snapshot["agents"], snapshot["offload"]
    now = snapshot["time"]
    model = root.get("model") or "模型等待运行数据"
    identity = (root.get("id") or "等待本窗口首次发送 / 选择会话")
    status = LABEL.get(root.get("status"), "未知")
    result = [(f" CODEX HUD  {model} {root.get('effort') or ''}  · {status}  · {identity[:36]}  · {time.strftime('%H:%M:%S', time.localtime(now))}", "title")]
    used, window = root.get("context_used"), root.get("context_window")
    if isinstance(used, int) and isinstance(window, int) and window > 0:
        pct = used / window
        bar = "━" * min(12, int(pct * 12)) + "·" * max(0, 12 - int(pct * 12))
        context = f"上下文请求输入(含系统) [{bar}] {pct:.1%}  {number(used)}/{number(window)}"
    else:
        context = f"上下文 ?/{number(window)} (等待 token 事件)"
    usage = root.get("usage") or {}
    result.append((f" {context}  | 累计 token {number(usage.get('total_tokens'))}", "context"))
    result.append((f" 输入 {number(usage.get('input_tokens'))}  缓存 {number(usage.get('cached_input_tokens'))}  输出 {number(usage.get('output_tokens'))}  推理 {number(usage.get('reasoning_output_tokens'))}", "muted"))
    git = snapshot["git"]
    branch = git.get("branch")
    cwd = root.get("cwd") or os.getcwd()
    cwd = cwd.replace(str(Path.home()), "~", 1) if cwd.startswith(str(Path.home())) else cwd
    git_text = f"{branch} · {'干净' if git.get('changes') == 0 else str(git.get('changes')) + ' 处变更'}" if branch else git.get("error") or "非仓库 / 未知"
    result.append((f" Git {git_text}  | {cwd}", "muted"))
    counts = {key: sum(a["status"] == key for a in agents) for key in LABEL}
    count_text = "  ".join(f"{LABEL[k]} {v}" for k, v in counts.items() if v or k in ("running", "completed"))
    result.append((f" 子 agent {len(agents)}  · {count_text}" if snapshot.get("agents_known", True) else " 子 agent ? · 等待关系数据", "title"))
    for agent in agents:
        name = agent["name"].removeprefix("/root/")
        result.append((f"  {LABEL.get(agent['status'], '未知'):2}  {name}  [{elapsed(agent, now)}]  {clean(agent['task'])}", agent["status"]))
    enabled = "ON" if offload["enabled"] is True else "OFF" if offload["enabled"] is False else "未知"
    jobs = offload["jobs"]
    job_count = len(jobs) if root.get("id") else "?"
    result.append((f" codex-x-codex {enabled} · {offload['model']}  | 本会话分流 {job_count}  (含其子 agent)", "title"))
    totals = {}
    unknown = 0
    for job in jobs:
        usage = job.get("usage")
        if job["status"] in ("completed", "failed"):
            if usage is None:
                unknown += 1
            else:
                for k, v in usage.items():
                    if isinstance(v, int) and v >= 0:
                        totals[k] = totals.get(k, 0) + v
    if jobs:
        result.append((f" 分流已返回用量：输入 {number(totals.get('input_tokens'))}  缓存 {number(totals.get('cached_input_tokens'))}  输出 {number(totals.get('output_tokens'))}  未知用量 {unknown} 项", "muted"))
        for job in jobs:
            usage = job.get("usage")
            detail = f"I {number(usage.get('input_tokens'))} C {number(usage.get('cached_input_tokens'))} O {number(usage.get('output_tokens'))}" if usage else "用量未知"
            result.append((f"  {LABEL.get(job['status'], '未知')} {job['model']} [{elapsed(job, now)}] {detail} · {clean(job['task'])}", job["status"]))
    for error in snapshot["errors"]:
        result.append((f" ! {error}", "failed"))
    return result


def paint(screen, snapshot, scroll=0):
    height, width = screen.getmaxyx()
    data = lines(snapshot)
    screen.erase()
    # Core telemetry stays fixed while agent and offload details scroll.
    header = min(4, max(1, height - 3))
    room = max(0, height - header - 1)
    scroll = max(0, min(scroll, max(0, len(data) - header - room)))
    visible = data[:header] + data[header + scroll:header + scroll + room]
    colors = {"title": 1, "context": 2, "running": 2, "completed": 3, "failed": 4, "unknown": 5, "idle": 5, "muted": 5}
    for y, (text, kind) in enumerate(visible):
        try:
            attr = curses.color_pair(colors.get(kind, 5))
            if kind == "title":
                attr |= curses.A_BOLD
            screen.addstr(y, 0, clip(text, max(0, width - 1)), attr)
        except curses.error:
            pass
    remaining = max(0, len(data) - header - room - scroll)
    footer = f" Ctrl-b H 展开/收起 · ↑↓/j/k 滚动 · Esc 回到 CLI · CLI /quit 退出"
    if remaining:
        footer += f" · 下方还有 {remaining} 行"
    try:
        screen.addstr(height - 1, 0, clip(footer, width - 1), curses.color_pair(5))
    except curses.error:
        pass
    screen.refresh()
    return scroll
