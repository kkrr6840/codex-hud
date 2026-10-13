import unittest

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
        snapshot["offload"]["jobs"] = [{"model": "relay-model", "task": "test task", "status": "completed", "usage": None, "duration": 3}]
        text = "\n".join(row.text for row in render_rows(snapshot))
        self.assertIn("未知用量 1 项", text)
        self.assertIn("用量未知", text)

    def test_bars_and_clipping_respect_terminal_cells(self):
        for fraction in (-.1, 0, .03, .5, 1, 1.2):
            self.assertEqual(cell_width("".join(progress(fraction, 14))), 14)
        self.assertLessEqual(cell_width(clip(" 中文abcdef", 8)), 8)
        self.assertTrue(clip(" 中文abcdef", 8).endswith("…"))
        self.assertEqual(clip("  │  ", 10), "  │  ")


if __name__ == "__main__":
    unittest.main()
