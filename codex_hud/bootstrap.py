#!/usr/bin/env python3
"""Standalone launcher copied to user settings; no dependency on plugin files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib


def read_settings(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def installed_root(cfg, real):
    root = Path(cfg.get("plugin_root") or "/nonexistent")
    parent = cfg.get("cache_plugin_dir")
    key = cfg.get("plugin_key")
    if not parent or not key:
        return root
    # The native manager is the authority for installed versions, including
    # downgrades and prereleases. Never guess from the largest cached folder.
    try:
        result = subprocess.run([real, "plugin", "list", "--json"],
                                capture_output=True, text=True, encoding="utf-8", timeout=5)
        if result.returncode:
            return None
        for plugin in json.loads(result.stdout).get("installed", []):
            if plugin.get("pluginId") == key and plugin.get("enabled") is True:
                version = plugin.get("version")
                if isinstance(version, str) and version not in ("", ".", "..") and Path(version).name == version:
                    candidate = Path(parent) / version
                    if (candidate / "codex_hud/integration.py").is_file():
                        return candidate
    except (OSError, ValueError, TypeError, AttributeError, subprocess.SubprocessError):
        pass
    return None


def enabled(cfg):
    if cfg.get("enabled") is not True:
        return False
    key = cfg.get("plugin_key")
    if not key:
        return True
    try:
        home = Path(os.environ.get("CODEX_HOME", cfg["codex_home"]))
        plugin = tomllib.loads((home / "config.toml").read_text(encoding="utf-8")).get("plugins", {}).get(key, {})
        return isinstance(plugin, dict) and plugin.get("enabled") is True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def main():
    directory = Path(os.environ.get("CODEX_HUD_CONFIG_HOME", Path.home() / ".config/codex-hud"))
    cfg = read_settings(directory / "settings.json")
    real = os.environ.get("CODEX_HUD_FALLBACK_CLI") or cfg.get("real_cli")
    if not isinstance(real, str) or not Path(real).is_file() or not os.access(real, os.X_OK):
        print("codex-hud: 官方 Codex 路径不可用，请重新配置插件", file=sys.stderr)
        return 127
    if os.environ.get("CODEX_HUD_ROUTE_PID") == str(os.getpid()):
        print("codex-hud: 检测到其他启动器递归调用 codex，请配置官方 CLI 路径", file=sys.stderr)
        return 126
    if enabled(cfg) and sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("CODEX_HUD_ACTIVE"):
        try:
            root = installed_root(cfg, real)
        except (TypeError, ValueError):
            root = None
        if root and (root / "codex_hud/integration.py").is_file():
            try:
                sys.path.insert(0, str(root))
                from codex_hud.integration import dispatch
            except (ImportError, OSError, SyntaxError):
                pass  # An incomplete upgrade must not block the official CLI.
            else:
                return dispatch(sys.argv[1:], real, cfg)
    os.environ["CODEX_HUD_ROUTE_PID"] = str(os.getpid())
    if os.name == "nt":
        return subprocess.call([real, *sys.argv[1:]])
    os.execv(real, [real, *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
