import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from codex_hud.agents import entries, transcript, details


class AgentsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        (self.home / "sessions").mkdir()
        self.db = sqlite3.connect(self.home / "state_5.sqlite")
        self.db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT)")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def save(self, identity, text):
        path = self.home / "sessions" / (identity+".jsonl")
        records = [{"type": "session_meta", "payload": {"id": identity}},
                   {"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}},
                   {"type": "response_item", "payload": {"type": "function_call", "call_id": "tool", "name": "exec_command", "arguments": "{}"}}]
        path.write_text("".join(json.dumps(r)+"\n" for r in records))
        self.db.execute("INSERT INTO threads VALUES(?,?)", (identity, str(path)))
        self.db.commit()
        return path

    def test_native_and_relay_entries_keep_distinct_identity_and_worker_link(self):
        snapshot = {"agents": [{"id": "child", "name": "research"}],
                    "offload": {"jobs": [{"id": "call", "worker_id": "worker"}]}}
        a, b = entries(snapshot)
        self.assertEqual(a["key"], ("native", "child"))
        self.assertEqual(a["thread"], "child")
        self.assertEqual(b["key"], ("relay", "call"))
        self.assertEqual(b["thread"], "worker")
        self.assertEqual(entries({"agents": [], "offload": {"jobs": []}}), [])

    def test_transcript_is_read_only_and_updates_without_resuming_worker(self):
        path = self.save("child", "First result")
        before = path.read_bytes()
        tools, messages, notice = transcript(self.home, {"thread": "child"})
        self.assertEqual(messages, ["First result"])
        self.assertEqual(tools[0]["name"], "exec_command")
        self.assertEqual(notice, "")
        self.assertEqual(path.read_bytes(), before)
        with path.open("a") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "agent_message", "content": [{"text": "Second result"}]}})+"\n")
        self.assertEqual(transcript(self.home, {"thread": "child"})[1][-1], "Second result")

    def test_main_entry_is_always_first_and_never_loads_a_transcript(self):
        from unittest.mock import patch
        snapshot = {"root": {"id": "parent", "status": "running", "skills": ["hud"]},
                    "agents": [{"id": "child"}]}
        main, skill, child = entries(snapshot)
        self.assertEqual(main["key"], ("main", "parent"))
        self.assertEqual(main["name"], "Main agent")
        with patch("codex_hud.agents.transcript", side_effect=AssertionError("Main is live")):
            self.assertIn("Main agent", details(self.home, main, 0)[0])

    def test_mismatched_identity_and_non_session_paths_are_rejected(self):
        path = self.save("different", "Private output")
        self.db.execute("INSERT INTO threads VALUES(?,?)", ("child", str(path)))
        outside = self.home / "private.jsonl"
        outside.write_text("private data")
        self.db.execute("INSERT INTO threads VALUES(?,?)", ("outside", str(outside)))
        self.db.commit()
        self.assertIn("identity mismatch", transcript(self.home, {"thread": "child"})[2])
        self.assertIn("outside Codex", transcript(self.home, {"thread": "outside"})[2])

    def test_inherited_parent_output_is_not_presented_as_child_output(self):
        path = self.save("child", "")
        records = [{"type": "session_meta", "timestamp": "2026-10-14T10:00:00Z", "payload": {"id": "child"}}]
        for stamp, message in [("2026-10-14T09:00:00Z", "Parent output"), ("2026-10-14T10:01:00Z", "Child output")]:
            records.append({"type": "response_item", "timestamp": stamp, "payload": {"type": "message", "role": "assistant", "content": [{"text": message}]}})
        path.write_text("".join(json.dumps(r)+"\n" for r in records))
        self.assertEqual(transcript(self.home, {"thread": "child"})[1], ["Child output"])

    def test_missing_and_encrypted_output_stays_unavailable(self):
        self.assertIn("no worker", transcript(self.home, {})[2])
        path = self.save("child", "")
        with path.open("a") as f:
            f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "encrypted_content", "data": "opaque"}]}})+"\n")
        self.assertEqual(transcript(self.home, {"thread": "child"})[1], [])
        self.assertIn("no readable", transcript(self.home, {"thread": "child"})[2])
        rows = details(self.home, {"id": "call", "kind": "relay", "thread": None, "status": "failed", "error": "network failed"}, 100, True)
        self.assertIn("Tokens: unavailable", rows)
        self.assertTrue(any("network failed" in r for r in rows))

    def test_malformed_records_do_not_crash_inspection(self):
        path = self.save("child", "Readable output")
        with path.open("a") as f:
            for value in [None, [], {"type": "response_item", "payload": {"type": "agent_message", "content": 7}}]:
                f.write(json.dumps(value)+"\n")
        self.assertEqual(transcript(self.home, {"thread": "child"})[1], ["Readable output"])

    def test_relay_returned_answer_is_available_without_worker_log(self):
        item = {"id": "call", "kind": "relay", "status": "completed", "output": "Returned result\nSecond line"}
        rows = details(self.home, item, 100)
        self.assertIn("Returned result", rows)
        self.assertIn("Second line", rows)
        self.assertFalse(any("no worker" in row for row in rows))


if __name__ == "__main__":
    unittest.main()
