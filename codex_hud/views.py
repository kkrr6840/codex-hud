"""Configurable terminal views. Every metric keeps its own source and scope."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import math
from pathlib import Path

from .display import Row, clean, clip, cell_width, number, elapsed, progress, ICONS, STATE_COLOR


class View:
    def __init__(self, snapshot, width, cfg):
        self.s, self.c, self.now = snapshot, cfg, snapshot["time"]
        self.width = min(width, cfg["max_width"] or width)
        self.show, self.root = cfg["show"], snapshot["root"]
        self.sections = {}

    def tr(self, zh, en, hant=None):
        return en if self.c["language"] == "en" else hant or zh if self.c["language"] == "zh-Hant" else zh

    def state(self, state):
        return {"running": self.tr("运行", "running", "執行"), "idle": self.tr("待命", "idle"), "completed": self.tr("完成", "done"), "failed": self.tr("失败", "failed", "失敗"), "unknown": self.tr("未知", "unknown")}.get(state, state)

    def put(self, section, spans, pinned=False, key=None):
        if spans:
            self.sections.setdefault(section, []).append(Row(spans, pinned, key))

    def color(self, percent):
        return "bad" if percent >= self.c["critical_percent"] else "warn" if percent >= self.c["warning_percent"] else "good"

    def bar(self, fraction, color):
        size = 16 if self.width >= 100 else 10 if self.width >= 70 else 6
        if self.c["bar_filled"] == "█":
            filled, empty = progress(fraction, size)
            empty = empty.replace("░", self.c["bar_empty"])
        else:
            count = round(max(0, min(1, fraction)) * size)
            filled, empty = self.c["bar_filled"] * count, self.c["bar_empty"] * (size-count)
        return [(filled, color), (empty, "dim"), (" ", "dim")]

    def reset(self, seconds, minutes):
        if type(seconds) not in (int, float) or not math.isfinite(seconds):
            return ""
        delta = max(0, int(seconds-self.now))
        relative = f"{delta // 86400}d {(delta % 86400) // 3600}h" if delta >= 86400 else f"{delta // 3600}h {(delta % 3600) // 60}m" if delta >= 3600 else f"{delta // 60}m"
        clock = "%H:%M" if self.c["clock_24h"] else "%I:%M %p"
        if self.c["clock_seconds"]:
            clock = clock.replace("%M", "%M:%S")
        try:
            absolute = datetime.fromtimestamp(seconds).strftime("%m/%d " + clock)
        except (OverflowError, OSError, ValueError):
            return ""
        passed = max(0, min(100, 100 * (1 - delta / (minutes*60))))
        mode = self.c["reset_format"]
        value = absolute if mode == "absolute" else f"{relative} · {absolute}" if mode == "both" else f"{passed:.0f}% " + self.tr("已过", "elapsed", "已過") if mode == "elapsed" else f"{passed:.0f}% · {absolute}" if mode == "elapsed_absolute" else relative
        return value + (" " + self.tr("重置", "reset") if self.c["reset_label"] and mode not in ("elapsed", "elapsed_absolute") else "")

    def header(self):
        r, show, cfg, git = self.root, self.show, self.c, self.s["git"]
        started = r.get("started")
        turn_clock = show["turn_duration"] and r.get("status") == "running" and type(started) in (int, float) and math.isfinite(started) and 0 < started <= self.now
        # A directory or optional label alone must not reveal an empty session.
        if not any((r.get("model"), r.get("usage"), r.get("context_used"), git.get("branch"), turn_clock)):
            return
        parts = {}
        if show["model"] and r.get("model"):
            model = cfg["model_override"] or r["model"]
            if cfg["model_format"] == "short":
                model = model.removeprefix("gpt-")
            parts["model"] = [(f"[{clean(model)}]", "model")]
            if show["effort"] and r.get("effort"):
                effort = clean(r["effort"])
                symbol = {"low": "◔", "medium": "◑", "high": "◕", "xhigh": "●", "max": "◆", "ultra": "✦"}.get(effort)
                text = symbol if cfg["effort_format"] == "symbol" and symbol else f"{symbol} {effort}" if cfg["effort_format"] == "both" and symbol else effort
                parts["model"].append((" " + text, "muted"))
        if show["status"] and r.get("status"):
            state = r["status"]
            parts["status"] = [(ICONS.get(state, "?") + " " + self.state(state), STATE_COLOR.get(state, "muted"))]
        if turn_clock:
            seconds = int(self.now-started)
            duration = f"{seconds // 3600}h{seconds % 3600 // 60:02}m{seconds % 60:02}s" if seconds >= 3600 else elapsed({"duration": seconds}, self.now)
            parts["turn_duration"] = [(self.tr("本轮 ", "Turn ", "本輪 ") + duration, "muted")]
        if show["project"] and r.get("cwd"):
            cwd = str(r["cwd"])
            home = str(Path.home())
            if cwd == home or cwd.startswith(home + "/"):
                cwd = "~" + cwd[len(home):]
            if cfg["path_levels"]:
                cwd = "/".join(Path(cwd).parts[-cfg["path_levels"]:])
            parts["project"] = [(clip(cwd, 40 if self.width >= 100 else 22), "muted")]
            if show["added_dirs"]:
                extras = [str(p) for p in r.get("added_dirs", []) if isinstance(p, str) and p != r["cwd"]]
                for extra in extras[:5]:
                    parts["project"].append((" +" + clip(clean(Path(extra).name), 24), "muted"))
                if len(extras) > 5:
                    parts["project"].append((f" +{len(extras)-5}", "dim"))
        if show["git"] and git.get("branch"):
            spans = [("git:" + clip(clean(git["branch"]), 48), "agent")]
            g = cfg["git"]
            if g["dirty"] and git.get("changes"):
                spans.append((" *", "warn"))
            if g["ahead_behind"]:
                ahead, behind = git.get("ahead", 0), git.get("behind", 0)
                color = "bad" if g["push_critical"] and ahead >= g["push_critical"] else "warn" if g["push_warning"] and ahead >= g["push_warning"] else "muted"
                if ahead or behind:
                    spans.append((f" ↑{ahead} ↓{behind}", color))
            if g["file_stats"]:
                spans.append((f" !{git.get('modified', 0)} +{git.get('added', 0)} -{git.get('deleted', 0)} ?{git.get('untracked', 0)}", "muted"))
            if g["worktree"] and git.get("worktree"):
                spans.append((" ⎇ " + clean(git["worktree"]), "muted"))
            parts["git"] = spans
        for key, value in [("provider", cfg["provider_name"] or r.get("provider")), ("session_name", r.get("session_name")), ("version", r.get("cli_version"))]:
            if show[key] and value:
                parts[key] = [(clip(clean(value), 32), "muted")]
        limits = r.get("rate_limits") or {}
        if show["auth_plan"] and isinstance(limits, dict) and limits.get("plan_type"):
            parts.setdefault("provider", []).append((" " + clean(limits["plan_type"]), "muted"))
        order = cfg["header_order"] + [k for k in parts if k not in cfg["header_order"]]
        spans = [(" ", "text")]
        for key in order:
            if key in parts:
                if len(spans) > 1:
                    spans.append((" │ " if cfg["separators"] else "  ", "dim"))
                if key == "git" and cfg["git"]["wrap"] and cell_width("".join(x[0] for x in spans + parts[key])) > self.width-1:
                    self.put("header", spans, True)
                    spans = [(" ", "text")]
                spans.extend(parts[key])
        if len(spans) > 1:
            self.put("header", spans, True)

    def context(self):
        used = self.root.get("context_used")
        window = self.c["context_window"] or self.root.get("context_window")
        if not self.show["context"] or type(used) is not int or type(window) is not int or window <= 0:
            return
        percent, mode = used / window * 100, self.c["context_value"]
        label = self.tr("上下文", "Context") + ("*" if self.c["context_window"] else "")
        spans = [(" " + label + " ", "muted")]
        color = self.color(percent)
        if self.c["context_bar"]:
            spans += self.bar(used/window, color)
        value = f"{number(used)} / {number(window)} tokens" if mode == "tokens" else self.tr("剩余 ", "left ", "剩餘 ") + f"{max(0, 100-percent):.0f}%" if mode == "remaining" else f"{percent:.0f}%"
        spans.append((value, color))
        if mode == "both":
            spans.append((f"  {number(used)} / {number(window)} tokens", "muted"))
        self.put("context", spans, True)

    def quota(self):
        limits = self.root.get("rate_limits")
        if not isinstance(limits, dict):
            return
        bucket = limits.get("limit_name") or limits.get("limit_id")
        for key in ("primary", "secondary"):
            q = limits.get(key)
            if not isinstance(q, dict):
                continue
            minutes, used, reset = q.get("window_minutes"), q.get("used_percent"), q.get("resets_at")
            if type(minutes) not in (int, float) or not math.isfinite(minutes) or minutes <= 0 or type(used) not in (int, float) or not math.isfinite(used) or used < 0:
                continue
            name = "quota_5h" if minutes == 300 else "quota_weekly" if minutes == 10080 else "quota_other"
            if not self.show[name] or used < self.c["usage_threshold"] or (minutes == 10080 and used < self.c["weekly_threshold"]):
                continue
            label = self.tr("5小时", "5h", "5小時") if minutes == 300 else self.tr("周额度", "Weekly", "週額度") if minutes == 10080 else f"{minutes:g}m"
            spans = [(" " + label + " ", "muted")]
            if type(reset) in (int, float) and math.isfinite(reset) and reset <= self.now:
                self.put("quota", spans + [(self.tr("等待额度更新", "awaiting quota update", "等待額度更新"), "muted")], True)
                continue
            value = max(0, 100-used) if self.c["quota_value"] == "remaining" else used
            color = self.color(used)
            pace = False
            if self.c["usage_pace"] and type(reset) in (int, float) and math.isfinite(reset):
                passed = max(0, min(1, 1 - (reset-self.now)/(minutes*60)))
                pace = passed > .02 and used / 100 > passed
                if pace and color == "good":
                    color = "warn"
            if self.c["quota_style"] == "bar":
                spans += self.bar(value/100, color)
            prefix = self.tr("剩余 ", "left ", "剩餘 ") if self.c["quota_value"] == "remaining" else self.tr("已用 ", "used ")
            spans.append((prefix + f"{value:g}%" + (" ▲" if pace else ""), color))
            if bucket and bucket != "codex":
                spans.append((" " + clip(clean(bucket), 24), "muted"))
            stamp = self.reset(reset, minutes)
            if stamp:
                spans.append(("  " + stamp, "muted"))
            self.put("quota", spans, True)

    def tokens(self):
        usage = self.root.get("usage") or {}
        if self.show["tokens"] and any(type(x) in (int, float) and x > 0 for x in usage.values()):
            spans = [(" " + self.tr("主会话 Token", "Main tokens", "主會話 Token"), "muted")]
            for key, label in [("input_tokens", "↑"), ("output_tokens", "↓"), ("reasoning_output_tokens", self.tr("推理", "reasoning"))]:
                if type(usage.get(key)) in (int, float) and usage[key] > 0:
                    spans += [(f"  {label} ", "muted"), (number(usage[key]), "text")]
            if type(usage.get("total_tokens")) is int:
                spans += [(" │ " + self.tr("累计 ", "total ", "累計 "), "dim"), (number(usage["total_tokens"]), "text")]
            self.put("tokens", spans)
        cached, inputs = usage.get("cached_input_tokens"), usage.get("input_tokens")
        if self.show["cache"] and type(cached) is int and type(inputs) is int and 0 <= cached <= inputs and inputs > 0:
            spans = [(" " + self.tr("缓存", "Cache", "快取") + " ", "muted")]
            if self.c["cache_style"] == "bar":
                spans += [*self.bar(cached/inputs, "model"), (f"{cached/inputs:.0%}", "model"), (f"  {number(cached)} / {number(inputs)} tokens", "muted")]
            else:
                spans += [(f"{cached/inputs:.0%}", "model")]
            self.put("cache", spans)

    def activity(self):
        skills = self.root.get("skills") or []
        if self.show["skills"] and skills:
            limit = self.c["skills_limit"] or len(skills)
            names = " · ".join(clip(clean(name), 32) for name in skills[:limit])
            if len(skills) > limit:
                names += f" +{len(skills)-limit}"
            self.put("skills", [(" Skills ", "model"), (self.tr("本轮已读 ", "read this turn ", "本輪已讀 "), "muted"), (names, "agent")],
                     key=("skills", self.root.get("skill_turn_id") or self.root.get("id")))
        tools = self.root.get("tools") or []
        if self.show["tools"] and tools:
            active = [t for t in tools if t["status"] == "running"]
            completed = Counter((t["name"], t["status"]) for t in tools if t["status"] != "running")
            spans = [(" " + self.tr("最近工具", "Recent tools") + " ", "model")]
            for tool in active:
                spans.append(("● " + clip(clean(tool["name"]), self.c["tool_name_length"] or 500) + "  ", "good"))
            entries = list(completed.items())
            limit = self.c["tools_limit"] or len(entries)
            for (name, state), count in entries[-limit:]:
                spans.append((f"{ICONS.get(state, '?')} {clip(clean(name), self.c['tool_name_length'] or 500)} ×{count}  ", STATE_COLOR.get(state, "muted")))
            if len(entries) > limit:
                spans.append((f"+{len(entries)-limit}", "dim"))
            self.put("tools", spans)
        if self.show["mcp"]:
            names = sorted({t["name"].split("__")[1] for t in tools if t["name"].startswith("mcp__") and len(t["name"].split("__")) >= 3})
            if names:
                self.put("mcp", [(" MCP ", "model"), (", ".join(map(clean, names)), "muted")])
        todos = self.root.get("todos") or []
        if self.show["todos"] and todos:
            complete = sum(t["status"] == "completed" for t in todos)
            current = next((t["step"] for t in todos if t["status"] == "in_progress"), "")
            self.put("todos", [(" " + self.tr("计划", "Plan", "計畫") + f" {complete}/{len(todos)} ", "model"), (clean(current), "muted")])
        for name, items in [("agents", self.s["agents"]), ("offload", self.s["offload"]["jobs"])]:
            if not self.c["agents_show_completed"]:
                items = [item for item in items if item.get("status") != "completed" and not (item.get("status") == "unknown" and item.get("ended") is not None)]
            if not self.c["agents_show_failed"]:
                items = [item for item in items if item.get("status") != "failed"]
            if not self.show[name] or not items:
                continue
            title = " Agents" if name == "agents" else " codex-x-codex"
            counts = Counter(t["status"] for t in items)
            spans = [(title + " ", "model")]
            for state in ("running", "completed", "idle", "failed", "unknown"):
                if counts[state]:
                    spans.append((f" {counts[state]} {self.state(state)}", STATE_COLOR[state]))
            spans.append((" · ↓ " + self.tr("选择", "select", "選擇"), "dim"))
            self.put(name, spans)
            limit = self.c["agents_limit"] or len(items)
            for item in items[:limit]:
                state = item["status"]
                label = item.get("name") if name == "agents" else item.get("model")
                spans = [(f" {ICONS.get(state, '?')} ", STATE_COLOR.get(state, "muted")), (clean(label or "worker").removeprefix("/root/"), "agent"), (" " + elapsed(item, self.now), "muted")]
                if name == "agents" and item.get("model"):
                    spans.append((" [" + clean(item["model"]) + "]", "muted"))
                usage = item.get("usage")
                if isinstance(usage, dict):
                    for key, arrow in [("input_tokens", "↑"), ("output_tokens", "↓")]:
                        if type(usage.get(key)) is int:
                            spans.append((f" {arrow}{number(usage[key])}", "muted"))
                elif name == "offload" and state != "running":
                    spans.append((" · " + self.tr("未返回用量", "usage unavailable", "未回傳用量"), "muted"))
                if state == "failed" and item.get("error"):
                    error = item["error"]
                    reason = self.tr("连接失败", "connection failed", "連線失敗") if "connect" in error.lower() or "network" in error.lower() else self.tr("任务失败", "task failed", "任務失敗")
                    spans.append((" · " + reason, "bad"))
                task = clean(item.get("task", "")).removesuffix(" (任务名；详细指令加密)")
                if task:
                    spans.append((" · " + task, "muted"))
                self.put(name, spans, key=("native" if name == "agents" else "relay", item.get("id")))
            if len(items) > limit:
                self.put(name, [(f" +{len(items)-limit} " + self.tr("项", "more", "項"), "dim")])

    def session(self):
        r, spans = self.root, []
        if self.show["duration"] and r.get("created_at"):
            spans.append((self.tr("会话 ", "Session ", "會話 ") + elapsed({"duration": max(0, self.now-r["created_at"])}, self.now), "muted"))
        if self.show["session_start"] and r.get("created_at"):
            try:
                spans.append((datetime.fromtimestamp(r["created_at"]).strftime("%m/%d %H:%M"), "muted"))
            except (ValueError, OverflowError, OSError):
                pass
        if self.show["last_response"] and r.get("last_response_at"):
            spans.append((self.tr("最近响应 ", "Last response ", "最近回應 ") + elapsed({"duration": max(0, self.now-r["last_response_at"])}, self.now), "muted"))
        if self.show["compactions"] and r.get("compactions"):
            spans.append((self.tr("压缩 ", "Compactions ", "壓縮 ") + ("≥" if r.get("history_partial") else "") + str(r["compactions"]), "muted"))
        if spans:
            self.put("session", [(" ", "text"), *join_spans(spans)])
        env = self.s.get("environment")
        if self.show["environment"] and env:
            self.put("environment", [(" " + self.tr("配置", "Configured") + f" · MCP {env['mcp']} · Plugins {env['plugins']} · AGENTS.md {env['instructions']}", "muted")])
        mem = self.s.get("memory")
        if self.show["memory"] and mem and mem["total"] > 0:
            self.put("memory", [(" " + self.tr("系统 RAM ", "System RAM ", "系統 RAM "), "muted"), *self.bar(mem["used"]/mem["total"], "model"), (f"{mem['used']/2**30:.1f}/{mem['total']/2**30:.1f} GiB", "muted")])
        if self.show["custom"] and self.c["custom_text"]:
            self.put("custom", [(" " + clean(self.c["custom_text"]), "muted")])

    def render(self):
        self.header(); self.context(); self.quota(); self.tokens(); self.activity(); self.session()
        order = self.c["order"] + [s for s in self.sections if s not in self.c["order"]]
        if "custom" in order and self.c["custom_position"] == "first":
            order.remove("custom"); order.insert(0, "custom")
        result, previous = [], None
        for section in order:
            for row in self.sections.get(section, []):
                same_group = any(previous in group and section in group for group in self.c["merge_groups"])
                merge = result and ((self.c["layout"] == "compact" and section in ("context", "quota", "tokens", "cache", "session")) or same_group or (section == previous == "quota" and self.c["quota_style"] == "compact"))
                gap = " │ " if self.c["separators"] else "  "
                if merge and (not row.key or row.key[0] == "skills") and not result[-1].key and cell_width(result[-1].text + gap + row.text.lstrip()) < self.width:
                    if section in self.c["right_align"]:
                        gap = " " * max(len(gap), self.width-1-cell_width(result[-1].text)-cell_width(row.text.lstrip()))
                    result[-1].spans += [(gap, "dim"), *[(text.lstrip() if i == 0 else text, color) for i, (text, color) in enumerate(row.spans)]]
                    result[-1].key = row.key
                else:
                    result.append(row)
                previous = section
        # A live Main destination precedes every selectable record, regardless
        # of custom section order. An uninitialized HUD stays completely hidden.
        if result and self.root.get("id") and self.show.get("main_agent", True):
            index = next((i for i, row in enumerate(result) if row.key), len(result))
            result.insert(index, Row([(" Main agent", "model"),
                                     (" · " + self.tr("主会话", "live conversation", "主會話"), "muted"),
                                     (" · ↑↓ " + self.tr("切换", "switch", "切換"), "dim")],
                                    key=("main", self.root["id"])))
        for error in self.s.get("errors", []):
            result.append(Row([(" ! " + clean(error), "warn")]))
        if self.show["git"] and self.s["git"].get("error"):
            result.append(Row([(" ! " + clean(self.s["git"]["error"]), "warn")]))
        return result


def join_spans(spans):
    result = []
    for span in spans:
        if result:
            result.append((" │ ", "dim"))
        result.append(span)
    return result


def render(snapshot, width, config):
    return View(snapshot, width, config).render()
