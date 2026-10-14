"""Reversible, user-scoped native Windows launcher installation."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import locale
import os
from pathlib import Path
import platform
import shutil
import struct
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SIGNATURE = "# codex-hud automatic launcher v1"
RC_START = "# >>> codex-hud PATH >>>"
RC_END = "# <<< codex-hud PATH <<<"


def settings_dir():
    return Path(os.environ.get("CODEX_HUD_CONFIG_HOME", Path.home() / ".config/codex-hud")).expanduser()


def _json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, value):
    # ASCII JSON can also be read by the standalone launcher's locale decoder.
    path = Path(path)
    pending = path.with_suffix(f".{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=True), encoding="ascii")
    pending.chmod(0o600)
    pending.replace(path)


def is_shim(path):
    path = Path(path)
    if path.is_symlink():
        return False
    try:
        return SIGNATURE in path.read_bytes()[:256].decode("ascii")
    except (OSError, UnicodeError):
        return False


def is_pe(path, machine=None):
    """Require a real PE executable, rather than trusting an .exe extension."""
    try:
        path = Path(path)
        if path.suffix.lower() != ".exe":
            return False
        with path.open("rb") as handle:
            header = handle.read(64)
            if len(header) < 64 or header[:2] != b"MZ":
                return False
            offset = struct.unpack_from("<I", header, 60)[0]
            if offset < 64 or offset > path.stat().st_size - 6:
                return False
            handle.seek(offset)
            pe = handle.read(6)
            actual = struct.unpack("<H", pe[4:])[0]
            return pe[:4] == b"PE\0\0" and actual in ((machine,) if machine else (0x8664, 0xAA64))
    except (OSError, ValueError, struct.error):
        return False


def _target():
    machine = platform.machine().lower()
    if machine in ("arm64", "aarch64"):
        return "arm64", "aarch64-pc-windows-msvc"
    if machine in ("amd64", "x86_64", "x64"):
        return "x64", "x86_64-pc-windows-msvc"
    raise RuntimeError(f"不支持的 Windows 架构：{machine}")


def _matches_optional(package, metadata, optional_name, arch):
    """npm aliases retain the target package's name, not the directory name."""
    dependencies = package.get("optionalDependencies") or {}
    spec = dependencies.get(optional_name) if isinstance(dependencies, dict) else None
    if not isinstance(metadata, dict) or not isinstance(spec, str):
        return False
    name, version = optional_name, spec
    if spec.startswith("npm:"):
        name, separator, version = spec[4:].rpartition("@")
        if not separator or name != "@openai/codex" or version != f"{package.get('version')}-win32-{arch}":
            return False
    if metadata.get("name") != name or metadata.get("version") != version:
        return False
    for key, expected in (("os", "win32"), ("cpu", arch)):
        limits = metadata.get(key)
        if limits is not None and (not isinstance(limits, list) or expected not in limits or "!" + expected in limits):
            return False
    return True


def _package_executable(package_root, entry=None):
    package_root = Path(package_root)
    package = _json(package_root / "package.json", {})
    if not isinstance(package, dict) or package.get("name") != "@openai/codex":
        return None
    bins = package.get("bin", {})
    bin_path = bins.get("codex") if isinstance(bins, dict) else bins
    if not isinstance(bin_path, str):
        return None
    declared = (package_root / bin_path).resolve()
    if not declared.is_file() or not declared.is_relative_to(package_root.resolve()):
        return None
    if entry is not None and declared != Path(entry).resolve():
        return None
    arch, triple = _target()
    # Match the official JS entrypoint's optional-package and vendored layouts.
    optional_name = f"@openai/codex-win32-{arch}"
    vendors = []
    for ancestor in (package_root, *package_root.parents):
        for optional in (ancestor / "node_modules" / optional_name, ancestor / optional_name):
            metadata = _json(optional / "package.json", {})
            if _matches_optional(package, metadata, optional_name, arch):
                vendors.append(optional / "vendor")
    vendors.append(package_root / "vendor")
    for vendor in vendors:
        executable = vendor / triple / "bin/codex.exe"
        if is_pe(executable, 0x8664 if arch == "x64" else 0xAA64):
            return str(executable.resolve())
    return None


