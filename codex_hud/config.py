"""Persistent display preferences, shared by the skill, CLI and live panel."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re

ELEMENTS = {
    "model": "模型", "effort": "推理力度", "status": "运行状态", "turn_duration": "本轮耗时（含等待）",
    "project": "项目路径", "git": "Git", "provider": "提供商",
    "session_name": "会话名称", "version": "CLI 版本", "context": "上下文",
    "quota_5h": "5 小时额度", "quota_weekly": "每周额度", "quota_other": "其他额度窗口",
    "tokens": "Token 明细", "cache": "缓存命中率", "tools": "工具活动",
    "mcp": "MCP 活动", "skills": "本轮已读 Skills", "agents": "子 agent", "todos": "计划进度",
    "duration": "会话时长", "session_start": "开始时间", "last_response": "最近响应",
    "compactions": "压缩次数", "environment": "配置数量", "memory": "系统内存",
    "offload": "Codex 分流", "custom": "自定义文字",
    "added_dirs": "额外工作目录", "auth_plan": "订阅计划",
}
SECTIONS = ["header", "context", "quota", "tokens", "cache", "skills", "tools", "mcp", "agents", "todos", "session", "environment", "memory", "offload", "custom"]
DEFAULT = {
    "language": "zh-Hans", "layout": "expanded", "separators": True,
    "height": 11, "max_width": 0, "path_levels": 2,
    "show": {key: key in {"model", "effort", "status", "turn_duration", "project", "git", "context", "quota_5h", "quota_weekly", "quota_other", "tokens", "cache", "skills", "tools", "mcp", "agents", "todos", "offload"} for key in ELEMENTS},
    "order": SECTIONS, "header_order": ["model", "status", "turn_duration", "project", "git", "provider", "session_name", "version"],
    "merge_groups": [["context", "quota"], ["tokens", "cache"]], "right_align": [],
    "context_value": "both", "context_bar": True, "context_window": None,
    "quota_style": "bar", "quota_value": "remaining", "reset_format": "both",
    "cache_style": "text", "skills_limit": 3,
    "reset_label": True, "clock_24h": True, "clock_seconds": False,
    "usage_pace": False, "usage_threshold": 0, "weekly_threshold": 0,
    "warning_percent": 70, "critical_percent": 90,
    "tools_limit": 4, "agents_limit": 4, "agents_show_completed": False, "agents_show_failed": False, "tool_name_length": 32,
    "git": {"dirty": True, "ahead_behind": False, "file_stats": False, "worktree": False, "wrap": False, "push_warning": 0, "push_critical": 0},
    "model_format": "full", "effort_format": "text", "model_override": "", "provider_name": "",
    "custom_text": "", "custom_position": "last",
    "colors": {"model": 81, "good": 114, "warn": 220, "bad": 203, "muted": 245, "agent": 141, "dim": 238},
    "bar_filled": "█", "bar_empty": "░",
}
UNAVAILABLE = {
    "cost / daily_cost / weekly_cost": "Codex 会话未提供实际费用，订阅额度不等于美元费用；不按 token 猜账单。",
    "speed": "当前日志没有可靠的单次 API 耗时；整轮时间含工具与等待，不能冒充生成 tok/s。",
    "prompt_cache_expiry": "Codex 未提供缓存到期时间；支持真实缓存 token 和命中率。",
    "active_skills": "显示本轮成功读取的 SKILL.md 名称，不代表持续运行、已安装数量或全部可用技能；未被记录的读取无法显示。",
    "advisor / output_style / auth_user": "Claude 专有字段或当前日志未提供；不读取登录凭据来补齐。",
    "external_usage / extra_command": "不复用 Claude 账号额度或执行外部状态命令；额度来自当前 Codex 会话。",
    "jj": "本版实现 Git；Jujutsu 的工作副本与冲突状态未实现。",
}


def path():
    return Path(os.environ.get("CODEX_HUD_CONFIG_HOME", Path.home() / ".config/codex-hud")) / "display.json"


def merge(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def read(pathname):
    try:
        if pathname.stat().st_size > 65536:
            return {}, "显示配置超过 64 KB"
        value = json.loads(pathname.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("顶层必须是 JSON 对象")
        return value, None
    except FileNotFoundError:
        return {}, None
    except (OSError, ValueError) as error:
        return {}, f"显示配置: {error}"


def validate(cfg):
    enums = {"language": ["zh-Hans", "zh-Hant", "en"], "layout": ["expanded", "compact"],
             "context_value": ["percent", "tokens", "remaining", "both"], "quota_style": ["bar", "text", "compact"], "cache_style": ["bar", "text"],
             "quota_value": ["used", "remaining"], "reset_format": ["relative", "absolute", "both", "elapsed", "elapsed_absolute"],
             "model_format": ["full", "short"], "effort_format": ["text", "symbol", "both"], "custom_position": ["first", "last"]}
    for key, values in enums.items():
        if cfg[key] not in values:
            raise ValueError(f"{key} 需为 {', '.join(values)}")
    for key, lo, hi in [("height", 1, 30), ("max_width", 0, 500), ("path_levels", 0, 10), ("tools_limit", 0, 30), ("agents_limit", 0, 30), ("skills_limit", 0, 30), ("tool_name_length", 0, 120), ("warning_percent", 0, 100), ("critical_percent", 0, 100), ("usage_threshold", 0, 100), ("weekly_threshold", 0, 100)]:
        if type(cfg[key]) is not int or not lo <= cfg[key] <= hi:
            raise ValueError(f"{key} 需为 {lo}–{hi} 的整数")
    if cfg["warning_percent"] > cfg["critical_percent"]:
        raise ValueError("警告阈值不能大于严重阈值")
    if cfg["context_window"] is not None and (type(cfg["context_window"]) is not int or cfg["context_window"] <= 0):
        raise ValueError("context_window 需为正整数或 null")
    for group in (None, "show", "git"):
        actual, defaults = (cfg, DEFAULT) if group is None else (cfg[group], DEFAULT[group])
        if not isinstance(actual, dict):
            raise ValueError(f"{group} 需为对象")
        for key, value in defaults.items():
            if isinstance(value, bool) and type(actual[key]) is not bool:
                raise ValueError(f"{group or ''}.{key} 需为 true 或 false")
    for key in ("push_warning", "push_critical"):
        if type(cfg["git"][key]) is not int or cfg["git"][key] < 0:
            raise ValueError(f"git.{key} 需为非负整数")
    for key, allowed in [("order", SECTIONS), ("header_order", DEFAULT["header_order"]), ("right_align", SECTIONS)]:
        if not isinstance(cfg[key], list) or any(type(x) is not str or x not in allowed for x in cfg[key]) or len(set(cfg[key])) != len(cfg[key]):
            raise ValueError(f"{key} 含无效或重复项目")
    groups = cfg["merge_groups"]
    if not isinstance(groups, list) or len(groups) > 14 or any(not isinstance(g, list) or any(type(x) is not str or x not in SECTIONS for x in g) for g in groups):
        raise ValueError("merge_groups 需为显示区域名称的二维列表")
    for key in ("model_override", "provider_name", "custom_text"):
        if not isinstance(cfg[key], str) or len(cfg[key]) > 80:
            raise ValueError(f"{key} 需为不超过 80 字的文本")
    from .display import cell_width, clean
    for key in ("bar_filled", "bar_empty"):
        if not isinstance(cfg[key], str) or len(cfg[key]) != 1 or cell_width(cfg[key]) != 1 or clean(cfg[key]) != cfg[key]:
            raise ValueError(f"{key} 需为单列宽可见字符")
    if not isinstance(cfg["colors"], dict):
        raise ValueError("colors 需为对象")
    for value in cfg["colors"].values():
        if not ((type(value) is int and 0 <= value <= 255) or (isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}|red|green|yellow|cyan|magenta|white|blue|dim", value))):
            raise ValueError("颜色需为 0–255、#rrggbb 或标准色名")


def load(defaults=None):
    base, error = read(path())
    override, other = read(path().with_name("override.json"))
    cfg = merge(merge(merge(DEFAULT, defaults or {}), base), override)
    try:
        validate(cfg)
    except (ValueError, KeyError, TypeError) as invalid:
        cfg, error = deepcopy(DEFAULT), f"显示配置: {invalid}"
    return cfg, error or other


def save(changes):
    from .sources import atomic_json
    base, error = read(path())
    if error:
        raise ValueError(error)
    updated = merge(base, changes)
    validate(merge(DEFAULT, updated))
    path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(path(), updated)


def preset(name):
    if name == "focus":
        visible = {"status", "turn_duration", "git", "context", "quota_5h", "quota_weekly", "quota_other", "tokens", "cache", "skills", "tools", "mcp", "agents", "todos", "offload"}
        return {"show": {key: key in visible for key in ELEMENTS}, "layout": "compact", "cache_style": "text",
                "order": ["header", "skills", "context", "quota", "tokens", "cache", "tools", "mcp", "agents", "todos", "offload", "session", "environment", "memory", "custom"],
                "merge_groups": [["context", "quota"], ["tokens", "cache"], ["header", "skills"]]}
    minimum = {"model", "project", "git", "context", "quota_5h", "quota_weekly"}
    standard = minimum | {"effort", "status", "turn_duration", "cache", "skills", "tools", "mcp", "agents", "todos", "offload"}
    visible = minimum if name == "minimal" else standard if name == "standard" else set(ELEMENTS)
    return {"show": {key: key in visible for key in ELEMENTS}, "layout": "compact" if name == "minimal" else "expanded"}


def configure(argv):
    parser = argparse.ArgumentParser(prog="codex-hud config", description="显示配置；保存后所有 HUD 下次刷新生效")
    parser.add_argument("--show", help="显示项目，逗号分隔")
    parser.add_argument("--hide", help="隐藏项目，逗号分隔")
    parser.add_argument("--preset", choices=["minimal", "standard", "full", "focus"])
    parser.add_argument("--layout", choices=["expanded", "compact"])
    parser.add_argument("--language", choices=["zh-Hans", "zh-Hant", "en"])
    parser.add_argument("--set", action="append", default=[], metavar="KEY=JSON", help="例如 --set 'git.ahead_behind=true'")
    parser.add_argument("--menu", action="store_true", help="交互选择显示项目")
    parser.add_argument("--describe", action="store_true", help="显示项目、完整配置及数据源限制")
    args = parser.parse_args(argv)
    if args.menu:
        if os.name == "nt":
            parser.error("Windows 请在 HUD 中按 Ctrl-b 后 C 或 F8；完整设置可通过 hud skill 修改")
        from .menu import run
        return run()
    if args.describe:
        cfg, error = load()
        print(json.dumps({"path": str(path()), "elements": ELEMENTS, "effective": cfg, "unavailable": UNAVAILABLE, "error": error}, ensure_ascii=False, indent=2))
        return 0
    changes = preset(args.preset) if args.preset else {}
    for action, value in ((True, args.show), (False, args.hide)):
        for key in value.split(",") if value else []:
            if key not in ELEMENTS:
                parser.error(f"未知显示项 {key}；可选 {', '.join(ELEMENTS)}")
            changes.setdefault("show", {})[key] = action
    for key in ("layout", "language"):
        if getattr(args, key):
            changes[key] = getattr(args, key)
    for item in args.set:
        try:
            name, raw = item.split("=", 1)
            keys, cursor, defaults = name.split("."), changes, DEFAULT
            for key in keys[:-1]:
                if not isinstance(defaults.get(key), dict):
                    raise ValueError(f"未知配置 {name}")
                defaults, cursor = defaults[key], cursor.setdefault(key, {})
            if keys[-1] not in defaults:
                raise ValueError(f"未知配置 {name}")
            cursor[keys[-1]] = json.loads(raw)
        except (ValueError, TypeError) as error:
            parser.error(str(error))
    if changes:
        try:
            save(changes)
        except (ValueError, OSError) as error:
            parser.error(str(error))
    cfg, error = load()
    print(json.dumps({"saved": bool(changes), "path": str(path()), "effective": cfg, "error": error,
                      "override": str(path().with_name("override.json")) if path().with_name("override.json").exists() else None}, ensure_ascii=False, indent=2))
    return 0
