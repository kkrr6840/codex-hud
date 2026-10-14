import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_hud.integration import interactive, resolve_codex, entrypoint_plan, replace_link
from codex_hud.bootstrap import installed_root, read_settings

ENTRY = Path(__file__).resolve().parents[1] / "codex-hud"


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        real_bin = self.base / "official"
        real_bin.mkdir()
        real = real_bin / "codex"
        real.write_text(f"#!{sys.executable}\n" + "import sys,json,os\nprint(json.dumps({'argv':sys.argv[1:],'proxy':os.getenv('HTTP_PROXY')}))\nsys.exit(23 if '--fixture-error' in sys.argv else 0)\n")
        real.chmod(0o755)
        self.rc = self.base / "shell.rc"
        self.original_rc = 'codex() { HTTP_PROXY="http://127.0.0.1:7890" command codex "$@"; }\n'
        self.rc.write_text(self.original_rc)
        self.env = dict(os.environ, PATH=str(real_bin) + os.pathsep + os.environ["PATH"],
                        CODEX_HOME=str(self.base / "codex-home"),
                        CODEX_HUD_CONFIG_HOME=str(self.base / "settings"),
                        CODEX_HUD_REAL_CLI=str(real),
                        CODEX_HUD_BIN_DIR=str(self.base / "local-bin"),
                        CODEX_HUD_SHELL_RC=str(self.rc))

    def tearDown(self):
        self.temp.cleanup()

    def setup(self, *args):
        return subprocess.run([sys.executable, str(ENTRY), "setup", *args], env=self.env,
                              capture_output=True, text=True, timeout=5)

    def npm_entry(self, name="package"):
        package = self.base / name
        (package / "bin").mkdir(parents=True)
        target = package / "bin/codex.js"
        target.write_bytes(Path(self.env["CODEX_HUD_REAL_CLI"]).read_bytes())
        target.chmod(0o755)
        (package / "package.json").write_text(json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}))
        entry = self.base / "official/codex"
        entry.unlink()
        entry.symlink_to(os.path.relpath(target, entry.parent))
        return entry, target

    def test_old_entry_reaches_launcher_and_uninstall_restores_original_link(self):
        entry, target = self.npm_entry()
        original = os.readlink(entry)
        content = target.read_bytes()
        for _ in range(2):
            self.assertEqual(self.setup("--enable").returncode, 0)
        self.assertEqual(os.readlink(entry), str(self.base / "local-bin/codex"))
        cfg = json.loads((self.base / "settings/settings.json").read_text())
        self.assertEqual(cfg["real_cli"], str(target.resolve()))
        self.assertEqual(cfg["entrypoint_bridge"]["original_target"], original)
        for action in ("--enable", "--disable"):
            self.assertEqual(self.setup(action).returncode, 0)
            result = subprocess.run([str(entry), "exec", "spaces and 'quotes'", "--fixture-error"], env=dict(self.env, HTTP_PROXY="fixture-proxy"), capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 23)
            self.assertEqual(json.loads(result.stdout)["proxy"], "fixture-proxy")
            self.assertEqual(json.loads(result.stdout)["argv"], ["exec", "spaces and 'quotes'", "--fixture-error"])
        # The compatibility entry must also survive loss of Python support.
        old_python = self.base / "python-bin"
        old_python.mkdir()
        (old_python / "python3").write_text("#!/bin/sh\nexit 1\n")
        (old_python / "python3").chmod(0o755)
        result = subprocess.run([str(entry), "--version"], env=dict(self.env, PATH=str(old_python) + os.pathsep + self.env["PATH"]), capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["argv"], ["--version"])
        self.assertEqual(self.setup("--uninstall").returncode, 0)
        self.assertEqual(os.readlink(entry), original)
        self.assertEqual(target.read_bytes(), content)

    def test_package_update_retargets_bridge_and_uninstall_preserves_user_replacement(self):
        entry, old = self.npm_entry()
        self.assertEqual(self.setup("--enable").returncode, 0)
        # Model npm replacing its bin link with a different package version.
        entry.unlink()
        entry.symlink_to(old)
        entry, new = self.npm_entry("updated-package")
        original = os.readlink(entry)
        self.assertEqual(self.setup("--enable").returncode, 0)
        cfg = json.loads((self.base / "settings/settings.json").read_text())
        self.assertEqual(cfg["real_cli"], str(new.resolve()))
        self.assertEqual(cfg["entrypoint_bridge"]["original_target"], original)
        entry.unlink()
        entry.write_text("user replacement")
        self.assertEqual(self.setup("--uninstall").returncode, 0)
        self.assertEqual(entry.read_text(), "user replacement")

    def test_only_verified_entry_symlinks_are_bridged_and_changes_are_preserved(self):
        entry, target = self.npm_entry()
        wrapper = self.base / "local-bin/codex"
        (target.parent.parent / "package.json").write_text(json.dumps({"name": "@openai/codex", "bin": {"codex": "different.js"}}))
        self.assertIsNone(entrypoint_plan(str(entry), wrapper, {})[1])
        before = os.readlink(entry)
        entry.unlink()
        entry.write_text("changed concurrently")
        with self.assertRaises(RuntimeError):
            replace_link(entry, str(wrapper), before)
        self.assertEqual(entry.read_text(), "changed concurrently")

    def test_classifier_respects_values_commands_and_prompt_delimiter(self):
        yes = [[], ["a normal prompt"], ["-m", "exec"], ["-c", "name='review'", "resume", "--last"], ["--", "exec"], ["fork", "thread-id"]]
        no = [["exec"], ["-m", "MODEL", "exec", "prompt"], ["plugin", "add", "x"], ["--version"], ["--help"], ["debug", "models"], ["resume", "--help"], ["--future-option"], ["-m"], ["--remote", "unix://other"], ["resume", "--remote", "unix://other"], ["prompt", "--help"], ["resume", "--future-option"], ["exec", "--", "prompt"]]
        for argv in yes:
            self.assertTrue(interactive(argv), argv)
        for argv in no:
            self.assertFalse(interactive(argv), argv)

    def test_setup_is_idempotent_and_preserves_existing_function(self):
        for _ in range(2):
            result = self.setup("--enable")
            self.assertEqual(result.returncode, 0, result.stderr)
        text = self.rc.read_text()
        self.assertTrue(text.startswith(self.original_rc))
        self.assertEqual(text.count("# >>> codex-hud PATH >>>"), 1)
        self.assertEqual((self.base / "settings/shell-rc.before").read_text(), self.original_rc)
        result = self.setup("--uninstall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.rc.read_text(), self.original_rc)
        self.assertFalse((self.base / "local-bin/codex").exists())

    def test_existing_unrelated_launcher_is_never_overwritten(self):
        wrapper = self.base / "local-bin/codex"
        wrapper.parent.mkdir()
        wrapper.write_text("existing unrelated program")
        result = self.setup("--enable")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(wrapper.read_text(), "existing unrelated program")
        self.assertEqual(self.rc.read_text(), self.original_rc)

    def test_bypass_preserves_all_arguments_proxy_and_exit_status(self):
        self.assertEqual(self.setup("--enable").returncode, 0)
        wrapper = self.base / "local-bin/codex"
        args = ["exec", "", "spaces and 'quotes'", "line\nbreak", "$(should-not-execute)", "*", "--", "-h"]
        result = subprocess.run([str(wrapper), *args], env=dict(self.env, HTTP_PROXY="http://127.0.0.1:7890"), capture_output=True, text=True, timeout=5)
        obj = json.loads(result.stdout)
        self.assertEqual(obj["argv"], args)
        self.assertEqual(obj["proxy"], "http://127.0.0.1:7890")
        error = subprocess.run([str(wrapper), "--fixture-error"], env=self.env, capture_output=True, timeout=5)
        self.assertEqual(error.returncode, 23)

    def test_disabling_or_removing_plugin_still_reaches_original_cli(self):
        home = Path(self.env["CODEX_HOME"])
        home.mkdir()
        config = home / "config.toml"
        config.write_text('[plugins."codex-hud@codex-hud"]\nenabled = true\n')
        self.assertEqual(self.setup("--enable").returncode, 0)
        settings = json.loads((self.base / "settings/settings.json").read_text())
        self.assertEqual(settings["plugin_key"], "codex-hud@codex-hud")
        config.write_text('[plugins."codex-hud@codex-hud"]\nenabled = false\n')
        result = subprocess.run([str(self.base / "local-bin/codex"), "--version"], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["argv"], ["--version"])
        self.assertEqual(self.setup("--disable").returncode, 0)
        self.assertFalse(json.loads((self.base / "settings/settings.json").read_text())["enabled"])

    def test_python_removal_or_corrupt_settings_preserve_native_cli(self):
        self.assertEqual(self.setup("--enable").returncode, 0)
        wrapper = self.base / "local-bin/codex"
        broken_python = self.base / "old-python"
        broken_python.mkdir()
        (broken_python / "python3").write_text("#!/bin/sh\nexit 1\n")
        (broken_python / "python3").chmod(0o755)
        result = subprocess.run([str(wrapper), "--version"], env=dict(self.env, PATH=str(broken_python) + os.pathsep + self.env["PATH"]), capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["argv"], ["--version"])
        (self.base / "settings/settings.json").write_text("[]")
        result = subprocess.run([str(wrapper), "--version"], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(result.stdout)["argv"], ["--version"])

    def test_native_installed_version_wins_over_higher_cached_version(self):
        cache = self.base / "cache"
        for version in ("1.0.0", "2.0.0-beta.1", "9.0.0"):
            directory = cache / version / "codex_hud"
            directory.mkdir(parents=True)
            (directory / "integration.py").touch()
        cfg = {"plugin_key": "codex-hud@codex-hud", "cache_plugin_dir": str(cache), "plugin_root": str(cache / "9.0.0")}
        for version in ("1.0.0", "2.0.0-beta.1"):
            response = subprocess.CompletedProcess([], 0, json.dumps({"installed": [{"pluginId": cfg["plugin_key"], "enabled": True, "version": version}]}))
            with patch("codex_hud.bootstrap.subprocess.run", return_value=response):
                self.assertEqual(installed_root(cfg, "/official/codex"), cache / version)

    def test_unknown_upstream_wrapper_is_rejected_before_install(self):
        proxy = self.base / "proxy"
        proxy.mkdir()
        (proxy / "codex").write_text('#!/bin/sh\nexec codex "$@"\n')
        (proxy / "codex").chmod(0o755)
        env = dict(self.env, PATH=str(proxy) + os.pathsep + self.env["PATH"])
        env.pop("CODEX_HUD_REAL_CLI", None)
        result = subprocess.run([sys.executable, str(ENTRY), "setup", "--enable"], env=env, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.base / "local-bin/codex").exists())


if __name__ == "__main__":
    unittest.main()
