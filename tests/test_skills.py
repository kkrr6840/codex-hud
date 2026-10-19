import unittest

from codex_hud.skills import read_skill_records, read_skills


class SkillReadTest(unittest.TestCase):
    def records(self, paths, output):
        return read_skill_records(self.item([{"type": "read", "path": p} for p in paths], output))

    def test_descriptions_and_original_paths(self):
        paths = ["./技能/界面/SKILL.md", r"C:\Users\me\skills\docs\SKILL.md",
                 "/plugins/cache/market/tool/1/skills/hud/SKILL.md"]
        output = ('---\nname: "界面"\ndescription: "用户界面"\n---\n'
                  "---\nname: 'docs'\ndescription: 'It''s documentation'\n---\n"
                  '---\nname: hud\ndescription: >-\n  First line\n  second line\n---\n')
        self.assertEqual(self.records(paths, output), [
            dict(name="界面", path=paths[0], description="用户界面", source="local"),
            dict(name="docs", path=paths[1], description="It's documentation", source="local"),
            dict(name="tool:hud", path=paths[2], description="First line second line", source="tool")])

    def test_ambiguous_names_and_path_collisions(self):
        paths = ["/a/hud/SKILL.md", "./b/hud/SKILL.md", "/a/hud/SKILL.md"]
        output = "---\nname: hud\ndescription: one\n---\n"
        records = self.records(paths, output)
        self.assertEqual([r["path"] for r in records], paths[:2])
        self.assertEqual([r["description"] for r in records], ["", ""])
        self.assertEqual(read_skills(self.item([{"type": "read", "path": p} for p in paths], output)), ["hud"])
        for ambiguous in (output + output, "---\nname: hud\nname: other\ndescription: wrong\n---\n",
                          "---\nname: other\ndescription: wrong\n---\n", "# HUD\ndescription: body text"):
            self.assertEqual(self.records(paths[:1], ambiguous)[0]["description"], "")

    def test_namespaced_attribution(self):
        paths = ["/plugins/cache/market/one/1/skills/hud/SKILL.md",
                 "/plugins/cache/market/two/1/skills/hud/SKILL.md"]
        output = "---\nname: hud\ndescription: ambiguous\n---\n"
        self.assertEqual([r["description"] for r in self.records(paths, output)], ["", ""])
        output = "---\nname: one:hud\ndescription: unique\n---\n"
        self.assertEqual([r["description"] for r in self.records(paths, output)], ["unique", ""])

    def test_unknown_windows_relative_and_unicode_paths(self):
        for path in (r"C:\Users\my skills\hud\SKILL.md", "./技能/hud/SKILL.md", "../hud/SKILL.md"):
            for reader in ("cat", "Get-Content"):
                item = self.item([{"type": "unknown", "command": f'{reader} "{path}"'}])
                self.assertEqual(read_skill_records(item), [dict(name="hud", path=path, description="", source="local")])
                self.assertEqual(read_skill_records(dict(item, aggregatedOutput="---\nname: other\n---\n")), [])
        for path in (r"C:\skills\hud\SKILL.md", "./技能/hud/SKILL.md"):
            item = self.item([{"type": "unknown", "command": f"cat {path}"}])
            self.assertEqual(read_skill_records(item)[0]["path"], path)

    def test_no_records_without_successful_nonempty_command_output(self):
        item = self.item([{"type": "read", "path": "/hud/SKILL.md"}])
        for extra in ({"exitCode": 1}, {"exitCode": None}, {"status": "inProgress"},
                      {"aggregatedOutput": " \n"}, {"aggregatedOutput": None},
                      {"aggregatedOutput": ["text"]}, {"type": "agentMessage"}):
            self.assertEqual(read_skill_records(dict(item, **extra)), [])

    def test_quoted_multiline_examples_and_search_are_not_reads(self):
        for command in ("echo 'example; cat /hud/SKILL.md; done'",
                        'echo "example\ncat /hud/SKILL.md\n"',
                        "rg name /hud/SKILL.md", "cat <<EOF\n/hud/SKILL.md\nEOF"):
            self.assertEqual(read_skill_records(self.item([{"type": "unknown", "command": command}])), [])

    def test_unreliable_descriptions_remain_empty(self):
        for description in ('[one, two]', '"unterminated', '>\n  one\n    nested',
                            '>\n  one\n\n  second paragraph',
                            'first\ndescription: second'):
            output = f"---\nname: hud\ndescription: {description}\n---\n"
            self.assertEqual(self.records(["/hud/SKILL.md"], output)[0]["description"], "")

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
