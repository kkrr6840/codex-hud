import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_hud import windows_setup as setup

ROOT = Path(__file__).resolve().parents[1]


def pe_fixture(path, machine=0x8664):
    """Minimal PE header for discovery tests; runtime tests use real python.exe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = bytearray(256)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 60, 128)
    content[128:134] = b"PE\0\0" + struct.pack("<H", machine)
    path.write_bytes(content)
    return path


class WindowsSetupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex HUD 中文 ")
        self.base = Path(self.temp.name)
        self.real = pe_fixture(self.base / "official/codex.exe")
        self.directory = self.base / "settings"
        self.rc = self.base / "Documents/PowerShell/profile.ps1"
        self.rc.parent.mkdir(parents=True)
        self.original = ("$env:HTTP_PROXY = 'http://127.0.0.1:7890'\r\n"
                         "function codex { & (Get-Command codex -CommandType Application) @args }\r\n"
                         "# 保留原有配置\r\n").encode("utf-8-sig")
        self.rc.write_bytes(self.original)
        self.env = {"CODEX_HUD_CONFIG_HOME": str(self.directory),
                    "CODEX_HUD_BIN_DIR": str(self.base / "local bin"),
                    "CODEX_HUD_SHELL_RC": str(self.rc), "CODEX_HUD_REAL_CLI": str(self.real),
                    "CODEX_HOME": str(self.base / "codex-home")}
        self.environ = patch.dict(os.environ, self.env)
        self.environ.start()
        self.arch = patch.object(setup, "_target", return_value=("x64", "x86_64-pc-windows-msvc"))
        self.arch.start()
        self.native = patch.object(setup, "install_native_dependencies", return_value=self.directory / "native_node")
        self.native_mock = self.native.start()
        # Never mutate real machine ACLs/profiles during simulated installation.
        self.protect = patch.object(setup, "_protect_directory")
        self.protect.start()

    def tearDown(self):
        self.protect.stop()
        self.native.stop()
        self.arch.stop()
        self.environ.stop()
        self.temp.cleanup()

    def configure(self, *args):
        with redirect_stdout(io.StringIO()):
            return setup.configure(list(args))

    def settings(self):
        return json.loads((self.directory / "settings.json").read_text(encoding="ascii"))

    def npm_package(self, optional=True, name="@openai/codex", declared="bin/codex.js"):
        prefix = self.base / "npm"
        package = prefix / "node_modules/@openai/codex"
        (package / "bin").mkdir(parents=True)
        (package / "bin/codex.js").write_text("#!/usr/bin/env node\n", encoding="ascii")
        (package / "package.json").write_text(json.dumps({"name": name, "bin": {"codex": declared}}), encoding="ascii")
        shim = prefix / "codex.cmd"
        shim.write_text('@echo off\nnode "%~dp0\\node_modules\\@openai\\codex\\bin\\codex.js" %*\n', encoding="ascii")
        vendor = prefix / "node_modules/@openai/codex-win32-x64" if optional else package
        vendor.mkdir(parents=True, exist_ok=True)
        if optional:
            (vendor / "package.json").write_text(json.dumps({"name": "@openai/codex-win32-x64"}), encoding="ascii")
        real = pe_fixture(vendor / "vendor/x86_64-pc-windows-msvc/bin/codex.exe")
        return shim, package, real

    def test_enable_idempotent_preserves_profile_proxy_and_official_exe(self):
        content = self.real.read_bytes()
        for _ in range(2):
            self.assertEqual(self.configure("--enable"), 0)
        cfg = self.settings()
        self.assertEqual(cfg["real_cli"], str(self.real.resolve()))
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["native_node"], str(self.directory / "native_node"))
        self.assertEqual(cfg["codex_home"], str(Path(self.env["CODEX_HOME"]).resolve()))
        self.assertEqual(cfg["plugin_root"], str(ROOT))
        self.assertTrue(self.rc.read_bytes().startswith(self.original))
        self.assertEqual(self.rc.read_bytes().count(setup.RC_START.encode()), 1)
        self.assertEqual((self.directory / "shell-rc.before").read_bytes(), self.original)
        self.assertEqual(self.real.read_bytes(), content)
        wrapper = Path(cfg["wrapper"])
        self.assertEqual(wrapper.name, "codex.cmd")
        wrapper.read_bytes().decode("ascii")
        self.assertIn("DisableDelayedExpansion", wrapper.read_text(encoding="ascii"))

    def test_disable_status_and_height(self):
        self.assertEqual(self.configure("--enable", "--height", "8"), 0)
        self.assertEqual(self.settings()["height"], 8)
        self.assertEqual(self.configure("--disable"), 0)
        self.assertFalse(self.settings()["enabled"])
        capture = io.StringIO()
        with redirect_stdout(capture):
            self.assertEqual(setup.configure(["--status"]), 0)
        self.assertFalse(json.loads(capture.getvalue())["enabled"])
        with self.assertRaises(SystemExit):
            self.configure("--height", "3")

    def test_uninstall_removes_only_owned_wrapper_and_block(self):
        self.configure("--enable")
        cfg = self.settings()
        self.rc.write_bytes(self.rc.read_bytes() + b"# user addition\r\n")
        self.assertEqual(self.configure("--uninstall"), 0)
        self.assertEqual(self.rc.read_bytes(), self.original + b"# user addition\r\n")
        self.assertFalse(Path(cfg["wrapper"]).exists())
        self.assertFalse((self.directory / "settings.json").exists())
        self.assertTrue(self.real.is_file())
        self.assertEqual((self.directory / "shell-rc.before").read_bytes(), self.original)

    def test_user_replacement_and_edited_marked_block_are_preserved(self):
        self.configure("--enable")
        wrapper = Path(self.settings()["wrapper"])
        wrapper.write_text("user replacement", encoding="ascii")
        self.rc.write_bytes(self.rc.read_bytes().replace(b"Remove-Variable", b"# edited Remove-Variable"))
        before = self.rc.read_bytes()
        self.configure("--uninstall")
        self.assertEqual(wrapper.read_text(encoding="ascii"), "user replacement")
        self.assertEqual(self.rc.read_bytes(), before)

    def test_existing_third_party_shim_is_never_overwritten(self):
        wrapper = Path(self.env["CODEX_HUD_BIN_DIR"]) / "codex.cmd"
        wrapper.parent.mkdir()
        wrapper.write_text("other launcher", encoding="ascii")
        with self.assertRaises(SystemExit):
            self.configure("--enable")
        self.assertEqual(wrapper.read_text(encoding="ascii"), "other launcher")
        self.assertEqual(self.rc.read_bytes(), self.original)
        self.native_mock.assert_not_called()

    def test_dependency_failure_leaves_profile_and_cli_unmodified(self):
        self.native_mock.side_effect = RuntimeError("fixture install failed")
        with self.assertRaises(SystemExit):
            self.configure("--enable")
        self.assertEqual(self.rc.read_bytes(), self.original)
        self.assertFalse((Path(self.env["CODEX_HUD_BIN_DIR"]) / "codex.cmd").exists())

    def test_profile_edit_during_dependency_install_is_never_overwritten(self):
        changed = self.original + b"# edited while npm installs\r\n"
        def installing(directory):
            self.rc.write_bytes(changed)
            return directory / "native_node"
        self.native_mock.side_effect = installing
        with self.assertRaises(SystemExit):
            self.configure("--enable")
        self.assertEqual(self.rc.read_bytes(), changed)
        self.assertFalse((Path(self.env["CODEX_HUD_BIN_DIR"]) / "codex.cmd").exists())

    def test_utf16_profile_roundtrip(self):
        for encoding in ("utf-16-le", "utf-16-be"):
            original = "\ufeff# 中文\r\n$env:HTTPS_PROXY='fixture'\r\n".encode(encoding)
            self.rc.write_bytes(original)
            self.configure("--enable")
            self.assertTrue(self.rc.read_bytes().startswith(original))
            self.configure("--uninstall")
            self.assertEqual(self.rc.read_bytes(), original)

    def test_missing_profile_is_created_without_user_configuration(self):
        self.rc.unlink()
        self.configure("--enable")
        self.assertIn(setup.RC_START, self.rc.read_text(encoding="utf-8-sig"))
        self.assertFalse((self.directory / "shell-rc.before").exists())

    def test_explicit_cli_wins_over_saved_cli(self):
        self.directory.mkdir()
        old = pe_fixture(self.base / "old/codex.exe")
        setup._save(self.directory / "settings.json", {"real_cli": str(old)})
        self.assertEqual(setup.resolve_codex(str(old)), str(self.real.resolve()))

    def test_saved_cli_wins_over_path(self):
        self.directory.mkdir()
        setup._save(self.directory / "settings.json", {"real_cli": str(self.real)})
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_REAL_CLI")
            with patch.object(os, "get_exec_path", return_value=[]):
                self.assertEqual(setup.resolve_codex(), str(self.real.resolve()))

    def test_resolve_official_optional_and_embedded_npm_vendor_executables(self):
        for optional in (True, False):
            if (self.base / "npm").exists():
                shutil.rmtree(self.base / "npm")
            shim, package, real = self.npm_package(optional)
            content = shim.read_bytes()
            with patch.dict(os.environ, {"CODEX_HUD_REAL_CLI": str(shim)}):
                self.assertEqual(setup.resolve_codex(), str(real.resolve()))
            with patch.dict(os.environ, {"CODEX_HUD_REAL_CLI": str(package / "bin/codex.js")}):
                self.assertEqual(setup.resolve_codex(), str(real.resolve()))
            self.assertEqual(shim.read_bytes(), content)

    def test_reject_unknown_npm_package_wrong_bin_and_false_executable(self):
        shim, package, _ = self.npm_package(name="third-party")
        with patch.dict(os.environ, {"CODEX_HUD_REAL_CLI": str(shim)}):
            with self.assertRaises(RuntimeError):
                setup.resolve_codex()
        (package / "package.json").write_text(json.dumps({"name": "@openai/codex", "bin": {"codex": "different.js"}}), encoding="ascii")
        with patch.dict(os.environ, {"CODEX_HUD_REAL_CLI": str(package / "bin/codex.js")}):
            with self.assertRaises(RuntimeError):
                setup.resolve_codex()
        fake = self.base / "fake.exe"
        fake.write_text("MZ just a disguised script", encoding="ascii")
        with patch.dict(os.environ, {"CODEX_HUD_REAL_CLI": str(fake)}):
            with self.assertRaises(RuntimeError):
                setup.resolve_codex()

    def test_path_discovery_skips_owned_shim_and_never_executes_candidates(self):
        self.configure("--enable")
        (self.directory / "settings.json").unlink()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_REAL_CLI")
            with patch.object(os, "get_exec_path", return_value=[self.env["CODEX_HUD_BIN_DIR"], str(self.real.parent)]), patch.object(setup.subprocess, "run") as run:
                self.assertEqual(setup.resolve_codex(), str(self.real.resolve()))
                run.assert_not_called()

    def test_no_profile_path_lookup_does_not_load_user_profile(self):
        result = subprocess.CompletedProcess([], 0, str(self.rc), "")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_SHELL_RC")
            with patch.object(setup, "_powershells", return_value=["pwsh.exe"]), patch.object(setup.subprocess, "run", return_value=result) as run:
                self.assertEqual(setup.powershell_profile(), self.rc)
                self.assertIn("-NoProfile", run.call_args.args[0])
                self.assertIn("$PROFILE.CurrentUserCurrentHost", run.call_args.args[0][-1])

    def test_host_discovery_finds_standard_installs_outside_path(self):
        program = self.base / "Program Files"
        system = self.base / "Windows"
        pwsh = program / "PowerShell/7/pwsh.exe"
        ps5 = system / "System32/WindowsPowerShell/v1.0/powershell.exe"
        for path in (pwsh, ps5):
            path.parent.mkdir(parents=True)
            path.touch()
        with patch.dict(os.environ, {"ProgramFiles": str(program), "SystemRoot": str(system)}), patch.object(setup.shutil, "which", return_value=None):
            self.assertEqual(setup._powershells(), [str(pwsh), str(ps5)])
            self.assertEqual(setup._powershell(), str(pwsh))

    def test_default_discovers_both_powershell_hosts_without_loading_profiles(self):
        ps5 = self.base / "Documents/WindowsPowerShell/profile.ps1"
        responses = [subprocess.CompletedProcess([], 0, str(self.rc), ""),
                     subprocess.CompletedProcess([], 0, str(ps5), "")]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_SHELL_RC")
            with patch.object(setup, "_powershells", return_value=["pwsh.exe", "powershell.exe"]), patch.object(setup.subprocess, "run", side_effect=responses) as run:
                self.assertEqual(setup.powershell_profiles(), [self.rc, ps5])
                self.assertEqual([call.args[0][0] for call in run.call_args_list], ["pwsh.exe", "powershell.exe"])
                for call in run.call_args_list:
                    self.assertIn("-NoProfile", call.args[0])
                    self.assertNotIn("-ExecutionPolicy", call.args[0])

    def test_explicit_profile_only_touches_the_selected_file(self):
        other = self.base / "WindowsPowerShell/profile.ps1"
        other.parent.mkdir()
        other.write_bytes(b"# unrelated host\r\n")
        with patch.object(setup, "_powershells", side_effect=AssertionError("must not discover hosts")):
            self.configure("--enable")
        self.assertEqual([profile["shell_rc"] for profile in self.settings()["profiles"]], [str(self.rc)])
        self.assertEqual(other.read_bytes(), b"# unrelated host\r\n")
        self.configure("--uninstall")
        self.assertEqual(other.read_bytes(), b"# unrelated host\r\n")

    def test_both_profiles_have_private_backups_and_preserve_encoding_across_lifecycle(self):
        ps5 = self.base / "Documents/WindowsPowerShell/profile.ps1"
        ps5.parent.mkdir()
        original5 = "\ufeff# PS5 中文\r\nfunction proxy { 'preserved' }\r\n".encode("utf-16-le")
        ps5.write_bytes(original5)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_SHELL_RC")
            with patch.object(setup, "powershell_profiles", return_value=[self.rc, ps5]) as hosts:
                for _ in range(2):
                    self.configure("--enable")
                profiles = self.settings()["profiles"]
                self.assertEqual(len(profiles), 2)
                for profile, original in zip(profiles, (self.original, original5)):
                    path = Path(profile["shell_rc"])
                    self.assertTrue(path.read_bytes().startswith(original))
                    text, _ = setup._profile_text(path)
                    self.assertEqual(text.count(setup.RC_START), 1)
                    self.assertEqual(Path(profile["backup"]).read_bytes(), original)
                    if os.name != "nt":
                        self.assertEqual(Path(profile["backup"]).stat().st_mode & 0o777, 0o600)
                self.configure("--disable")
                self.assertFalse(self.settings()["enabled"])
                self.assertEqual(self.settings()["profiles"], profiles)
                hosts.return_value = [self.rc]  # PS5 was removed after installation.
                self.configure("--enable")
                self.assertEqual(self.settings()["profiles"], profiles)
                ps5.write_bytes(ps5.read_bytes() + "# user addition\r\n".encode("utf-16-le"))
                self.configure("--uninstall")
        self.assertEqual(self.rc.read_bytes(), self.original)
        self.assertEqual(ps5.read_bytes(), original5 + "# user addition\r\n".encode("utf-16-le"))

    def test_legacy_single_profile_adds_new_host_without_losing_original_backup(self):
        self.configure("--enable")
        current = self.settings()
        current.pop("profiles")
        setup._save(self.directory / "settings.json", current)
        state = setup._json(self.directory / "initial-state.json")
        state.pop("profiles")
        setup._save(self.directory / "initial-state.json", state)
        ps5 = self.base / "WindowsPowerShell/profile.ps1"
        ps5.parent.mkdir()
        original5 = b"# newly installed PS5\r\n"
        ps5.write_bytes(original5)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_SHELL_RC")
            with patch.object(setup, "powershell_profiles", return_value=[ps5, self.rc]):
                self.configure("--enable")
                cfg = self.settings()
                self.assertEqual(cfg["shell_rc"], str(self.rc))
                self.assertEqual([profile["shell_rc"] for profile in cfg["profiles"]], [str(self.rc), str(ps5)])
                self.assertEqual((self.directory / "shell-rc.before").read_bytes(), self.original)
                self.assertEqual(Path(cfg["profiles"][1]["backup"]).read_bytes(), original5)
                self.configure("--uninstall")
        self.assertEqual(self.rc.read_bytes(), self.original)
        self.assertEqual(ps5.read_bytes(), original5)

    def test_edited_second_profile_is_rejected_before_modifying_either_host(self):
        ps5 = self.base / "WindowsPowerShell/profile.ps1"
        ps5.parent.mkdir()
        ps5.write_bytes(b"# original PS5\r\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CODEX_HUD_SHELL_RC")
            with patch.object(setup, "powershell_profiles", return_value=[self.rc, ps5]):
                self.configure("--enable")
                ps5.write_bytes(ps5.read_bytes().replace(b"Remove-Variable", b"# user edit Remove-Variable"))
                before = self.rc.read_bytes(), ps5.read_bytes()
                calls = self.native_mock.call_count
                with self.assertRaises(SystemExit):
                    self.configure("--enable")
                self.assertEqual(self.native_mock.call_count, calls)
                self.assertEqual((self.rc.read_bytes(), ps5.read_bytes()), before)
                self.configure("--uninstall")
        self.assertEqual(self.rc.read_bytes(), self.original)
        self.assertEqual(ps5.read_bytes(), before[1])

    def test_plugin_metadata_and_cache_use_windows_path_parts(self):
        home = Path(self.env["CODEX_HOME"])
        home.mkdir()
        (home / "config.toml").write_text('[plugins."codex-hud@codex-hud"]\nenabled = true\n', encoding="ascii")
        self.configure("--enable")
        self.assertEqual(self.settings()["plugin_key"], "codex-hud@codex-hud")


class NativeDependencyTest(unittest.TestCase):
    def test_windows_backup_acl_is_current_user_only_without_policy_change(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with patch.object(setup.os, "name", "nt"), patch.object(setup, "_powershell", return_value="pwsh.exe"), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                setup._protect_directory(directory)
            command = run.call_args.args[0]
            self.assertIn("-NoProfile", command)
            self.assertNotIn("-ExecutionPolicy", command)
            script = base64.b64decode(command[-1]).decode("utf-16le")
            self.assertIn("WindowsIdentity]::GetCurrent().User", script)
            self.assertIn("SetAccessRuleProtection($true,$false)", script)
            self.assertIn("'ContainerInherit,ObjectInherit'", script)

    def test_install_uses_user_prefix_and_direct_npm_js_and_skips_unchanged(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "plugin"
            (root / "native").mkdir(parents=True)
            manifest = {"dependencies": {"@lydell/node-pty": "1.2.0-beta.15", "@xterm/headless": "6.0.0"}}
            (root / "native/package.json").write_text(json.dumps(manifest), encoding="ascii")
            (root / "native/package-lock.json").write_text('{"lockfileVersion":3}', encoding="ascii")
            npm = base / "Node/npm.cmd"
            cli = npm.parent / "node_modules/npm/bin/npm-cli.js"
            cli.parent.mkdir(parents=True)
            cli.touch()
            node = npm.parent / "node.exe"
            responses = [subprocess.CompletedProcess([], 0, "22\n"), subprocess.CompletedProcess([], 0),
                         subprocess.CompletedProcess([], 0, "", "")]
            with patch.object(setup.shutil, "which", side_effect=lambda name: str(node) if name.startswith("node") else str(npm)), patch.object(setup.subprocess, "run", side_effect=responses) as run:
                prefix = setup.install_native_dependencies(base / "settings", root)
                self.assertEqual(prefix, base / "settings/native_node")
                install_call = run.call_args_list[1]
                self.assertEqual(install_call.args[0][:2], [str(node), str(cli)])
                self.assertIn(str(prefix), install_call.args[0])
                self.assertIn("--include=optional", install_call.args[0])
                self.assertIn("--global=false", install_call.args[0])
                self.assertIn("--workspaces=false", install_call.args[0])
                self.assertEqual(install_call.kwargs["cwd"], str(prefix))
                self.assertNotIn("shell", install_call.kwargs)
                verify = run.call_args.args[0]
                self.assertIn("createRequire", verify[2])
                self.assertIn("@lydell/node-pty", verify[2])
                self.assertIn("@xterm/headless", verify[2])
                self.assertEqual(verify[-1], str(prefix / "package.json"))
            self.assertEqual((prefix / "package-lock.json").read_bytes(), (root / "native/package-lock.json").read_bytes())
            for dep in manifest["dependencies"]:
                package = prefix / "node_modules" / dep / "package.json"
                package.parent.mkdir(parents=True)
                package.touch()
            with patch.object(setup.shutil, "which", return_value="node"), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "22\n")) as run:
                setup.install_native_dependencies(base / "settings", root)
                self.assertEqual(run.call_count, 2)
                self.assertIn("createRequire", run.call_args.args[0][2])

    def test_missing_native_module_is_reported_after_install_and_for_unchanged_prefix(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "plugin"
            (root / "native").mkdir(parents=True)
            manifest = {"dependencies": {"@lydell/node-pty": "1.2.0-beta.15", "@xterm/headless": "6.0.0"}}
            (root / "native/package.json").write_text(json.dumps(manifest), encoding="ascii")
            failed = subprocess.CompletedProcess([], 1, "", "fixture missing ConPTY binary")
            responses = [subprocess.CompletedProcess([], 0, "22\n"), subprocess.CompletedProcess([], 0), failed]
            with patch.object(setup.shutil, "which", return_value="node"), patch.object(setup.subprocess, "run", side_effect=responses):
                with self.assertRaisesRegex(RuntimeError, "fixture missing ConPTY binary"):
                    setup.install_native_dependencies(base / "settings", root)
            prefix = base / "settings/native_node"
            for dependency in manifest["dependencies"]:
                package = prefix / "node_modules" / dependency / "package.json"
                package.parent.mkdir(parents=True)
                package.write_text("{}", encoding="ascii")
            responses = [subprocess.CompletedProcess([], 0, "22\n"), failed]
            with patch.object(setup.shutil, "which", return_value="node"), patch.object(setup.subprocess, "run", side_effect=responses) as run:
                with self.assertRaisesRegex(RuntimeError, "fixture missing ConPTY binary"):
                    setup.install_native_dependencies(base / "settings", root)
                self.assertEqual(run.call_count, 2)

    def test_missing_manifest_and_unsupported_node_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "package.json"):
                setup.install_native_dependencies(base, base)
            (base / "native").mkdir()
            (base / "native/package.json").write_text("{}", encoding="ascii")
            with patch.object(setup.shutil, "which", return_value="node"), patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "18\n")):
                with self.assertRaisesRegex(RuntimeError, "Node.js 20"):
                    setup.install_native_dependencies(base, base)


class BatchLauncherTest(unittest.TestCase):
    def test_ascii_encoded_paths_missing_bootstrap_fallback_and_argv(self):
        with tempfile.TemporaryDirectory(prefix="HUD 中文 ") as temp:
            base = Path(temp)
            fixture = base / "fixture.py"
            fixture.write_text("import json,os,sys\nprint(json.dumps({'argv':sys.argv[1:],'proxy':os.getenv('HTTP_PROXY')}))\nsys.exit(23)\n", encoding="ascii")
            text = setup.wrapper_text(sys.executable, base / "missing launcher.py")
            text.encode("ascii")
            code = re.search(r'^python -c "(.+)" %\*', text, re.MULTILINE).group(1)
            args = [str(fixture), "", "spaces and 'quotes'", 'double"quote', "中文", "!value!", "--"]
            result = subprocess.run([sys.executable, "-c", code, *args], env=dict(os.environ, HTTP_PROXY="fixture-proxy"), capture_output=True, text=True)
            self.assertEqual(result.returncode, 23, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"argv": args[1:], "proxy": "fixture-proxy"})
            self.assertIn("node -e", text)
            self.assertIn("process.argv.slice(1)", text)
            self.assertNotIn("CALL", text)


@unittest.skipUnless(os.name == "nt", "requires actual Windows cmd.exe/PowerShell")
class WindowsNativeSmokeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex HUD 中文 ")
        self.base = Path(self.temp.name)
        self.fixture = self.base / "fake official CLI.py"
        self.fixture.write_text("import json,os,sys\nprint(json.dumps({'argv':sys.argv[1:],'proxy':os.getenv('HTTP_PROXY')}))\nsys.exit(23)\n", encoding="ascii")
        self.bootstrap = self.base / "launcher.py"
        self.bootstrap.write_bytes((ROOT / "codex_hud/bootstrap.py").read_bytes())
        self.wrapper = self.base / "codex.cmd"
        self.wrapper.write_bytes(setup.wrapper_text(sys.executable, self.bootstrap).encode("ascii"))
        self.env = dict(os.environ, CODEX_HUD_CONFIG_HOME=str(self.base / "settings"), HTTP_PROXY="fixture-proxy")
        self.assertTrue(setup.is_pe(sys.executable), "Runtime fixture must be a real PE executable")

    def tearDown(self):
        self.temp.cleanup()

    def batch(self, arguments, env=None):
        # cmd /s /c strips the outer pair; inner quoting is kept for the batch.
        command = subprocess.list2cmdline([str(self.wrapper), str(self.fixture), *arguments])
        comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
        return subprocess.run(f'"{comspec}" /d /s /c "{command}"', env=env or self.env,
                              capture_output=True, encoding="utf-8", timeout=20)

    def assert_forwarded(self, result, arguments):
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"argv": arguments, "proxy": "fixture-proxy"})

    def compile_native_fixture(self):
        # Framework's compiler emits a genuine x64 PE. Do not bypass discovery's
        # PE validation for the bare `codex --version` smoke.
        framework = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319"
        compiler = framework / "csc.exe"
        self.assertTrue(compiler.is_file(), "Windows CI requires the .NET Framework C# compiler")
        source = self.base / "FakeCodex.cs"
        source.write_text(r'''
using System;
using System.Text;
class FakeCodex {
    static string Quote(string value) {
        return value == null ? "null" : "\"" + value.Replace("\\", "\\\\")
            .Replace("\"", "\\\"").Replace("\r", "\\r")
            .Replace("\n", "\\n").Replace("\t", "\\t") + "\"";
    }
    static int Main(string[] args) {
        Console.OutputEncoding = new UTF8Encoding(false);
        string argv = String.Join(",", Array.ConvertAll(args, new Converter<string,string>(Quote)));
        Console.WriteLine("{\"argv\":[" + argv + "],\"proxy\":"
            + Quote(Environment.GetEnvironmentVariable("HTTP_PROXY")) + "}");
        return 23;
    }
}
''', encoding="ascii")
        executable = self.base / "native official/codex.exe"
        executable.parent.mkdir()
        result = subprocess.run([str(compiler), "/nologo", "/target:exe", "/platform:x64",
                                 "/out:" + str(executable), str(source)],
                                capture_output=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(setup.is_pe(executable))
        return executable

    def test_real_cmd_preserves_arguments_proxy_and_exit_code(self):
        args = ["exec", "", "spaces and 'quotes'", 'double"quote', "中文参数", "!literal!", "--", "-h"]
        self.assert_forwarded(self.batch(args), args)

    def test_python_missing_uses_node_without_native_modules(self):
        node = shutil.which("node.exe") or shutil.which("node")
        self.assertIsNotNone(node)
        env = dict(self.env, PATH=str(Path(node).parent))
        args = ["--version", "quoted spaces", "中文参数"]
        self.assert_forwarded(self.batch(args, env), args)

    def test_bootstrap_missing_still_uses_official_executable(self):
        self.bootstrap.unlink()
        args = ["--version", "中文参数"]
        self.assert_forwarded(self.batch(args), args)

    def test_powershell_profile_path_block_reaches_real_cmd(self):
        powershell = shutil.which("pwsh.exe") or shutil.which("pwsh")
        self.assertIsNotNone(powershell, "CI installs PowerShell 7")
        profile = self.base / "profile.ps1"
        profile.write_text(setup.profile_block(self.base), encoding="utf-8-sig")
        runner = self.base / "profile smoke.ps1"
        literal = lambda value: "'" + str(value).replace("'", "''") + "'"
        runner.write_text(f". {literal(profile)}\n& codex {literal(self.fixture)} 'quoted spaces' '中文参数'\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
        result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-File", str(runner)], env=self.env, capture_output=True, encoding="utf-8", timeout=20)
        self.assert_forwarded(result, ["quoted spaces", "中文参数"])

    def test_real_windows_install_private_backup_dependencies_and_uninstall(self):
        profile = self.base / "installed profile.ps1"
        original = b"$env:HTTP_PROXY = 'fixture-proxy'\r\n"
        profile.write_bytes(original)
        real = self.compile_native_fixture()
        npm_prefix = self.base / "original npm"
        npm_prefix.mkdir()
        npm_cmd = npm_prefix / "codex.cmd"
        npm_ps1 = npm_prefix / "codex.ps1"
        npm_cmd.write_bytes(b"@echo off\r\necho ORIGINAL_NPM_CMD_MUST_NOT_RUN\r\nexit /b 97\r\n")
        npm_ps1.write_text("Write-Error 'ORIGINAL_NPM_PS1_MUST_NOT_RUN'; exit 98\n", encoding="ascii")
        cmd_before, ps1_before = npm_cmd.read_bytes(), npm_ps1.read_bytes()
        shim_bin = self.base / ".local/bin"
        self.wrapper = shim_bin / "codex.cmd"
        # Registry installation inherits the runner's real proxy settings. The
        # synthetic proxy only belongs to the later CLI/profile passthrough.
        env = dict(os.environ, CODEX_HUD_CONFIG_HOME=self.env["CODEX_HUD_CONFIG_HOME"],
                   CODEX_HOME=str(self.base / "empty-codex-home"),
                   CODEX_HUD_REAL_CLI=str(real), CODEX_HUD_BIN_DIR=str(shim_bin),
                   CODEX_HUD_SHELL_RC=str(profile),
                   PATH=str(npm_prefix) + os.pathsep + self.env["PATH"])
        with patch.dict(os.environ, env):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup.configure(["--enable"]), 0)
            settings = json.loads((self.base / "settings/settings.json").read_text(encoding="ascii"))
            self.assertTrue((Path(settings["native_node"]) / "node_modules/@lydell/node-pty/package.json").is_file())
            self.assertEqual((self.base / "settings/shell-rc.before").read_bytes(), original)
            runner = self.base / "bare codex smoke.ps1"
            discovery = self.base / "command-discovery.json"
            literal = lambda value: "'" + str(value).replace("'", "''") + "'"
            runner.write_text(
                "$ErrorActionPreference = 'Stop'\n"
                f". {literal(profile)}\n"
                "$resolved = Get-Command codex\n"
                "$commands = @(Get-Command codex -All | ForEach-Object { "
                "@{ path = $_.Path; type = $_.CommandType.ToString() } })\n"
                "$record = @{ selected = $resolved.Path; type = $resolved.CommandType.ToString(); commands = $commands }\n"
                f"[IO.File]::WriteAllText({literal(discovery)}, ($record | ConvertTo-Json -Depth 4), "
                "(New-Object Text.UTF8Encoding($false)))\n"
                "codex --version\nexit $LASTEXITCODE\n", encoding="utf-8-sig")
            powershells = [shutil.which("pwsh.exe") or shutil.which("pwsh"),
                          shutil.which("powershell.exe")]
            self.assertIsNotNone(powershells[0], "CI requires PowerShell 7")
            for powershell in dict.fromkeys(path for path in powershells if path):
                with self.subTest(powershell=powershell):
                    discovery.unlink(missing_ok=True)
                    result = subprocess.run([powershell, "-NoLogo", "-NoProfile", "-File", str(runner)],
                                            env=dict(env, HTTP_PROXY="before-profile"), capture_output=True, encoding="utf-8", timeout=30)
                    self.assertTrue(discovery.is_file(), result.stderr)
                    found = json.loads(discovery.read_text(encoding="utf-8-sig"))
                    self.assertEqual(found["type"], "Application", found)
                    # ExternalScript paths may expand RUNNER~1 to runneradmin,
                    # while Application paths retain the short spelling. File
                    # identity preserves the assertion across either form.
                    self.assertTrue(os.path.samefile(found["selected"], self.wrapper), found)
                    candidates = [item["path"] for item in found["commands"] if item.get("path")]
                    for original in (npm_cmd, npm_ps1):
                        self.assertTrue(any(os.path.samefile(candidate, original) for candidate in candidates),
                                        f"Original npm entry was not discovered: {original}; commands={found}")
                    self.assert_forwarded(result, ["--version"])
            with redirect_stdout(io.StringIO()):
                self.assertEqual(setup.configure(["--uninstall"]), 0)
        self.assertEqual(profile.read_bytes(), original)
        self.assertFalse(self.wrapper.exists())
        self.assertEqual(npm_cmd.read_bytes(), cmd_before)
        self.assertEqual(npm_ps1.read_bytes(), ps1_before)


if __name__ == "__main__":
    unittest.main()