def _resolve_candidate(candidate):
    path = Path(candidate).expanduser().absolute()
    if not path.is_file() or is_shim(path):
        return None
    if is_pe(path):
        return str(path.resolve())
    if path.suffix.lower() == ".js":
        return _package_executable(path.parent.parent, path)
    # npm-generated .cmd/.ps1 shims point into this prefix. Do not execute them.
    if path.suffix.lower() in (".cmd", ".ps1") or path.name.lower() == "codex":
        try:
            text = path.read_text(encoding="utf-8-sig").replace("\\", "/")
        except (OSError, UnicodeError):
            return None
        package = path.parent / "node_modules/@openai/codex"
        metadata = _json(package / "package.json", {})
        bins = metadata.get("bin", {}) if isinstance(metadata, dict) else {}
        declared = bins.get("codex") if isinstance(bins, dict) else bins
        if (isinstance(declared, str)
                and ("node_modules/@openai/codex/" + declared.replace("\\", "/")) in text):
            return _package_executable(package)
    return None


def resolve_codex(preferred=None):
    """Return a verified native PE path, never a third-party/npm shell wrapper."""
    explicit = os.environ.get("CODEX_HUD_REAL_CLI")
    current = _json(settings_dir() / "settings.json", {})
    saved = current.get("real_cli") if isinstance(current, dict) else None
    candidates = [explicit, preferred, saved]
    candidates.extend(str(Path(part) / name) for part in os.get_exec_path()
                      for name in ("codex.exe", "codex.cmd", "codex.ps1", "codex"))
    visited = set()
    for candidate in candidates:
        if not candidate or str(candidate) in visited:
            continue
        visited.add(str(candidate))
        resolved = _resolve_candidate(candidate)
        if resolved:
            return resolved
        if candidate == explicit or Path(candidate).is_file() and not is_shim(candidate):
            raise RuntimeError(f"{candidate} 不是可验证的官方 Windows CLI；请用 CODEX_HUD_REAL_CLI 指定官方 codex.exe 或 npm 入口")
    raise RuntimeError("找不到官方 Windows codex.exe；请先安装官方 Codex CLI")


def _b64(value):
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def wrapper_text(real, bootstrap, python_executable=None):
    # Keep the batch source ASCII even when the user's home contains Unicode.
    # Node spawns the exact installation interpreter; never invoke PATH's
    # python.cmd/py.cmd or Windows application aliases. %* expands only once.
    python_executable = python_executable or sys.executable
    code = ("import base64,os,runpy,subprocess,sys;"
            f"real=base64.b64decode('{_b64(real)}').decode('utf-8');"
            f"bootstrap=base64.b64decode('{_b64(bootstrap)}').decode('utf-8');"
            "os.environ['CODEX_HUD_FALLBACK_CLI']=real;"
            "runpy.run_path(bootstrap,run_name='__main__') if os.path.isfile(bootstrap) "
            "else sys.exit(subprocess.call([real,*sys.argv[1:]]))")
    warning = "Codex HUD: 安装时的 Python 不可用或低于 3.11；请重新运行 hud setup。本次使用官方 CLI，不显示 HUD。"
    launcher = ("const spawn=require('child_process').spawnSync;"
                "const decode=s=>Buffer.from(s,'base64').toString('utf8');"
                f"const real=decode('{_b64(real)}'),python=decode('{_b64(python_executable)}'),code=decode('{_b64(code)}');"
                "const args=process.argv.slice(1);"
                "const finish=r=>{if(r.error)console.error(r.error.message);process.exit(r.status===null?127:r.status);};"
                f"const fallback=()=>{{console.error(decode('{_b64(warning)}')+' '+python);finish(spawn(real,args,{{stdio:'inherit'}}));}};"
                "const probe=spawn(python,['-c','import sys;sys.exit(sys.version_info < (3,11))'],{stdio:'ignore',windowsHide:true,timeout:5000});"
                "if(probe.error||probe.status!==0)fallback();"
                "const run=spawn(python,['-c',code,...args],{stdio:'inherit'});"
                "if(run.error)fallback();else finish(run);")
    return (f"@echo off\r\nrem {SIGNATURE}\r\nsetlocal DisableDelayedExpansion\r\n"
            f'node -e "{launcher}" -- %*\r\nexit /b %errorlevel%\r\n')


