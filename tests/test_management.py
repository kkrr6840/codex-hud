import copy
import unittest
import tempfile
import json
from pathlib import Path
from unittest.mock import patch

from codex_hud.agents import entries, details
from codex_hud.management import actions, stamp, request_text, stage_request


class ManagementTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"root": {"id": "parent"}, "agents": [
            {"id": "child", "status": "running", "turn_id": "turn-A", "started": 10}],
            "offload": {"jobs": [{"id": "call-1", "status": "failed", "ended": 20, "worker_id": "worker"}]}}

    def test_requests_are_scoped_single_line_drafts_and_never_mutate_telemetry(self):
        before = copy.deepcopy(self.snapshot)
        for item, action in zip(entries(self.snapshot), ("stop", "retry")):
            text = request_text(self.snapshot, "parent", item["key"], action, stamp(item))
            self.assertIn(item["id"], text)
            self.assertIn("主会话", text)
            self.assertNotIn("\n", text)
            self.assertNotIn("\r", text)
        self.assertEqual(self.snapshot, before)
        self.snapshot["display"] = {"language": "en"}
        item = entries(self.snapshot)[0]
        self.assertIn("Codex HUD task request", request_text(self.snapshot, "parent", item["key"], "stop", stamp(item)))

    def test_foreign_stale_main_and_unsupported_targets_are_rejected(self):
        item = entries(self.snapshot)[0]
        for root, key, action, expected in [
            ("other", item["key"], "stop", stamp(item)),
            ("parent", ("native", "foreign"), "stop", stamp(item)),
            ("parent", item["key"], "retry", stamp(item)),
            ("parent", item["key"], "stop", ["old-turn", 10, None, "running"]),
            ("parent", item["key"], "pause", stamp(item))]:
            with self.subTest(key=key, action=action), self.assertRaises(ValueError):
                request_text(self.snapshot, root, key, action, expected)
        self.snapshot["agents"][0]["id"] = "parent"
        with self.assertRaises(ValueError):
            request_text(self.snapshot, "parent", ("native", "parent"), "stop", stamp(item))

    def test_unknown_active_state_and_skills_have_no_task_actions(self):
        self.assertEqual(actions({"kind": "relay", "status": "unknown"}), [])
        self.assertEqual(actions({"kind": "relay", "status": "unknown", "ended": 1}), ["retry"])
        self.assertEqual(actions({"kind": "skills", "status": "running"}), [])
        self.assertEqual(actions({"kind": "native", "status": "idle", "ended": 1}), ["retry"])
        self.assertEqual(actions({"kind": "native", "status": "idle"}), [])

    def test_unverified_runtime_identity_never_touches_terminal_input(self):
        with tempfile.TemporaryDirectory(prefix="codex-hud-") as folder:
            runtime = Path(folder)
            spec = {"socket": runtime.name, "top": "%0", "home": folder, "diagnostic": str(runtime/"missing.log")}
            (runtime/"spec.json").write_text(json.dumps(spec))
            (runtime/"snapshot.json").write_text(json.dumps(self.snapshot))
            with patch("codex_hud.cli.tmux") as terminal:
                with self.assertRaises(ValueError):
                    stage_request(runtime, "parent", ("native", "child"), "stop", stamp(self.snapshot["agents"][0]))
                terminal.assert_not_called()

    def test_skill_details_use_recorded_metadata_without_opening_paths(self):
        self.snapshot["root"].update(skills=["hud"], skill_turn_id="turn-A", skill_reads=[
            {"name": "hud", "path": "/missing/private/SKILL.md", "description": "HUD display", "source": "codex-hud", "read_at": 100}])
        item = entries(self.snapshot)[0]
        self.assertEqual(item["key"], ("skills", "turn-A"))
        rendered = "\n".join(details("/nonexistent", item, 101, True))
        self.assertIn("/missing/private/SKILL.md", rendered)
        self.assertIn("HUD display", rendered)
        self.assertIn("codex-hud", rendered)
        self.assertNotIn("日志不可读取", rendered)
