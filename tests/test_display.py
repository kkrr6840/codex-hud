import unittest
from datetime import datetime

from codex_hud.display import render_rows, progress, cell_width, clip


class DisplayTest(unittest.TestCase):
    def snapshot(self):
        return {"time": 100, "root": {"id": "session", "cwd": "/work/project"},
                "agents": [], "agents_known": True,
                "offload": {"enabled": True, "model": "relay-model", "jobs": []},
                "git": {"branch": None, "changes": None}, "errors": []}

    def test_fresh_session_has_no_placeholder_or_empty_sections(self):
        snapshot = self.snapshot()
        self.assertEqual(render_rows(snapshot), [])
        snapshot["root"]["model"] = "test-model"
        text = "\n".join(row.text for row in render_rows(snapshot))
        self.assertIn("test-model", text)
        for absent in ("等待", "?", "Agents", "codex-x-codex", "Token", "上下文", "缓存"):
            self.assertNotIn(absent, text)

    def test_context_bar_uses_request_input_not_cumulative_tokens(self):
        snapshot = self.snapshot()
        snapshot["root"].update(model="test-model", context_used=250, context_window=1000,
                                usage={"input_tokens": 10000, "output_tokens": 200, "cached_input_tokens": 8000, "total_tokens": 10200})
        rows = render_rows(snapshot)
        context = next(row for row in rows if "上下文" in row.text)
        cache = next(row for row in rows if "缓存" in row.text)
        self.assertIn("25%", context.text)
        self.assertIn("250 / 1.0k", context.text)
        self.assertIn("80%", cache.text)
        self.assertIn("8.0k / 10.0k", cache.text)
        self.assertTrue(any("█" in text and kind == "good" for text, kind in context.spans))
        snapshot["root"]["context_used"] = 950
        context = next(row for row in render_rows(snapshot) if "上下文" in row.text)
        self.assertTrue(any("█" in text and kind == "bad" for text, kind in context.spans))

    def test_real_failures_and_unknown_returned_usage_remain_visible(self):
        snapshot = self.snapshot()
        snapshot["errors"] = ["会话日志不可读"]
        self.assertIn("会话日志不可读", render_rows(snapshot)[0].text)
        snapshot["offload"]["jobs"] = [{"model": "relay-model", "task": "test task", "status": "failed", "usage": None, "duration": 3}]
        text = "\n".join(row.text for row in render_rows(snapshot))
        self.assertEqual(text.count("未返回用量"), 1)
        self.assertIn("1 失败", text)

    def test_completed_agents_and_relay_jobs_leave_no_header_rows_or_overflow(self):
        snapshot = self.snapshot()
        completed = [{"id": str(i), "name": "completed-native", "model": "completed-relay", "status": "completed"} for i in range(8)]
        snapshot["agents"] = list(completed)
        snapshot["offload"]["jobs"] = list(completed)
        self.assertEqual(render_rows(snapshot), [])
        # Filtering changes the display, not telemetry or retained history.
        self.assertEqual(len(snapshot["agents"]), 8)
        self.assertEqual(len(snapshot["offload"]["jobs"]), 8)
        snapshot["agents"].append({"id": "active", "name": "still-running", "status": "running"})
        snapshot["offload"]["jobs"].append({"id": "failed", "model": "failed-relay", "status": "failed"})
        rows = render_rows(snapshot)
        text = "\n".join(r.text for r in rows)
        self.assertIn("still-running", text)
        self.assertIn("failed-relay", text)
        self.assertNotIn("completed-native", text)
        self.assertNotIn("completed-relay", text)
        self.assertNotIn("+", text)
        self.assertEqual([r.key for r in rows if r.key], [("native", "active"), ("relay", "failed")])
        visible = render_rows(snapshot, config={"agents_show_completed": True})
        self.assertTrue(any(r.key == ("native", "0") for r in visible))

    def test_quotas_use_window_length_and_show_remaining_with_local_reset(self):
        snapshot = self.snapshot()
        reset = 1792476628
        snapshot["root"]["rate_limits"] = {"limit_id": "codex",
            "primary": {"window_minutes": 10080, "used_percent": 17, "resets_at": reset},
            "secondary": {"window_minutes": 300, "used_percent": 92, "resets_at": reset}}
        rows = render_rows(snapshot)
        weekly = next(row for row in rows if "周额度" in row.text)
        hourly = next(row for row in rows if "5小时" in row.text)
        self.assertIn("剩余 83%", weekly.text)
        self.assertIn(datetime.fromtimestamp(reset).strftime("%m/%d %H:%M") + " 重置", weekly.text)
        self.assertIn("剩余 8%", hourly.text)
        self.assertTrue(any("█" in text and kind == "bad" for text, kind in hourly.spans))
        snapshot["root"]["rate_limits"]["secondary"] = None
        self.assertFalse(any("5小时" in row.text for row in render_rows(snapshot)))

    def test_missing_invalid_and_expired_quotas_never_become_full_allowance(self):
        snapshot = self.snapshot()
        for used in (None, "17", True, float("nan"), float("inf"), -1):
            snapshot["root"]["rate_limits"] = {"primary": {"window_minutes": 300, "used_percent": used}}
            self.assertEqual(render_rows(snapshot), [])
        snapshot["root"]["rate_limits"] = {"primary": {"window_minutes": 300, "used_percent": 92, "resets_at": 99}}
        text = "\n".join(row.text for row in render_rows(snapshot))
        self.assertIn("等待额度更新", text)
        self.assertNotIn("剩余", text)

    def test_bars_and_clipping_respect_terminal_cells(self):
        for fraction in (-.1, 0, .03, .5, 1, 1.2):
            self.assertEqual(cell_width("".join(progress(fraction, 14))), 14)
        self.assertLessEqual(cell_width(clip(" 中文abcdef", 8)), 8)
        self.assertTrue(clip(" 中文abcdef", 8).endswith("…"))
        self.assertEqual(clip("  │  ", 10), "  │  ")


if __name__ == "__main__":
    unittest.main()
