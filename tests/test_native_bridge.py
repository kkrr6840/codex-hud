"""Real Python bridge, Unicode output, scoped details and live configuration."""
import json
import os
from pathlib import Path
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from codex_hud.native import Bridge
from codex_hud.sources import atomic_json

ENTRY = Path(__file__).resolve().parents[1]/"codex-hud"


class NativeBridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)/"中文目录"
        self.runtime = self.base/"runtime"
        self.home = self.base/"home"
        self.runtime.mkdir(parents=True)
        (self.home/"sessions").mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "CODEX_HUD_CONFIG_HOME": str(self.base/"settings"),
            "CODEX_X_CODEX_HOME": str(self.base/"relay"),
        })
        self.env.start()
        self.root = "00000000-0000-0000-0000-000000000001"
        self.child = "00000000-0000-0000-0000-000000000002"
        self.other = "00000000-0000-0000-0000-000000000003"
        self.db = sqlite3.connect(self.home/"state_5.sqlite")
        self.db.executescript("CREATE TABLE threads(id TEXT,source TEXT,rollout_path TEXT,cwd TEXT,model TEXT,agent_nickname TEXT); CREATE TABLE thread_spawn_edges(parent_thread_id TEXT,child_thread_id TEXT,status TEXT);")
        self.record(self.root, "cli", "主会话输出")
        self.record(self.child, "subagent", "中文 agent 结果")
        self.record(self.other, "cli", "另一会话私有结果")
        self.db.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (self.root, self.child, "open"))
        self.db.commit()
        self.log = self.runtime/"codex-tui.log"
        self.identify(self.root)
        atomic_json(self.runtime/"spec.json", {"runtime": str(self.runtime), "home": str(self.home), "diagnostic": str(self.log), "cwd": str(self.base), "hud_height": 11})
        atomic_json(self.runtime/"runner.json", {"pid": 123, "started": time.time()})
        self.proc = None

    def record(self, identity, source, text):
        path = self.home/"sessions"/(identity+".jsonl")
        records = [{"type": "session_meta", "payload": {"id": identity, "source": source, "cwd": str(self.base)}},
                   {"type": "turn_context", "payload": {"model": "fixture-model"}},
                   {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}]
        path.write_text("".join(json.dumps(x, ensure_ascii=False)+"\n" for x in records), encoding="utf-8")
        self.db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", (identity, source, str(path), str(self.base), "fixture-model", "测试 agent"))

    def identify(self, identity):
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write('2026-10-14T02:00:00Z INFO thread_spawn{otel.name="thread_spawn"}:session_init:session_init.thread_persistence{otel.name="session_init.thread_persistence" session_init.ephemeral=false}: codex_core::session::session: new\n')
            handle.write(f"2026-10-14T02:00:00Z INFO session_loop{{thread_id={identity}}}: codex_core::session: new\n")

    def tearDown(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.stdin.close()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            self.proc.stdout.close()
            self.proc.stderr.close()
        self.db.close()
        self.env.stop()
        self.temp.cleanup()

    def test_details_are_scoped_read_only_and_live_config_keeps_unknown_fields(self):
        state = Bridge(self.runtime)
        packet = state.collect()
        self.assertEqual(packet["snapshot"]["root"]["id"], self.root)
        self.assertEqual([r["key"] for r in packet["all_rows"] if r["key"]], [("native", self.child)])
        request = {"type": "detail", "request_id": 8, "root": self.root, "key": ["native", self.child]}
        before = {p: p.read_bytes() for p in (self.home/"sessions").glob("*.jsonl")}
        response = state.request(request)
        self.assertIn("中文 agent 结果", response["lines"])
        self.assertEqual(response["request_id"], 8)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        foreign = state.request({**request, "key": ["native", self.other]})
        self.assertIn("error", foreign)
        self.assertNotIn("另一会话私有结果", json.dumps(foreign, ensure_ascii=False))
        self.assertEqual(foreign["key"], ("native", self.other))
        self.assertIn("error", state.request({**request, "root": "old-root"}))
        state.request({"type": "config", "changes": {"language": "en", "custom_text": "保留", "advanced": {"keep": True}}})
        self.assertNotIn("error", state.request({"type": "config", "changes": {"preset": "minimal"}}))
        data = state.collect()["snapshot"]["display"]
        self.assertEqual(data["custom_text"], "保留")
        self.assertEqual(data["advanced"], {"keep": True})
        self.assertFalse(data["show"]["tokens"])
        self.assertIn("error", state.request({"type": "config", "changes": {"preset": "unknown"}}))
        self.assertTrue((self.runtime/"snapshot.json").is_file())

    def test_real_jsonl_process_refreshes_model_and_root_then_exits_on_eof(self):
        self.proc = subprocess.Popen([sys.executable, str(ENTRY), "_native_bridge", str(self.runtime)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
        incoming = queue.Queue()
        errors, seen = [], []
        def reader():
            for line in self.proc.stdout:
                incoming.put(json.loads(line))
        def stderr():
            for line in self.proc.stderr:
                errors.append(line)
        threading.Thread(target=reader, daemon=True).start()
        threading.Thread(target=stderr, daemon=True).start()
        def receive(predicate):
            deadline = time.monotonic()+8
            while time.monotonic() < deadline:
                try:
                    value = incoming.get(timeout=.2)
                except queue.Empty:
                    if self.proc.poll() is not None:
                        self.fail(f"bridge exited {self.proc.returncode}: {''.join(errors)}")
                    continue
                seen.append((value.get("type"), value.get("snapshot", {}).get("root", {}).get("id")))
                if predicate(value):
                    return value
            self.fail(f"bridge did not emit expected packet; seen={seen}; stderr={''.join(errors)}")
        def send(value):
            self.proc.stdin.write(json.dumps(value)+"\n")
            self.proc.stdin.flush()
        receive(lambda x: x.get("type") == "snapshot" and x["snapshot"]["root"]["id"] == self.root)
        send({"type": "detail", "request_id": 1, "root": self.root, "key": ["native", self.child]})
        self.assertIn("中文 agent 结果", receive(lambda x: x.get("request_id") == 1)["lines"])
        self.db.execute("UPDATE threads SET model='switched-model' WHERE id=?", (self.root,))
        self.db.commit()
        receive(lambda x: x.get("type") == "snapshot" and x["snapshot"]["root"].get("model") == "switched-model")
        self.identify(self.other)
        receive(lambda x: x.get("type") == "snapshot" and x["snapshot"]["root"].get("id") == self.other)
        send({"type": "detail", "request_id": 2, "root": self.root, "key": ["native", self.child]})
        self.assertIn("error", receive(lambda x: x.get("request_id") == 2))
        self.proc.stdin.close()
        self.assertEqual(self.proc.wait(timeout=5), 0, "".join(errors))


if __name__ == "__main__":
    unittest.main()
