from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_hud.config import DEFAULT, load, save, path, preset, validate
from codex_hud.display import render_rows, paint, color_index


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"CODEX_HUD_CONFIG_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def snapshot(self):
        return {"time": 100, "root": {"model": "test-model", "context_used": 25, "context_window": 100,
            "usage": {"input_tokens": 1000, "output_tokens": 20, "cached_input_tokens": 800, "total_tokens": 1020}},
            "agents": [], "offload": {"jobs": [], "enabled": True}, "git": {}, "errors": []}

    def test_partial_changes_preserve_manual_values_and_reload(self):
        path().write_text(json.dumps({"future_option": 42, "colors": {"model": "#33ff55"}, "show": {"cache": False}}))
        save({"show": {"tools": False}})
        cfg, error = load()
        self.assertIsNone(error)
        self.assertEqual(cfg["future_option"], 42)
        self.assertEqual(cfg["colors"]["model"], "#33ff55")
        self.assertFalse(cfg["show"]["cache"])
        self.assertFalse(cfg["show"]["tools"])
        save({"show": {"cache": True}})
        self.assertTrue(load()[0]["show"]["cache"])
        self.assertEqual(path().stat().st_mode & 0o777, 0o600)

    def test_override_wins_and_is_never_written(self):
        override = path().with_name("override.json")
        contents = '{"show":{"tokens":false}}'
        override.write_text(contents)
        save({"show": {"tokens": True}})
        self.assertFalse(load()[0]["show"]["tokens"])
        self.assertEqual(override.read_text(), contents)

    def test_launch_height_is_fallback_and_explicit_preferences_win(self):
        self.assertEqual(load({"height": 6})[0]["height"], 6)
        save({"height": 8})
        self.assertEqual(load({"height": 6})[0]["height"], 8)

    def test_watch_once_obeys_effective_display_preferences(self):
        from codex_hud.cli import observe
        from io import StringIO
        save({"show": {key: False for key in DEFAULT["show"]}})
        with patch("codex_hud.cli.Collector") as collector, patch("sys.stdout", new_callable=StringIO) as output:
            collector.return_value.collect.return_value = self.snapshot()
            self.assertEqual(observe(["--session", "test-session", "--once"]), 0)
            self.assertNotIn("test-model", output.getvalue())

    def test_invalid_preferences_do_not_break_rendering_or_overwrite_file(self):
        save({"layout": "compact"})
        original = path().read_bytes()
        for value in [{"show": {"tools": "yes"}}, {"bar_filled": "💥"}, {"order": ["quota", "quota"]}, {"colors": {"model": -1}}, {"warning_percent": 99, "critical_percent": 70}]:
            with self.assertRaises(ValueError):
                save(value)
            self.assertEqual(path().read_bytes(), original)
        path().write_text("broken")
        self.assertIsNotNone(load()[1])
        self.assertEqual(load()[0], DEFAULT)

    def test_visibility_order_and_english_are_effective(self):
        cfg = deepcopy(DEFAULT)
        cfg["show"]["tokens"] = False
        cfg["show"]["cache"] = False
        cfg["order"] = ["context", "header"]
        cfg["language"] = "en"
        rows = render_rows(self.snapshot(), config=cfg)
        self.assertIn("Context", rows[0].text)
        self.assertNotIn("Main tokens", "\n".join(r.text for r in rows))
        self.assertFalse(any("Cache" in r.text for r in rows))
        cfg["show"] = {key: False for key in cfg["show"]}
        self.assertEqual(render_rows(self.snapshot(), config=cfg), [])

    def test_compact_merges_when_it_fits_and_breaks_when_narrow(self):
        cfg = deepcopy(DEFAULT)
        cfg.update(layout="compact", context_bar=False, context_value="percent")
        wide = render_rows(self.snapshot(), 200, cfg)
        narrow = render_rows(self.snapshot(), 40, cfg)
        self.assertLess(len(wide), len(narrow))

    def test_all_presets_are_valid_and_keep_thresholds_separate(self):
        for name in ("minimal", "standard", "full"):
            save(preset(name))
            validate(load()[0])
        self.assertIn(color_index("#00ff00"), range(256))

    def test_expanded_view_can_scroll_past_all_header_rows(self):
        class Screen:
            def __init__(self): self.writes = []
            def getmaxyx(self): return 3, 100
            def erase(self): self.writes = []
            def addstr(self, y, x, text, attr): self.writes.append(text)
            def refresh(self): pass
        screen = Screen()
        snapshot = self.snapshot()
        with patch("codex_hud.display.curses.color_pair", return_value=0):
            paint(screen, snapshot, 999, expanded=True)
        self.assertIn("缓存", "".join(screen.writes))


if __name__ == "__main__":
    unittest.main()
