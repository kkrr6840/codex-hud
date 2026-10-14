"""Names of skills actually read by successful current-thread commands."""
from __future__ import annotations

from pathlib import PurePosixPath
import re
import shlex


def skill_name(path):
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if len(parts) < 2 or parts[-1] != "SKILL.md":
        return None
    name = parts[-2]
    if "cache" in parts:
        index = parts.index("cache")
        if index >= 1 and parts[index-1] == "plugins" and len(parts) > index+3:
            name = parts[index+2] + ":" + name
    return name


def read_skills(item):
    if item.get("status") != "completed" or item.get("exitCode") != 0:
        return []
    output = item.get("aggregatedOutput") or ""
    if not isinstance(output, str) or not output.strip():
        return []
    names = set()
    for block in re.findall(r"(?ms)^---\r?\n(.*?)^---\s*$", output):
        match = re.search(r"(?m)^name:\s*(.+?)\s*$", block)
        if match:
            names.add(match[1].strip("\"'"))
    paths = []
    for action in item.get("commandActions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("type") == "read" and isinstance(action.get("path"), str):
            paths.append(action["path"])
            continue
        if action.get("type") != "unknown":
            continue
        command = action.get("command") or ""
        # Never interpret a script or heredoc's quoted examples as execution.
        if not isinstance(command, str) or "<<" in command:
            continue
        for segment in re.split(r"[;\n]", command):
            match = re.match(r"\s*(?:then\s+)?(?:cat|sed|head|tail|Get-Content)\s+(.+)$", segment)
            if not match:
                continue
            try:
                tokens = shlex.split(match[1])
            except ValueError:
                continue
            for path in tokens:
                name = skill_name(path)
                if name and name.split(":")[-1] in names:
                    paths.append(path)
    return list(dict.fromkeys(name for path in paths if (name := skill_name(path))))
