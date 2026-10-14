import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_hud.display import clean, elapsed, lines
from codex_hud.sources import Collector, Diagnostic, Rollout, offload_job, turn_state, response_json


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"CODEX_X_CODEX_HOME": str(self.home / "offload")})
        self.env.start()
        self.state = sqlite3.connect(self.home / "state_5.sqlite")
        self.state.executescript("""
        CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, cwd TEXT,
            model TEXT, agent_path TEXT, agent_nickname TEXT, title TEXT, first_user_message TEXT);
        CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);
        """)
        self.history = sqlite3.connect(self.home / "thread_history_1.sqlite")
        self.history.executescript("""
        CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
            status TEXT, started_at INTEGER, completed_at INTEGER, duration_ms INTEGER);
        CREATE TABLE thread_items(thread_id TEXT, item_id TEXT, item_type TEXT, item_json TEXT,
            rollout_ordinal INTEGER, updated_at_ordinal INTEGER, created_at_ms INTEGER,
            started_at_ms INTEGER, completed_at_ms INTEGER);
        """)

    def tearDown(self):
        self.state.close()
        self.history.close()
        self.env.stop()
        self.temp.cleanup()

    def thread(self, identity, parent=None, status="completed", task=None):
        path = self.home / f"{identity}.jsonl"
        meta = {"id": identity, "session_id": "root-A", "cwd": str(self.home), "parent_thread_id": parent}
        records = [dict(type="session_meta", payload=meta),
                   dict(type="turn_context", payload={"model": "actual-model"})]
        if task:
            records.append(dict(type="response_item", payload={"type": "agent_message", "content": [{"type": "input_text", "text": f"Message Type: NEW_TASK\nPayload:\n{task}"}]}))
        path.write_text("".join(json.dumps(x) + "\n" for x in records))
        self.state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?,?)", (identity, str(path), str(self.home), "actual-model", f"/root/{identity}", f"Nick-{identity}", "", ""))
        if parent:
            self.state.execute("INSERT INTO thread_spawn_edges VALUES(?,?,?)", (parent, identity, "open"))
        self.history.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)", (identity, "turn", 1, status, 100, 105 if status == "completed" else None, 5000 if status == "completed" else None))
        self.state.commit()
        self.history.commit()
        return path

    def job(self, owner, identity, status="completed", usage=None, ordinal=10):
        response = {"ok": True, "outcome": "completed", "model": "worker-model", "duration_seconds": 2.5, "usage": usage, "thread_id": "worker-id"}
        item = {"id": identity, "type": "commandExecution", "status": status,
                "commandActions": [{"command": "python3 /Users/test/Projects/codex-x-codex/scripts/offload.py run --task 'read one file'"}],
                "aggregatedOutput": json.dumps(response) if status == "completed" else ""}
        self.history.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?,?,?)", (owner, identity, "commandExecution", json.dumps(item), ordinal, ordinal, 100000, 100000, 102500 if status == "completed" else None))
        self.history.commit()

    def test_same_cwd_threads_are_isolated_and_nested_children_included(self):
        self.thread("root-A")
        self.thread("child-A", "root-A", "inProgress", "Check A")
        self.thread("grandchild-A", "child-A", task="Check nested")
        self.thread("root-B")
        self.thread("child-B", "root-B", "inProgress", "Private B")
        self.job("root-A", "a", usage={"input_tokens": 123})
        self.job("grandchild-A", "nested", usage={"input_tokens": 456})
        self.job("root-B", "b", usage={"input_tokens": 99999})
        snapshot = Collector(home=self.home, root_id="root-A").collect()
        self.assertEqual(snapshot["root"]["id"], "root-A")
        self.assertEqual({a["id"] for a in snapshot["agents"]}, {"child-A", "grandchild-A"})
        self.assertEqual({a["task"] for a in snapshot["agents"]}, {"Check A", "Check nested"})
        self.assertEqual(sum(j["usage"]["input_tokens"] for j in snapshot["offload"]["jobs"]), 579)
        self.assertFalse(snapshot["errors"])

    def test_model_selection_refreshes_without_new_turn_and_is_thread_scoped(self):
        path = self.thread("root-A")
        self.thread("root-B")
        self.state.execute("ALTER TABLE threads ADD COLUMN reasoning_effort TEXT")
        self.state.commit()
        before = path.read_bytes()
        collector = Collector(home=self.home, root_id="root-A")
        self.assertEqual(collector.collect()["root"]["model"], "actual-model")
        self.state.execute("UPDATE threads SET model='selected-model',reasoning_effort='high' WHERE id='root-A'")
        self.state.execute("UPDATE threads SET model='other-session-model' WHERE id='root-B'")
        self.state.commit()
        root = collector.collect()["root"]
        self.assertEqual(root["model"], "selected-model")
        self.assertEqual(root["effort"], "high")
        self.assertEqual(path.read_bytes(), before)
        self.state.execute("UPDATE threads SET reasoning_effort=NULL WHERE id='root-A'")
        self.state.commit()
        self.assertNotIn("effort", collector.collect()["root"])

    def test_missing_selected_model_falls_back_to_last_request(self):
        self.thread("root-A")
        self.state.execute("UPDATE threads SET model=NULL WHERE id='root-A'")
        self.state.commit()
        self.assertEqual(Collector(home=self.home, root_id="root-A").collect()["root"]["model"], "actual-model")

    def test_update_running_job_without_duplicating_or_mixing_global_usage(self):
        self.thread("root-A", status="inProgress")
        self.job("root-A", "a", "inProgress")
        (self.home / "offload").mkdir()
        (self.home / "offload/usage.jsonl").write_text('{"usage":{"input_tokens":999999}}\n')
        collector = Collector(home=self.home, root_id="root-A")
        first = collector.collect()
        self.assertEqual(first["offload"]["jobs"][0]["status"], "running")
        self.history.execute("DELETE FROM thread_items")
        self.job("root-A", "a", usage=None, ordinal=11)
        second = collector.collect()
        self.assertEqual(len(second["offload"]["jobs"]), 1)
        self.assertIsNone(second["offload"]["jobs"][0]["usage"])
        self.assertIn("未返回用量", "\n".join(text for text, kind in lines(second)))

    def test_partial_json_tail_and_actual_window_not_configuration(self):
        path = self.thread("root-A")
        reader = Rollout(path)
        reader.refresh()
        record = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 25, "total_tokens": 30}, "total_token_usage": {"total_tokens": 200}, "model_context_window": 100}}})
        with path.open("a") as h:
            h.write(record[:50])
        self.assertNotIn("context_used", reader.refresh())
        with path.open("a") as h:
            h.write(record[50:] + "\n")
        data = reader.refresh()
        self.assertEqual(data["context_used"], 25)
        self.assertEqual(data["context_window"], 100)
        self.assertEqual(data["usage"]["total_tokens"], 200)

    def test_partial_first_line_is_retried_before_advancing_offset(self):
        path = self.home / "partial.jsonl"
        meta = json.dumps({"type": "session_meta", "payload": {"id": "root-A"}}) + "\n"
        path.write_text(meta[:20])
        reader = Rollout(path)
        self.assertEqual(reader.refresh(), {})
        self.assertEqual(reader.offset, 0)
        with path.open("a") as handle:
            handle.write(meta[20:])
        self.assertEqual(reader.refresh()["id"], "root-A")

    def test_agent_identity_uses_id_not_inherited_session_id(self):
        path = self.thread("child-A", "root-A")
        self.assertEqual(Rollout(path).refresh()["id"], "child-A")

    def test_quota_only_events_replace_missing_windows_and_stay_session_scoped(self):
        first = Rollout(self.thread("root-A"))
        second = Rollout(self.thread("root-B"))
        first.refresh()
        second.refresh()
        limits = {"primary": {"window_minutes": 300, "used_percent": 12},
                  "secondary": {"window_minutes": 10080, "used_percent": 17}}
        def event(value):
            return {"type": "event_msg", "payload": {"type": "token_count", "info": None, "rate_limits": value}}
        first.consume(event(limits))
        self.assertEqual(first.data["rate_limits"], limits)
        self.assertNotIn("rate_limits", second.data)
        first.consume(event({"primary": limits["secondary"], "secondary": None}))
        self.assertIsNone(first.data["rate_limits"]["secondary"])
        self.assertEqual(first.data["rate_limits"]["primary"]["window_minutes"], 10080)
        first.consume(event(None))
        self.assertIsNone(first.data["rate_limits"])

    def test_explicit_thread_tokens_are_not_replaced_by_aggregate_or_child_usage(self):
        reader = Rollout(self.thread("root-A"))
        reader.refresh()
        reader.consume({"type": "token_usage_record", "payload": {"thread_id": "root-A", "usage": {"input_tokens": 25}, "thread_token_usage": {"total_tokens": 100}}})
        reader.consume({"type": "token_usage_record", "payload": {"thread_id": "child-A", "usage": {"input_tokens": 999}, "thread_token_usage": {"total_tokens": 99999}}})
        reader.consume({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"total_tokens": 555}, "last_token_usage": {"input_tokens": 44}, "model_context_window": 100}}})
        self.assertEqual(reader.data["usage"]["total_tokens"], 100)
        self.assertEqual(reader.data["context_used"], 25)
        self.assertEqual(reader.data["context_window"], 100)

    def test_tool_activity_and_plan_are_scoped_to_the_recorded_thread(self):
        reader = Rollout(self.thread("root-A"))
        reader.refresh()
        reader.consume({"type": "response_item", "timestamp": 100, "payload": {"type": "function_call", "name": "update_plan", "call_id": "plan1", "arguments": json.dumps({"plan": [{"step": "Test", "status": "in_progress"}]})}})
        self.assertEqual(reader.data["todos"][0]["step"], "Test")
        self.assertEqual(reader.data["tools"][0]["status"], "running")
        reader.consume({"type": "response_item", "timestamp": 103, "payload": {"type": "function_call_output", "call_id": "plan1", "output": '{"exit_code":1}'}})
        self.assertEqual(reader.data["tools"][0]["status"], "failed")
        self.assertEqual(reader.data["tools"][0]["duration"], 3)
        self.assertNotIn("tools", Rollout(self.thread("root-B")).refresh())

    def test_stale_interrupted_completed_and_unknown_are_distinct(self):
        self.assertEqual(turn_state({"status": "inProgress", "started_at": 1}, launched_at=50), "unknown")
        self.assertEqual(turn_state({"status": "completed"}), "completed")
        self.assertEqual(turn_state({"status": "completed"}, root=True), "idle")
        self.assertEqual(turn_state({"status": "interrupted"}), "idle")
        self.assertEqual(turn_state(None), "unknown")
        self.assertEqual(elapsed({"status": "unknown", "started": 100}, 500), "耗时未知")

    def test_missing_and_corrupt_source_dont_become_zero(self):
        empty = Collector(home=self.home / "missing", root_id="missing").collect()
        self.assertIsNone(empty["root"].get("usage"))
        self.assertFalse(empty["agents_known"])
        self.assertIn("数据库不可用", "\n".join(text for text, kind in lines(empty)))
        (self.home / "offload").mkdir()
        (self.home / "offload/config.json").write_text("broken")
        self.assertIsNone(Collector(home=self.home).collect()["offload"]["enabled"])

    def test_terminal_escape_and_bidi_are_removed(self):
        self.assertNotIn("\x1b", clean("evil\x1b[2J\u202ehidden"))
        self.assertNotIn("\u202e", clean("evil\u202e"))

    def test_offload_status_command_is_not_a_job(self):
        row = {"thread_id": "a", "item_json": json.dumps({"command": "python3 /path/codex-x-codex/scripts/offload.py status"})}
        self.assertIsNone(offload_job(row, "model"))

    def test_rebind_on_cli_new_clears_previous_agents_and_usage(self):
        a = self.thread("root-A")
        b = self.thread("root-B")
        self.thread("child-A", "root-A")
        self.job("root-A", "a", usage={"input_tokens": 123})
        collector = Collector(home=self.home, runner=123)
        with patch("codex_hud.sources.bind_rollout", side_effect=[a, b, None]):
            first = collector.collect()
            self.assertEqual(len(first["agents"]), 1)
            collector.last_bind = 0
            second = collector.collect()
            self.assertEqual(second["root"]["id"], "root-B")
            self.assertEqual(second["agents"], [])
            self.assertEqual(second["offload"]["jobs"], [])
            collector.last_bind = 0
            third = collector.collect()
            self.assertIsNone(third["root"]["id"])

    def test_tool_session_proves_running_and_completed_projection_deduplicates(self):
        path = self.thread("root-A")
        command = "python3 /Users/test/Projects/codex-x-codex/scripts/offload.py run --task 'read one file'"
        records = [
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "call-start", "input": "text(await tools.exec_command({cmd:" + json.dumps(command) + "}));"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "call-start", "output": [{"type": "input_text", "text": json.dumps({"session_id": 456, "output": ""})}]}},
        ]
        with path.open("a") as h:
            h.write("".join(json.dumps(x) + "\n" for x in records))
        collector = Collector(home=self.home, root_id="root-A")
        self.assertEqual(collector.collect()["offload"]["jobs"][0]["status"], "running")
        self.job("root-A", "projected", usage={"input_tokens": 500})
        item = json.loads(self.history.execute("SELECT item_json FROM thread_items").fetchone()[0])
        item["processId"] = "456"
        self.history.execute("UPDATE thread_items SET item_json=?", (json.dumps(item),))
        self.history.commit()
        jobs = collector.collect()["offload"]["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "completed")
        self.assertEqual(jobs[0]["usage"]["input_tokens"], 500)

    def test_plugin_json_field_order_is_irrelevant(self):
        response = response_json('{"usage":{"input_tokens":42},"outcome":"completed","ok":true}')
        self.assertEqual(response["usage"]["input_tokens"], 42)

    def test_unreadable_log_does_not_keep_stale_tokens(self):
        path = self.thread("root-A")
        reader = Rollout(path)
        reader.refresh()
        reader.data["usage"] = {"total_tokens": 99}
        path.unlink()
        data = reader.refresh()
        self.assertNotIn("usage", data)
        self.assertIn("error", data)
        self.assertTrue(Collector(home=self.home, root_id="root-A").collect()["errors"])

    def test_yielded_exec_cell_is_matched_to_later_wait(self):
        path = self.thread("root-A")
        command = "python3 /path/codex-x-codex/scripts/offload.py run --task 'check'"
        response = {"ok": True, "outcome": "completed", "usage": {"input_tokens": 789}, "thread_id": "worker-unique"}
        records = [
            {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "a", "input": "text(await tools.exec_command({cmd:" + json.dumps(command) + "}));"}},
            {"type": "response_item", "payload": {"type": "custom_tool_call_output", "call_id": "a", "output": "Script running with cell ID 17\n"}},
            {"type": "response_item", "payload": {"type": "function_call", "name": "wait", "call_id": "b", "arguments": '{"cell_id":"17"}'}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "b", "output": [{"text": json.dumps({"exit_code": 0, "output": json.dumps(response)})}]}},
        ]
        with path.open("a") as h:
            h.write("".join(json.dumps(x) + "\n" for x in records))
        jobs = Collector(home=self.home, root_id="root-A").collect()["offload"]["jobs"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["status"], "completed")
        self.assertEqual(jobs[0]["usage"]["input_tokens"], 789)

    def test_failed_ownership_check_clears_previous_session(self):
        a = self.thread("root-A")
        collector = Collector(home=self.home, runner=123)
        with patch("codex_hud.sources.bind_rollout", side_effect=[a, OSError("ownership unavailable")]):
            self.assertEqual(collector.collect()["root"]["id"], "root-A")
            collector.last_bind = 0
            snapshot = collector.collect()
            self.assertIsNone(snapshot["root"]["id"])
            self.assertTrue(snapshot["errors"])

    def test_owned_diagnostic_switches_empty_session_and_ignores_auxiliary_threads(self):
        a = "00000000-0000-0000-0000-000000000001"
        b = "00000000-0000-0000-0000-000000000002"
        child = "00000000-0000-0000-0000-000000000003"
        title_worker = "00000000-0000-0000-0000-000000000004"
        pa, pb = self.thread(a), self.thread(b)
        self.thread(child, a)
        self.state.execute("ALTER TABLE threads ADD COLUMN source TEXT")
        self.state.execute("UPDATE threads SET source='cli' WHERE id IN (?,?)", (a, b))
        self.state.execute("UPDATE threads SET source='subagent' WHERE id=?", (child,))
        self.state.commit()
        log = self.home / "diagnostic.log"
        def event(identity):
            return f"2026-10-13T02:00:00Z INFO session_loop{{thread_id={identity}}}: codex_core::session: new\n"
        persistent = '2026-10-13T02:00:00Z INFO thread_spawn{otel.name="thread_spawn"}:session_init:session_init.thread_persistence{otel.name="session_init.thread_persistence" session_init.ephemeral=false}: codex_core::session::session: new\n'
        log.write_text(persistent + event(a) + event(child) + event(title_worker))
        diagnostic = Diagnostic(log)
        self.assertEqual(diagnostic.bind(self.home), (a, pa))
        # A real /new has a persisted root before a rollout or first turn exists.
        pb.unlink()
        self.history.execute("DELETE FROM thread_turns WHERE thread_id=?", (b,))
        self.history.commit()
        with log.open("a") as h:
            h.write(persistent + event(b))
        collector = Collector(home=self.home, runner=123, diagnostic=log)
        snapshot = collector.collect()
        self.assertEqual(snapshot["root"]["id"], b)
        self.assertEqual(snapshot["root"]["status"], "idle")
        self.assertEqual(snapshot["agents"], [])
        self.assertFalse(snapshot["errors"])
        empty = "00000000-0000-0000-0000-000000000005"
        with log.open("a") as h:
            h.write(persistent + event(empty))
        collector.last_bind = 0
        snapshot = collector.collect()
        self.assertEqual(snapshot["root"]["id"], empty)
        self.assertEqual(snapshot["agents"], [])

    def test_git_initial_branch_and_dirty_worktree(self):
        subprocess.run(["git", "init", "-q", "-b", "hud-test", str(self.home / "repo")], check=True)
        (self.home / "repo/file.txt").write_text("fixture")
        state = Collector(home=self.home).git(str(self.home / "repo"))
        self.assertEqual(state["branch"], "hud-test")
        self.assertEqual(state["changes"], 1)


if __name__ == "__main__":
    unittest.main()
