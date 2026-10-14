import unittest

from codex_hud.skills import read_skills


class SkillReadTest(unittest.TestCase):
    def item(self, actions, output="---\nname: hud\n---\n# HUD\n", **extra):
        return {"status": "completed", "exitCode": 0, "aggregatedOutput": output, "commandActions": actions, **extra}

    def test_structured_reads_keep_plugin_namespaces_and_windows_paths(self):
        paths = ["/home/.codex/plugins/cache/hud-market/codex-hud/0.7.0/skills/hud/SKILL.md", "C:\\Users\\me\\.agents\\skills\\openai-docs\\SKILL.md"]
        item = self.item([{"type": "read", "path": p} for p in paths])
        self.assertEqual(read_skills(item), ["codex-hud:hud", "openai-docs"])

    def test_conditional_reads_require_returned_skill_instructions(self):
        command = "if [ -f '/home/my skills/hud/SKILL.md' ]; then cat '/home/my skills/hud/SKILL.md'; else echo missing; fi"
        actions = [{"type": "unknown", "command": command}]
        self.assertEqual(read_skills(self.item(actions)), ["hud"])
        self.assertEqual(read_skills(self.item(actions, output="missing\n")), [])

    def test_searches_quoted_examples_failed_reads_and_pending_commands_are_not_loaded_skills(self):
        for action in [{"type": "search", "path": "/skills/hud/SKILL.md"},
                       {"type": "unknown", "command": "echo 'cat /skills/hud/SKILL.md'"},
                       {"type": "unknown", "command": "python3 - <<'PY'\ncat /skills/hud/SKILL.md\nPY"},
                       {"type": "read", "path": "/work/README.md"}]:
            self.assertEqual(read_skills(self.item([action])), [])
        action = [{"type": "read", "path": "/skills/hud/SKILL.md"}]
        self.assertEqual(read_skills(self.item(action, exitCode=1)), [])
        self.assertEqual(read_skills(self.item(action, status="inProgress")), [])


if __name__ == "__main__":
    unittest.main()
