import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codex_hud.keys import clear, down, empty_composer

EMPTY = "\x1b[1m\x1b[48;2;30;30;30m›\x1b[0m\x1b[48;2;30;30;30m \x1b[2mAsk Codex to do anything\x1b[0m\n"


class KeysTest(unittest.TestCase):
    def test_only_visible_default_dim_placeholder_is_empty_composer(self):
        self.assertTrue(empty_composer(EMPTY, 2))
        for line, x, visible, in_mode in [
            (EMPTY, 3, True, False), (EMPTY, 2, False, False),
            (EMPTY, 2, True, True), ("› typed draft", 2, True, False),
            (EMPTY.replace("\x1b[2m", ""), 2, True, False),
            ("› Ask Codex to do anything", 2, True, False),
            ("\x1b[2m› Select a model\n", 2, True, False),
        ]:
            with self.subTest(line=line, x=x, visible=visible, in_mode=in_mode):
                self.assertFalse(empty_composer(line, x, visible, in_mode))

    def test_rgb_and_palette_two_are_not_faint_flags(self):
        self.assertFalse(empty_composer("› \x1b[38;2;2;100;200mAsk Codex to do anything", 2))
        self.assertFalse(empty_composer("› \x1b[38;5;2mAsk Codex to do anything", 2))
        self.assertTrue(empty_composer("› \x1b[2;38;2;2;100;200mAsk Codex to do anything", 2))
        self.assertFalse(empty_composer(EMPTY.replace("\x1b[2m", "\x1b[2;22m"), 2))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-hud-key-test-")
        self.runtime = Path(self.temp.name)
        self.spec = {"socket": self.runtime.name, "top": "%0", "hud": "%1"}
        (self.runtime/"spec.json").write_text(json.dumps(self.spec), encoding="utf-8")
        (self.runtime/"snapshot.json").write_text(json.dumps({"root": {"id": "root"}}), encoding="utf-8")
        self.fields, self.line, self.hud_active = "1 0 2 4 1", EMPTY, "0"
        self.calls = []
        def tmux(socket, *args, **kwargs):
            self.calls.append(args)
            if args[0] == "capture-pane":
                result = self.line
            elif args[0] == "display-message":
                result = self.hud_active if args[-1] == "#{pane_active}" else self.fields
            else:
                result = ""
            return subprocess.CompletedProcess(args, 0, result)
        self.tmux = patch("codex_hud.cli.tmux", side_effect=tmux)
        self.tmux.start()
        self.render = patch("codex_hud.keys.render_rows", return_value=[SimpleNamespace(key=("native", "child"))])
        self.rows = self.render.start()

    def tearDown(self):
        self.render.stop()
        self.tmux.stop()
        self.temp.cleanup()

    def test_empty_down_selects_inline_row_and_sets_guarded_navigation(self):
        self.assertEqual(down(self.runtime, "%0"), 0)
        self.assertIn(("select-pane", "-t", "%1"), self.calls)
        self.assertEqual(json.loads((self.runtime/"navigation.json").read_text()), {"root": "root", "key": ["native", "child"]})
        self.assertFalse(any(x[0] == "send-keys" for x in self.calls))
        clear(self.runtime, self.spec)
        self.assertFalse((self.runtime/"navigation.json").exists())

    def test_draft_copy_mode_no_entries_and_unknown_composer_forward_down(self):
        for fields, line in [("1 0 3 4 1", EMPTY), ("1 1 2 4 1", EMPTY), ("1 0 2 4 1", "› draft")]:
            self.fields, self.line = fields, line
            down(self.runtime, "%0")
            self.assertEqual(self.calls[-1], ("send-keys", "-t", "%0", "Down"))
            self.assertFalse((self.runtime/"navigation.json").exists())
        self.rows.return_value = []
        down(self.runtime, "%0")
        self.assertEqual(self.calls[-1], ("send-keys", "-t", "%0", "Down"))

    def test_queued_down_after_handoff_goes_to_hud_and_foreign_pane_is_rejected(self):
        down(self.runtime, "%0")
        self.hud_active = "1"
        down(self.runtime, "%0")
        self.assertEqual(self.calls[-1], ("send-keys", "-t", "%1", "Down"))
        before = len(self.calls)
        self.assertEqual(down(self.runtime, "invalid;pane"), 1)
        self.assertEqual(len(self.calls), before)

    def test_snapshot_from_previous_root_is_never_selected(self):
        self.spec.update(diagnostic="private.log", home="home")
        (self.runtime/"spec.json").write_text(json.dumps(self.spec), encoding="utf-8")
        with patch("codex_hud.keys.Diagnostic.bind", return_value=("new-root", None)):
            down(self.runtime, "%0")
        self.assertEqual(self.calls[-1], ("send-keys", "-t", "%0", "Down"))
        self.assertFalse((self.runtime/"navigation.json").exists())


if __name__ == "__main__":
    unittest.main()
