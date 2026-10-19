"""Prepare scoped user requests; never signal workers or submit a CLI turn."""
from __future__ import annotations

import json
import re
import subprocess
import uuid
from pathlib import Path

from .sources import Diagnostic, read_json


def stamp(item):
    return [item.get(key) for key in ("turn_id", "started", "ended", "status")]


def actions(item):
    if item.get("kind") not in ("native", "relay"):
        return []
    if item.get("status") == "running":
        return ["stop"]
    if item.get("status") in ("completed", "failed") or (item.get("status") in ("unknown", "idle") and item.get("ended") is not None):
        return ["retry"]
    return []


def request_text(snapshot, root, key, action, expected):
    from .agents import entries
    if not root or (snapshot.get("root") or {}).get("id") != root:
        raise ValueError("会话已切换，请重新选择任务 / conversation changed")
    item = next((item for item in entries(snapshot) if item["key"] == tuple(key)), None)
    if item is None or item.get("id") == root or action not in actions(item):
        raise ValueError("当前任务不支持此操作，请刷新后重试 / action unavailable")
    if stamp(item) != expected:
        raise ValueError("任务状态已变化，请重新确认 / task changed")
    identity = item["id"]
    if not isinstance(identity, str) or not re.fullmatch(r"[\w:.\-]{1,200}", identity):
        raise ValueError("任务标识不可验证 / invalid task identity")
    target = {"root": root, "kind": item["kind"], "id": identity}
    for key in ("thread", "turn_id", "started"):
        if item.get(key) is not None:
            target[key] = item[key]
    if snapshot.get("display", {}).get("language") == "en":
        operation = "stop this subtask's current execution while retaining its history and existing file changes" if action == "stop" else "check the previous result and file changes, then retry this subtask within its original scope, permissions and model route"
        return ("Codex HUD task request: verify that this target belongs to the current main conversation, then " + operation +
                ". Handle only this subtask and preserve the main conversation and other tasks. If the target or operation cannot be verified, explain why. Report the actual outcome. Target: " +
                json.dumps(target, ensure_ascii=True, separators=(",", ":")))
    operation = "停止该子任务当前的执行，保留任务记录与已产生的文件改动" if action == "stop" else "核对上次结果和已有改动，在原任务范围、权限及模型路由下重试该子任务"
    return ("Codex HUD 子任务管理请求：请先核对以下目标确实属于当前主会话，再" + operation +
            "；只处理指定子任务，保留主会话及其他任务；如无法确认目标或执行操作，请说明原因，完成后报告实际结果。目标：" +
            json.dumps(target, ensure_ascii=True, separators=(",", ":")))


def stage_request(runtime, root, key, action, expected):
    """Place one line in an empty composer, without pressing Enter."""
    from .cli import tmux
    from .keys import clear, empty_composer
    runtime = Path(runtime)
    spec = read_json(runtime / "spec.json", {})
    snapshot = read_json(runtime / "snapshot.json", {})
    if spec.get("socket") != runtime.name or not spec.get("top"):
        raise ValueError("HUD 运行信息无效 / invalid HUD runtime")
    actual = Diagnostic(spec["diagnostic"]).bind(spec["home"]) if spec.get("diagnostic") else None
    if actual is None or actual[0] != root:
        raise ValueError("无法确认当前会话，请刷新 HUD 后重试 / current conversation cannot be verified")
    draft = request_text(snapshot, root, key, action, expected)
    fields = tmux(spec["socket"], "display-message", "-p", "-t", spec["top"],
                  "#{pane_in_mode} #{cursor_x} #{cursor_y} #{cursor_flag}").stdout.split()
    if len(fields) != 4:
        raise ValueError("无法核对输入框 / composer unavailable")
    line = tmux(spec["socket"], "capture-pane", "-p", "-e", "-t", spec["top"], "-S", fields[2], "-E", fields[2]).stdout
    if not empty_composer(line, int(fields[1]), fields[3] == "1", fields[0] != "0"):
        raise ValueError("输入框已有草稿或菜单，请先返回清空后再试 / composer is not empty")
    buffer = "hud-request-" + uuid.uuid4().hex
    try:
        subprocess.run(["tmux", "-L", spec["socket"], "load-buffer", "-b", buffer, "-"], input=draft,
                       text=True, capture_output=True, check=True, timeout=3)
        tmux(spec["socket"], "paste-buffer", "-p", "-d", "-b", buffer, "-t", spec["top"])
    finally:
        tmux(spec["socket"], "delete-buffer", "-b", buffer, check=False)
    clear(runtime, spec)
    tmux(spec["socket"], "select-pane", "-t", spec["top"])
    return draft
