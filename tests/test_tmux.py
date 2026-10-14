"""End-to-end lifecycle and isolation using actual tmux/PTYs and a local CLI fixture."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ENTRY = Path(__file__).resolve().parents[1] / "codex-hud"


@unittest.skipUnless(shutil.which("tmux") and shutil.which("lsof"), "tmux and lsof required")
class TmuxTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        fixture = self.bin / "codex"
        fixture.write_text(f"#!{sys.executable}\n" + '''
import json,os,pathlib,sys,time,uuid,sqlite3
if '--help' in sys.argv:
    print('fixture help');sys.exit(0)
home=pathlib.Path(os.environ['CODEX_HOME']);folder=home/'sessions';folder.mkdir(parents=True,exist_ok=True)
identity=str(uuid.uuid4());f=(folder/f'rollout-{identity}.jsonl').open('a')
def emit(kind,payload):
    f.write(json.dumps({'type':kind,'payload':payload})+'\\n');f.flush()
emit('session_meta',{'id':identity,'source':'cli','cwd':str(pathlib.Path.cwd()),'proxy':os.environ.get('HTTP_PROXY')})
def tokens(n):
    emit('turn_context',{'model':'fixture-model'})
    emit('event_msg',{'type':'token_count','info':{'model_context_window':100,'last_token_usage':{'input_tokens':n,'total_tokens':n+5},'total_token_usage':{'input_tokens':n,'output_tokens':5,'total_tokens':n+5}}})
if '--fixture-empty-start' in sys.argv:
    db=sqlite3.connect(home/'state_5.sqlite')
    db.executescript('CREATE TABLE threads(id TEXT,source TEXT,rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);')
    db.close()
else:tokens(20)
print('FIXTURE READY',flush=True)
def composer():
    if os.environ.get('HUD_TEST_COMPOSER'):
        print('\\x1b[2J\\x1b[HFixture READY\\r\\n› \\x1b[2mAsk Codex to do anything\\x1b[0m\\x1b[2;3H',end='',flush=True)
composer()
for line in sys.stdin:
    if line.strip()=='quit':break
    if line.strip()=='next':tokens(40)
    if line.strip()=='reset':
        f.close();identity=str(uuid.uuid4());f=(folder/f'rollout-{identity}.jsonl').open('a')
        emit('session_meta',{'id':identity,'source':'cli','cwd':str(pathlib.Path.cwd())})
    composer()
f.close()
''')
        fixture.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        CODEX_HOME=str(self.base / "home"), CODEX_THREAD_ID="wrong-launcher-thread",
                        CODEX_X_CODEX_HOME=str(self.base / "offload"),
                        CODEX_HUD_CONFIG_HOME=str(self.base / "hud-config"),
                        CODEX_HUD_BIN_DIR=str(self.base / "hud-bin"),
                        CODEX_HUD_REAL_CLI=str(fixture),
                        CODEX_HUD_SHELL_RC=str(self.base / "test.zshrc"))
        self.instances = []
        self.pty_children = []
        self.entry = ENTRY

    def tearDown(self):
        for spec in self.instances:
            self.tmux(spec, "kill-server", check=False)
            shutil.rmtree(spec["runtime"], ignore_errors=True)
        for proc, output in self.pty_children:
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=3)
            output.close()
        self.temp.cleanup()

    def launch(self, *args):
        result = subprocess.run([sys.executable, str(ENTRY), "--detach", "--width", "110", "--height", "32", "--", *args], cwd=self.base, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        spec = json.loads(result.stdout)
        self.instances.append(spec)
        return spec

    def tmux(self, spec, *args, check=True):
        return subprocess.run(["tmux", "-L", spec["socket"], *args], capture_output=True, text=True, check=check, timeout=5)

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.15)
        self.fail("timed out waiting for HUD lifecycle")

    def snapshot(self, spec):
        try:
            return json.loads((Path(spec["runtime"]) / "snapshot.json").read_text())
        except (OSError, ValueError):
            return {}

    def send(self, spec, text):
        self.tmux(spec, "send-keys", "-t", spec["top"], "-l", text)
        self.tmux(spec, "send-keys", "-t", spec["top"], "Enter")

    def test_two_same_cwd_instances_refresh_zoom_and_clean_exit(self):
        one, two = self.launch(), self.launch()
        for spec in (one, two):
            self.wait_for(lambda: self.snapshot(spec).get("root", {}).get("id"))
        a, b = self.snapshot(one), self.snapshot(two)
        self.assertNotEqual(a["root"]["id"], b["root"]["id"])
        self.assertNotEqual(a["root"]["id"], "wrong-launcher-thread")
        self.assertEqual(a["root"]["context_used"], 20)
        self.send(one, "next")
        self.wait_for(lambda: self.snapshot(one).get("root", {}).get("context_used") == 40)
        self.assertEqual(self.snapshot(two)["root"]["context_used"], 20)
        panel = self.tmux(one, "capture-pane", "-p", "-t", one["hud"]).stdout
        self.assertIn("fixture-model", panel)
        self.assertNotIn("Traceback", panel)
        self.tmux(one, "resize-window", "-t", "codex", "-x", "80", "-y", "24")
        self.wait_for(lambda: 1 <= int(self.tmux(one, "display-message", "-p", "-t", one["hud"], "#{pane_height}").stdout.strip()) <= 6)
        subprocess.run([sys.executable, str(ENTRY), "_toggle", one["runtime"]], check=True, timeout=5)
        self.assertEqual(self.tmux(one, "display-message", "-p", "#{window_zoomed_flag}").stdout.strip(), "1")
        subprocess.run([sys.executable, str(ENTRY), "_toggle", one["runtime"]], check=True, timeout=5)
        self.assertEqual(self.tmux(one, "display-message", "-p", "#{window_zoomed_flag}").stdout.strip(), "0")
        self.send(one, "quit")
        self.wait_for(lambda: not Path(one["runtime"]).exists())
        self.assertEqual(self.tmux(two, "has-session", "-t", "codex", check=False).returncode, 0)
        self.send(two, "quit")
        self.wait_for(lambda: not Path(two["runtime"]).exists())

    def test_fast_cli_exit_cleans_server_and_runtime(self):
        spec = self.launch("--help")
        self.wait_for(lambda: not Path(spec["runtime"]).exists())
        self.assertNotEqual(self.tmux(spec, "has-session", "-t", "codex", check=False).returncode, 0)

    def test_external_server_exit_is_cleaned_up(self):
        spec = self.launch()
        self.tmux(spec, "kill-server")
        self.wait_for(lambda: not Path(spec["runtime"]).exists())

    def test_empty_hud_is_hidden_and_returns_only_when_data_arrives(self):
        spec = self.launch("--fixture-empty-start")
        def window(pane):
            return self.tmux(spec, "display-message", "-p", "-t", pane, "#{window_id}").stdout.strip()
        self.wait_for(lambda: self.snapshot(spec).get("root", {}).get("id"))
        first_id = self.snapshot(spec)["root"]["id"]
        self.assertNotEqual(window(spec["top"]), window(spec["hud"]))
        self.assertEqual(self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{window_panes}").stdout.strip(), "1")
        self.send(spec, "next")
        self.wait_for(lambda: window(spec["top"]) == window(spec["hud"]))
        self.assertIn("40%", self.tmux(spec, "capture-pane", "-p", "-t", spec["hud"]).stdout)
        self.send(spec, "reset")
        self.wait_for(lambda: self.snapshot(spec).get("root", {}).get("id") not in (None, first_id))
        self.wait_for(lambda: window(spec["top"]) != window(spec["hud"]))
        self.assertEqual(self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{window_panes}").stdout.strip(), "1")
        self.send(spec, "quit")
        self.wait_for(lambda: not Path(spec["runtime"]).exists())

    @unittest.skipUnless(sys.platform == "darwin", "macOS BSD script PTY test")
    def test_plain_codex_automatically_opens_hud_without_attach_command(self):
        self.assert_auto_launch()

    @unittest.skipUnless(sys.platform == "darwin", "macOS BSD script PTY test")
    def test_cached_codex_in_already_open_shell_opens_hud_without_rehash(self):
        package = self.base / "npm-package"
        (package / "bin").mkdir(parents=True)
        target = package / "bin/codex.js"
        shutil.copy2(self.bin / "codex", target)
        (package / "package.json").write_text(json.dumps({"name": "@openai/codex", "bin": {"codex": "bin/codex.js"}}))
        (self.bin / "codex").unlink()
        (self.bin / "codex").symlink_to(target)
        self.assert_auto_launch(cached=True)

    @unittest.skipUnless(sys.platform == "darwin", "macOS BSD script PTY test")
    def test_nested_tmux_terminal_uses_synchronized_updates_and_steady_cursor(self):
        self.assert_auto_launch(terminal="tmux-256color")

    @unittest.skipUnless(sys.platform == "darwin", "macOS BSD script PTY test")
    def test_agent_popup_navigates_live_output_without_restarting_cli(self):
        self.assert_auto_launch(inspect_agents=True)

    @unittest.skipUnless(sys.platform == "darwin", "macOS BSD script PTY test")
    def test_plain_down_selects_inline_agent_and_esc_returns_without_losing_draft(self):
        self.assert_auto_launch(inspect_agents=True, direct_agents=True)

    @unittest.skipUnless(sys.platform == "darwin", "macOS BSD script PTY test")
    def test_plugin_cache_removal_keeps_arrow_navigation_and_live_details(self):
        cached = self.base / "cached-plugin"
        shutil.copytree(ENTRY.parent / "codex_hud", cached / "codex_hud", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ENTRY, cached / "codex-hud")
        self.entry = cached / "codex-hud"
        self.assert_auto_launch(inspect_agents=True, direct_agents=True, remove_plugin=True)

    def assert_auto_launch(self, cached=False, terminal="xterm-256color", inspect_agents=False, direct_agents=False, remove_plugin=False):
        Path(self.env["CODEX_HUD_SHELL_RC"]).write_text('codex() { HTTP_PROXY="http://fixture.invalid" command codex "$@"; }\n')
        if not cached:
            result = subprocess.run([sys.executable, str(self.entry), "setup", "--enable"],
                                    env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
        runtimes = self.base / "runtimes"
        runtimes.mkdir()
        env = dict(self.env, CODEX_HUD_RUNTIME_DIR=str(runtimes), TERM=terminal,
                   HUD_TEST_PYTHON=sys.executable, HUD_TEST_ENTRY=str(self.entry))
        if direct_agents:
            env["HUD_TEST_COMPOSER"] = "1"
        # BSD script owns the PTY lifecycle. Avoid Python pty.fork on macOS,
        # where forking a framework-linked interpreter can leave an exit hang.
        output = (self.base / "terminal-output.log").open("wb")
        script = 'source "$CODEX_HUD_SHELL_RC"; '
        if cached:
            script += 'codex --help; "$HUD_TEST_PYTHON" "$HUD_TEST_ENTRY" setup --enable; '
        script += 'codex'
        proc = subprocess.Popen(["/usr/bin/script", "-q", "/dev/null", "/bin/zsh", "-f", "-c", script],
                                cwd=self.base, env=env, stdin=subprocess.PIPE,
                                stdout=output, stderr=subprocess.STDOUT)
        self.pty_children.append((proc, output))
        self.wait_for(lambda: list(runtimes.glob("codex-hud-*/spec.json")))
        spec_path = next(runtimes.glob("codex-hud-*/spec.json"))
        self.wait_for(lambda: json.loads(spec_path.read_text()).get("hud"))
        spec = json.loads(spec_path.read_text())
        self.instances.append(spec)
        self.wait_for(lambda: self.snapshot(spec).get("root", {}).get("model") == "fixture-model")
        identity = self.snapshot(spec)["root"]["id"]
        with (Path(spec["home"]) / "sessions" / f"rollout-{identity}.jsonl").open() as handle:
            self.assertEqual(json.loads(handle.readline())["payload"]["proxy"], "http://fixture.invalid")
        self.assertEqual(self.tmux(spec, "display-message", "-p", "#{session_attached}").stdout.strip(), "1")
        self.assertEqual(self.tmux(spec, "show-options", "-p", "-v", "-t", spec["top"], "cursor-style").stdout.strip(), "block")
        if terminal == "tmux-256color":
            capabilities = self.tmux(spec, "list-clients", "-F", "#{client_termfeatures}").stdout.strip().split(",")
            self.assertIn("sync", capabilities)
            output.flush()
            terminal_bytes = (self.base / "terminal-output.log").read_bytes()
            self.assertIn(b"\x1b[?2026h", terminal_bytes)
            self.assertIn(b"\x1b[?2026l", terminal_bytes)
        if inspect_agents:
            import sqlite3
            home = Path(spec["home"])
            child = "00000000-0000-0000-0000-000000000001"
            path = home / "sessions" / ("rollout-"+child+".jsonl")
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": child}})+"\n")
            def result(value):
                with path.open("a") as f:
                    f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": value}]}})+"\n")
            result("Agent output one")
            with sqlite3.connect(home / "state_5.sqlite") as db:
                db.executescript("CREATE TABLE threads(id TEXT,source TEXT,rollout_path TEXT,model TEXT,agent_nickname TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);")
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?)", (identity, "cli", str(home / "sessions" / f"rollout-{identity}.jsonl"), "fixture-model", None))
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?)", (child, "subagent", str(path), "child-model", "inspect-child"))
                db.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (identity, child, "open"))
            self.wait_for(lambda: any(a["id"] == child for a in self.snapshot(spec).get("agents", [])))
            before = self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout
            def terminal_contains(value):
                return value in (self.base / "terminal-output.log").read_bytes().decode(errors="replace")
            def popup_pids():
                output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
                return [line.split()[0] for line in output.splitlines() if " _agents " + spec["runtime"] in line]
            def active(pane):
                return self.tmux(spec, "display-message", "-p", "-t", pane, "#{pane_active}").stdout.strip() == "1"
            navigation = Path(spec["runtime"])/"navigation.json"
            if remove_plugin:
                shutil.rmtree(self.entry.parent)
            if direct_agents:
                proc.stdin.write(b"\x1b[B"); proc.stdin.flush()
                self.wait_for(lambda: active(spec["hud"]) and navigation.exists())
                self.assertEqual(json.loads(navigation.read_text())["key"], ["native", child])
                self.assertFalse(popup_pids())
                def highlighted():
                    return any("inspect-child" in line and "7m" in line for line in self.tmux(spec, "capture-pane", "-p", "-e", "-t", spec["hud"]).stdout.splitlines())
                self.wait_for(highlighted)
                # Up at the first entry returns to the unchanged native editor.
                proc.stdin.write(b"\x1b[A"); proc.stdin.flush()
                self.wait_for(lambda: active(spec["top"]) and not navigation.exists())
                proc.stdin.write(b"\x1b[B\r"); proc.stdin.flush()
            else:
                # Exercise the actual prefix+Down binding on the attached client.
                proc.stdin.write(b"\x02\x1b[B"); proc.stdin.flush()
                self.wait_for(lambda: terminal_contains("Agent 选择器"))
            self.wait_for(popup_pids)
            if not direct_agents:
                proc.stdin.write(b"\r"); proc.stdin.flush()
            self.wait_for(lambda: terminal_contains("Agent output one"))
            result("Agent output two")
            self.wait_for(lambda: terminal_contains("Agent output two"))
            proc.stdin.write(b"\x1b" if direct_agents else b"q"); proc.stdin.flush()
            self.wait_for(lambda: not popup_pids())
            if direct_agents:
                self.wait_for(lambda: active(spec["hud"]))
                proc.stdin.write(b"\x1b"); proc.stdin.flush()
                self.wait_for(lambda: active(spec["top"]) and not navigation.exists())
                # Fast editing+Down must remain in the official composer.
                proc.stdin.write(b"draft\x1b[B"); proc.stdin.flush()
                self.wait_for(lambda: "draft" in self.tmux(spec, "capture-pane", "-p", "-t", spec["top"]).stdout)
                time.sleep(.3)
                self.assertTrue(active(spec["top"]))
                self.assertFalse(navigation.exists())
                proc.stdin.write(b"\x15\r"); proc.stdin.flush()
                self.wait_for(lambda: self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{cursor_x}").stdout.strip() == "2")
                proc.stdin.write(b"\x1b[B"); proc.stdin.flush()
                self.wait_for(lambda: active(spec["hud"]) and navigation.exists())
                proc.stdin.write("中文 draft".encode()); proc.stdin.flush()
                self.wait_for(lambda: "中文 draft" in self.tmux(spec, "capture-pane", "-p", "-t", spec["top"]).stdout)
                self.assertFalse(navigation.exists())
                proc.stdin.write(b"\x15\r"); proc.stdin.flush()
                self.wait_for(lambda: self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{cursor_x}").stdout.strip() == "2")
                proc.stdin.write(b"\x1b[B"); proc.stdin.flush()
                self.wait_for(lambda: active(spec["hud"]) and navigation.exists())
                # /new (fixture reset) clears navigation from the previous root.
                self.send(spec, "reset")
                self.wait_for(lambda: self.snapshot(spec).get("root", {}).get("id") not in (None, identity))
                self.wait_for(lambda: active(spec["top"]) and not navigation.exists())
            self.wait_for(lambda: active(spec["top"]))
            self.assertEqual(self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout, before)
        self.send(spec, "quit")
        self.wait_for(lambda: not Path(spec["runtime"]).exists())

    def test_live_preferences_hide_and_restore_without_restarting_cli(self):
        spec = self.launch()
        # Missing-database diagnostics intentionally stay visible even when
        # every optional element is off; this fixture needs a healthy source.
        import sqlite3
        Path(spec["home"]).mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(Path(spec["home"]) / "state_5.sqlite") as db:
            db.executescript("CREATE TABLE threads(id TEXT,source TEXT,rollout_path TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);")
        self.wait_for(lambda: self.snapshot(spec).get("root", {}).get("model") == "fixture-model")
        pid = self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout.strip()
        from codex_hud.config import ELEMENTS
        config = Path(self.env["CODEX_HUD_CONFIG_HOME"]) / "display.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(json.dumps({"show": {key: False for key in ELEMENTS}}))
        def hidden():
            return self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{window_id}").stdout != self.tmux(spec, "display-message", "-p", "-t", spec["hud"], "#{window_id}").stdout
        self.wait_for(hidden)
        config.write_text(json.dumps({"language": "en", "show": {"tokens": False}}))
        self.wait_for(lambda: not hidden())
        self.wait_for(lambda: "Context" in self.tmux(spec, "capture-pane", "-p", "-t", spec["hud"]).stdout)
        panel = self.tmux(spec, "capture-pane", "-p", "-t", spec["hud"]).stdout
        self.assertNotIn("Main tokens", panel)
        self.assertEqual(self.tmux(spec, "display-message", "-p", "-t", spec["top"], "#{pane_pid}").stdout.strip(), pid)
        self.assertIn("config --menu", self.tmux(spec, "list-keys", "-T", "prefix").stdout)
        import shlex
        menu = self.tmux(spec, "new-window", "-d", "-n", "settings-test", "-P", "-F", "#{pane_id}", shlex.join([sys.executable, str(ENTRY), "config", "--menu"])).stdout.strip()
        self.wait_for(lambda: "Display settings" in self.tmux(spec, "capture-pane", "-p", "-t", menu).stdout)
        self.tmux(spec, "send-keys", "-t", menu, "-l", " ")
        self.wait_for(lambda: json.loads(config.read_text()).get("show", {}).get("model") is False)
        self.tmux(spec, "send-keys", "-t", menu, "-l", "q")
        self.tmux(spec, "kill-window", "-t", "settings-test")
        self.send(spec, "quit")
        self.wait_for(lambda: not Path(spec["runtime"]).exists())


if __name__ == "__main__":
    unittest.main()