def _powershell():
    hosts = _powershells()
    return hosts[0] if hosts else None


def _powershells():
    hosts = {}
    for names, variable, relative in (
            (("pwsh.exe", "pwsh"), "ProgramFiles", "PowerShell/7/pwsh.exe"),
            (("powershell.exe", "powershell"), "SystemRoot", "System32/WindowsPowerShell/v1.0/powershell.exe")):
        executable = next((found for name in names if (found := shutil.which(name))), None)
        root = os.environ.get(variable)
        if executable is None and root:
            candidate = Path(root) / relative
            if candidate.is_file():
                executable = str(candidate)
        if executable:
            hosts.setdefault(os.path.normcase(executable), executable)
    return list(hosts.values())


def powershell_profiles():
    """Find each installed host's profile without executing either profile."""
    override = os.environ.get("CODEX_HUD_SHELL_RC")
    if override:
        return [Path(override).expanduser().absolute()]
    executables = _powershells()
    if not executables:
        raise RuntimeError("需要 Windows PowerShell 或 PowerShell 7")
    script = ("[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); "
              "[Console]::Write($PROFILE.CurrentUserCurrentHost)")
    profiles = {}
    for executable in executables:
        result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, encoding="utf-8", timeout=15)
        if result.returncode or not result.stdout.strip():
            raise RuntimeError(f"无法在 -NoProfile 模式读取 {executable} 的 profile 路径")
        profile = Path(result.stdout.strip())
        if not profile.is_absolute():
            raise RuntimeError("PowerShell 返回了无效的 profile 路径")
        profiles.setdefault(os.path.normcase(str(profile)), profile)
    return list(profiles.values())


def powershell_profile():
    """Compatibility accessor for the first profile; installation handles all."""
    return powershell_profiles()[0]


def _configured_profiles(current):
    profiles = current.get("profiles")
    if profiles is None:
        profiles = ([{"shell_rc": current["shell_rc"], "rc_block": current.get("rc_block")}]
                    if current.get("shell_rc") else [])
    if not isinstance(profiles, list) or any(
            not isinstance(profile, dict) or not isinstance(profile.get("shell_rc"), str)
            or not profile["shell_rc"] or not Path(profile["shell_rc"]).is_absolute()
            or profile.get("rc_block") is not None and not isinstance(profile["rc_block"], str)
            for profile in profiles):
        raise RuntimeError("PowerShell profile 安装记录无效；保留现有内容，请检查 settings.json")
    return profiles


def _profile_plan(current, wrapper):
    discovered = powershell_profiles()
    previous = _configured_profiles(current)
    by_path = {os.path.normcase(profile["shell_rc"]): profile for profile in previous}
    discovered_paths = {os.path.normcase(str(path)) for path in discovered}
    if os.environ.get("CODEX_HUD_SHELL_RC") and by_path and set(by_path) != discovered_paths:
        raise RuntimeError("请先 --uninstall，再更换 CODEX_HUD_SHELL_RC")
    # Retain recorded hosts even if one shell has since been removed. Uninstall
    # must still be able to remove every block that this installation created.
    paths = {os.path.normcase(profile["shell_rc"]): Path(profile["shell_rc"]) for profile in previous}
    for path in discovered:
        paths.setdefault(os.path.normcase(str(path)), path)
    pending = []
    for identity, path in paths.items():
        text, encoding = _profile_text(path)
        block = by_path.get(identity, {}).get("rc_block")
        if RC_START in text and (not isinstance(block, str) or block not in text):
            raise RuntimeError(f"{path} 的 HUD 标记区块已改变；保留现有内容，请先核对该区块")
        append = RC_START not in text
        if append:
            block = profile_block(wrapper.parent, "\r\n" if "\r\n" in text or not text else "\n")
        pending.append({"shell_rc": str(path), "rc_block": block, "text": text,
                        "encoding": encoding, "append": append,
                        "original_bytes": text.encode(encoding) if path.exists() else None})
    return pending


