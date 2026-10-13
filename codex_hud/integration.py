"""One-time, reversible automatic launch integration for the native plugin."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tomllib

from .sources import atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]
SIGNATURE = "# codex-hud automatic launcher v1"
RC_START = "# >>> codex-hud PATH >>>"
RC_END = "# <<< codex-hud PATH <<<"
NONINTERACTIVE = {"exec", "e", "review", "login", "logout", "mcp", "plugin", "completion", "update", "doctor", "sandbox", "debug", "apply", "queue", "archive", "delete", "migrate-rollouts", "unarchive", "cloud", "exec-server", "app-server", "remote-control", "features", "help", "app", "agents"}
VALUES = {"-c", "--config", "-i", "--image", "-m", "--model", "-p", "--profile", "-C", "--cd", "-s", "--sandbox", "-a", "--ask-for-approval", "--add-dir", "--remote-auth-token-env", "--local-provider", "--enable", "--disable", "-P", "--permission-profile"}
FLAGS = {"--strict-config", "--oss", "--search", "--no-alt-screen", "--no-daemon", "--worktree", "--approve-for-me", "--dangerously-bypass-approvals-and-sandbox", "--yolo", "--dangerously-bypass-hook-trust"}


def interactive(argv):
    """Recognize the installed CLI grammar; ambiguous/future flags bypass HUD."""
    index = 0
    positional = None
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            break  # Everything after the delimiter is positional, even -h.
        if arg in ("-h", "--help", "-V", "--version"):
            return False
        key = arg.split("=", 1)[0]
        if key in VALUES:
            if "=" not in arg:
                index += 1
                if index >= len(argv):
                    return False  # Let the official parser report a missing value.
        elif arg == "--remote" or arg.startswith("--remote="):
            return False  # Local telemetry cannot observe a remote workspace.
        elif arg.startswith("-"):
            if arg not in FLAGS and not (positional in ("resume", "fork") and arg in ("--last", "--all")):
                return False
        else:
            if positional is None:
                positional = arg
                if arg in NONINTERACTIVE:
                    return False
        index += 1
    return True


def settings_dir():
    return Path(os.environ.get("CODEX_HUD_CONFIG_HOME", Path.home() / ".config/codex-hud")).expanduser()


def is_shim(path):
    try:
        with Path(path).open() as handle:
            return SIGNATURE in handle.read(256)
    except (OSError, UnicodeError):
        return False


def resolve_codex(preferred=None):
    candidates = [preferred or os.environ.get("CODEX_HUD_REAL_CLI")]
    cfg = read_json(settings_dir() / "settings.json", {})
    if not isinstance(cfg, dict):
        cfg = {}
    candidates.append(cfg.get("real_cli"))
    candidates.extend(str(Path(part) / "codex") for part in os.get_exec_path())
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK) and not is_shim(candidate):
            path = Path(candidate).absolute()
            if candidate == os.environ.get("CODEX_HUD_REAL_CLI"):
                return str(path)  # Explicitly selected CLI; also used by PTY fixtures.
            with path.open("rb") as handle:
                magic = handle.read(4)
            package = read_json(path.resolve().parents[1] / "package.json", {})
            if magic in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca") or (isinstance(package, dict) and package.get("name") == "@openai/codex"):
                return str(path)
            raise RuntimeError(f"{path} 是其他启动器；请通过 CODEX_HUD_REAL_CLI 指定官方 CLI 的路径后配置")
    raise RuntimeError("找不到官方 codex CLI；请先安装官方 CLI")


def plugin_key(home):
    path = Path(home) / "config.toml"
    if path.exists():
        config = tomllib.loads(path.read_text())
        for key in config.get("plugins", {}):
            if key.startswith("codex-hud@"):
                return key
    return None


def dispatch(argv, real_cli, settings):
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("CODEX_HUD_ACTIVE") or not interactive(argv):
        os.environ["CODEX_HUD_ROUTE_PID"] = str(os.getpid())
        os.execv(real_cli, [real_cli, *argv])
    from .cli import launch
    os.environ["CODEX_HUD_REAL_CLI"] = real_cli
    os.environ["CODEX_HUD_AUTOLAUNCH"] = "1"
    height = settings.get("height", 11)
    if type(height) is not int or not 6 <= height <= 30:
        height = 11
    return launch(["--hud-height", str(height), "--", *argv])


def wrapper_text(real, bootstrap):
    # A stable shell entry keeps the official CLI usable if Python is upgraded,
    # removed, or temporarily resolves to an incompatible virtual environment.
    return ("#!/bin/sh\n" + SIGNATURE + "\n"
            + "export CODEX_HUD_FALLBACK_CLI=" + shlex.quote(real) + "\n"
            + "if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then\n"
            + "  if [ -f " + shlex.quote(str(bootstrap)) + " ]; then\n"
            + "    exec python3 " + shlex.quote(str(bootstrap)) + ' "$@"\n  fi\nfi\n'
            + "exec " + shlex.quote(real) + ' "$@"\n')


def configure(argv):
    parser = argparse.ArgumentParser(prog="codex-hud setup", description="安装或配置一次自动 HUD；普通 codex 交互启动后自动显示")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--enable", action="store_true")
    action.add_argument("--disable", action="store_true")
    action.add_argument("--status", action="store_true")
    action.add_argument("--uninstall", action="store_true", help="移除本插件的启动包装，不卸载官方 CLI")
    parser.add_argument("--height", type=int, help="HUD 最大行数；内容少时自动收缩")
    args = parser.parse_args(argv)
    directory = settings_dir()
    settings_path = directory / "settings.json"
    current = read_json(settings_path, {})
    if not isinstance(current, dict):
        current = {}
    if args.status:
        print(json.dumps(current or {"enabled": False, "installed": False}, ensure_ascii=False, indent=2))
        return 0
    if args.height is not None and not 6 <= args.height <= 30:
        parser.error("高度需为 6–30")
    if args.uninstall:
        wrapper = Path(current.get("wrapper", Path.home() / ".local/bin/codex"))
        if is_shim(wrapper):
            wrapper.unlink()
        rc = Path(current.get("shell_rc", Path.home() / ".zshrc"))
        if rc.exists():
            text = rc.read_text()
            block = current.get("rc_block")
            if block and block in text:
                rc.write_text(text.replace(block, "", 1))
        if settings_path.exists():
            settings_path.unlink()
        print("已移除自动 HUD 启动包装；官方 CLI 和已有 shell 函数保留")
        return 0
    if args.disable:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_json(settings_path, {**current, "enabled": False})
        print("自动 HUD 已关闭，下一次 codex 启动使用官方 CLI")
        return 0
    if not shutil.which("tmux"):
        parser.error("需要 tmux；macOS 可通过 Homebrew 安装 tmux")
    try:
        real = resolve_codex(current.get("real_cli"))
    except RuntimeError as error:
        parser.error(str(error))
    wrapper = Path.home() / ".local/bin/codex"
    if os.environ.get("CODEX_HUD_BIN_DIR"):
        wrapper = Path(os.environ["CODEX_HUD_BIN_DIR"]) / "codex"
    if (wrapper.exists() or wrapper.is_symlink()) and not is_shim(wrapper):
        parser.error(f"{wrapper} 已有其他启动器，未覆盖；请为 HUD 指定 CODEX_HUD_BIN_DIR")
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
    key = plugin_key(home)
    rc_root = Path(os.environ.get("ZDOTDIR", Path.home()))
    shell_rc = rc_root / ".zshrc" if Path(os.environ.get("SHELL", "/bin/zsh")).name == "zsh" else Path.home() / ".bashrc"
    if os.environ.get("CODEX_HUD_SHELL_RC"):
        shell_rc = Path(os.environ["CODEX_HUD_SHELL_RC"])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (directory / "initial-state.json").exists():
        atomic_json(directory / "initial-state.json", {"shell_rc": str(shell_rc), "rc_existed": shell_rc.exists()})
        if shell_rc.exists():
            backup = directory / "shell-rc.before"
            backup.write_bytes(shell_rc.read_bytes())
            backup.chmod(0o600)
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    bootstrap = directory / "launcher.py"
    bootstrap.write_bytes((ROOT / "codex_hud/bootstrap.py").read_bytes())
    bootstrap.chmod(0o600)
    wrapper.write_text(wrapper_text(real, bootstrap))
    wrapper.chmod(0o755)
    block = "\n" + RC_START + "\nexport PATH=" + shlex.quote(str(wrapper.parent)) + ':"$PATH"\n' + RC_END + "\n"
    text = shell_rc.read_text() if shell_rc.exists() else ""
    if RC_START not in text:
        shell_rc.parent.mkdir(parents=True, exist_ok=True)
        shell_rc.write_text(text + block)
    else:
        block = current.get("rc_block", block)
    height = args.height if args.height is not None else current.get("height", 11)
    cache = ROOT.parent if "/plugins/cache/" in str(ROOT) else None
    settings = {"enabled": True, "height": height, "real_cli": real, "wrapper": str(wrapper),
                "plugin_root": str(ROOT), "cache_plugin_dir": str(cache) if cache else None,
                "plugin_key": key, "codex_home": str(home), "shell_rc": str(shell_rc), "rc_block": block}
    atomic_json(settings_path, settings)
    if args.height is not None:
        from .config import save
        save({"height": args.height})
    print(json.dumps({"ok": True, "enabled": True, "height": height, "wrapper": str(wrapper),
                      "plugin": key, "effect": "新 shell 里的普通 codex 会自动显示 HUD；非交互命令保持原样"}, ensure_ascii=False))
    return 0