def _check_profiles_unchanged(profiles):
    for profile in profiles:
        if profile["append"]:
            path = Path(profile["shell_rc"])
            actual = path.read_bytes() if path.exists() else None
            if actual != profile["original_bytes"]:
                raise RuntimeError(f"{path} 在安装期间已改变；保留现有内容，请重新运行 setup")


def _backup_profiles(directory, profiles):
    state_path = directory / "initial-state.json"
    state = _json(state_path, {})
    if not isinstance(state, dict):
        state = {}
    original = state.get("profiles", [])
    by_path = {item["shell_rc"]: item for item in original
               if isinstance(item, dict) and isinstance(item.get("shell_rc"), str)} if isinstance(original, list) else {}
    if state.get("shell_rc") and state["shell_rc"] not in by_path:
        by_path[state["shell_rc"]] = {"shell_rc": state["shell_rc"], "rc_existed": state.get("rc_existed", False)}
    result = []
    for index, profile in enumerate(profiles):
        path = Path(profile["shell_rc"])
        previous = by_path.get(str(path), {})
        existed = previous.get("rc_existed", path.exists())
        digest = hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]
        backup = directory / ("shell-rc.before" if index == 0 else f"shell-rc.{digest}.before")
        if existed and profile["original_bytes"] is not None and not backup.exists():
            backup.write_bytes(profile["original_bytes"])
            backup.chmod(0o600)
        metadata = {"shell_rc": str(path), "rc_existed": bool(existed), "backup": str(backup) if existed else None}
        result.append({**metadata, "rc_block": profile["rc_block"]})
    primary = result[0]
    _save(state_path, {**state, "shell_rc": primary["shell_rc"], "rc_existed": primary["rc_existed"],
                       "profiles": [{key: value for key, value in profile.items() if key != "rc_block"}
                                    for profile in result]})
    return result


def _profile_text(path):
    if not path.exists():
        return "", "utf-8-sig"
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le"), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be"), "utf-16-be"
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig"), "utf-8-sig"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeError:
        return data.decode(locale.getpreferredencoding(False)), locale.getpreferredencoding(False)


def profile_block(directory, newline="\r\n"):
    lines = ["", RC_START,
             f"$codexHudBin = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{_b64(directory)}'))",
             "$env:PATH = $codexHudBin + ';' + (($env:PATH -split ';' | Where-Object { $_ -ne $codexHudBin }) -join ';')",
             "Remove-Variable codexHudBin -ErrorAction SilentlyContinue", RC_END, ""]
    return newline.join(lines)


def _protect_directory(directory):
    directory.chmod(0o700)
    if os.name != "nt":
        return
    executable = _powershell()
    if not executable:
        raise RuntimeError("需要 PowerShell 来保护 profile 备份")
    escaped = str(directory).replace("'", "''")
    script = ("$ErrorActionPreference='Stop'; "
              "$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User; "
              "$acl=New-Object Security.AccessControl.DirectorySecurity; "
              "$acl.SetAccessRuleProtection($true,$false); "
              "$rule=New-Object Security.AccessControl.FileSystemAccessRule($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'); "
              "$acl.AddAccessRule($rule); "
              f"Set-Acl -LiteralPath '{escaped}' -AclObject $acl")
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    result = subprocess.run([executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("无法创建当前用户专用的 profile 备份目录")


def install_native_dependencies(directory, root=None):
    root = Path(root) if root is not None else ROOT
    source = root / "native/package.json"
    if not source.is_file():
        raise RuntimeError("插件缺少 native/package.json，请重新安装完整插件")
    node = shutil.which("node.exe") or shutil.which("node")
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not node or not npm:
        raise RuntimeError("原生 Windows HUD 需要 Node.js 20+（包含 npm）和 Python 3.11+")
    result = subprocess.run([node, "-p", "process.versions.node.split('.')[0]"],
                            capture_output=True, text=True, timeout=15)
    try:
        supported = result.returncode == 0 and int(result.stdout.strip()) >= 20
    except ValueError:
        supported = False
    if not supported:
        raise RuntimeError("原生 Windows HUD 需要 Node.js 20 或更新版本")
    prefix = (Path(directory) / "native_node").absolute()
    prefix.mkdir(parents=True, exist_ok=True)
    manifest = source.read_bytes()
    unchanged = (prefix / "package.json").is_file() and (prefix / "package.json").read_bytes() == manifest
    lock = root / "native/package-lock.json"
    if lock.is_file():
        unchanged = unchanged and (prefix / "package-lock.json").is_file() and (prefix / "package-lock.json").read_bytes() == lock.read_bytes()
    (prefix / "package.json").write_bytes(manifest)
    if lock.is_file():
        (prefix / "package-lock.json").write_bytes(lock.read_bytes())
    if unchanged and all((prefix / "node_modules" / dependency / "package.json").is_file()
                         for dependency in ("@lydell/node-pty", "@xterm/headless")):
        _verify_native_dependencies(node, prefix)
        return prefix
    # Running npm's JS directly avoids cmd.exe quoting/reparsing of user paths.
    npm_cli = Path(npm).parent / "node_modules/npm/bin/npm-cli.js"
    command = [node, str(npm_cli)] if npm_cli.is_file() else [npm]
    if Path(npm).suffix.lower() == ".cmd" and not npm_cli.is_file():
        raise RuntimeError("找不到 npm 的 npm-cli.js；请安装标准 Node.js LTS 发行版")
    # npm must never discover the caller's project/workspace or run globally.
    # Keep cwd and explicit prefix aligned, including paths with spaces/Unicode.
    result = subprocess.run([*command, "--prefix", str(prefix), "--global=false", "--workspaces=false",
                             "install", "--include=optional", "--no-audit", "--no-fund"],
                            cwd=str(prefix), timeout=300)
    if result.returncode:
        raise RuntimeError("原生终端依赖安装失败；现有 Codex 入口和 PowerShell profile 未修改")
    _verify_native_dependencies(node, prefix)
    return prefix


def _verify_native_dependencies(node, prefix):
    code = ("const r=require('module').createRequire(process.argv[1]);"
            "r('@lydell/node-pty');r('@xterm/headless')")
    result = subprocess.run([node, "-e", code, str(Path(prefix) / "package.json")],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        detail = (result.stderr or result.stdout or f"Node 退出码 {result.returncode}").strip()
        raise RuntimeError("原生终端依赖未就绪；启动包装和 profile 未修改：" + detail)


def _plugin_key(home):
    path = Path(home) / "config.toml"
    if path.is_file():
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        return next((key for key in config.get("plugins", {}) if key.startswith("codex-hud@")), None)
    return None


def configure(argv):
    parser = argparse.ArgumentParser(prog="codex-hud setup", description="安装原生 Windows 自动 HUD")
    actions = parser.add_mutually_exclusive_group()
    for name in ("enable", "disable", "status", "uninstall"):
        actions.add_argument("--" + name, action="store_true")
    parser.add_argument("--height", type=int)
    args = parser.parse_args(argv)
    directory = settings_dir()
    settings_path = directory / "settings.json"
    current = _json(settings_path, {})
    if not isinstance(current, dict):
        current = {}
    if args.status:
        print(json.dumps(current or {"enabled": False, "installed": False}, ensure_ascii=True, indent=2))
        return 0
    if args.height is not None and not 6 <= args.height <= 30:
        parser.error("高度需为 6–30")
    if args.uninstall:
        try:
            profiles = _configured_profiles(current)
        except RuntimeError as error:
            parser.error(str(error))
        wrapper = Path(current.get("wrapper", Path.home() / ".local/bin/codex.cmd"))
        if is_shim(wrapper):
            wrapper.unlink()
        for profile in profiles:
            rc = Path(profile["shell_rc"])
            block = profile.get("rc_block")
            if rc.exists() and isinstance(block, str) and block:
                text, encoding = _profile_text(rc)
                if block in text:
                    rc.write_bytes(text.replace(block, "", 1).encode(encoding))
        if settings_path.exists():
            settings_path.unlink()
        print("已移除 Windows 自动 HUD 启动包装；官方 CLI、代理函数和 profile 的其他内容保留")
        return 0
    if args.disable:
        directory.mkdir(parents=True, exist_ok=True)
        _save(settings_path, {**current, "enabled": False})
        print("自动 HUD 已关闭，下一次 codex 启动使用官方 CLI")
        return 0
    try:
        if sys.version_info < (3, 11):
            raise RuntimeError("需要 Python 3.11 或更新版本")
        real = resolve_codex()
        wrapper = Path(os.environ.get("CODEX_HUD_BIN_DIR", Path.home() / ".local/bin")).expanduser().absolute() / "codex.cmd"
        if (wrapper.exists() or wrapper.is_symlink()) and not is_shim(wrapper):
            raise RuntimeError(f"{wrapper} 已有其他启动器，未覆盖；请指定 CODEX_HUD_BIN_DIR")
        if current.get("wrapper") and Path(current["wrapper"]) != wrapper:
            raise RuntimeError("请先 --uninstall，再更换 CODEX_HUD_BIN_DIR")
        pending_profiles = _profile_plan(current, wrapper)
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        key = _plugin_key(home)
        directory.mkdir(parents=True, exist_ok=True)
        _protect_directory(directory)
        native_node = install_native_dependencies(directory)
        _check_profiles_unchanged(pending_profiles)
        profiles = _backup_profiles(directory, pending_profiles)
        bootstrap = directory / "launcher.py"
        bootstrap.write_bytes((ROOT / "codex_hud/bootstrap.py").read_bytes())
        bootstrap.chmod(0o600)
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        if (wrapper.exists() or wrapper.is_symlink()) and not is_shim(wrapper):
            raise RuntimeError(f"{wrapper} 的入口已变化，未覆盖")
        wrapper.write_bytes(wrapper_text(real, bootstrap).encode("ascii"))
        height = args.height if args.height is not None else current.get("height", 11)
        if type(height) is not int or not 6 <= height <= 30:
            height = 11
        cache = ROOT.parent if {"plugins", "cache"}.issubset(ROOT.parts) else None
        primary = profiles[0]
        settings = {"enabled": True, "height": height, "real_cli": real, "wrapper": str(wrapper),
                    "python_executable": sys.executable,
                    "plugin_root": str(ROOT), "cache_plugin_dir": str(cache) if cache else None,
                    "plugin_key": key, "codex_home": str(home), "shell_rc": primary["shell_rc"],
                    "rc_block": primary["rc_block"], "profiles": profiles, "native_node": str(native_node)}
        # Record every owned block before writing profiles, so an interrupted
        # install can always be retried/uninstalled using the full collection.
        _save(settings_path, settings)
        for profile in pending_profiles:
            if profile["append"]:
                rc = Path(profile["shell_rc"])
                rc.parent.mkdir(parents=True, exist_ok=True)
                _check_profiles_unchanged([profile])
                rc.write_bytes((profile["text"] + profile["rc_block"]).encode(profile["encoding"]))
        if args.height is not None:
            from .config import save
            save({"height": args.height})
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.error(str(error))
    print(json.dumps({"ok": True, "enabled": True, "height": height, "wrapper": str(wrapper),
                      "profile": primary["shell_rc"], "profiles": [profile["shell_rc"] for profile in profiles],
                      "plugin": key, "native_node": str(native_node),
                      "effect": "重新打开使用已配置 profile 的 PowerShell / Windows Terminal 后，普通 codex 会自动显示 HUD；非交互命令直接使用官方 CLI"}, ensure_ascii=True))
    return 0
