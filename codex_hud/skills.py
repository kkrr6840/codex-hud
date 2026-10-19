"""Metadata of skills actually read by successful recorded commands."""
from __future__ import annotations

import json
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


def _scalar(value):
    """Parse a conservative subset of single-line YAML strings."""
    value = value.strip()
    if value.startswith('"'):
        try:
            result = json.loads(value)
            return result if isinstance(result, str) else ""
        except ValueError:
            return ""
    if value.startswith("'"):
        return value[1:-1].replace("''", "'") if re.fullmatch(r"'(?:[^']|'')*'", value) else ""
    if not value or value[0] in "|>!&*[{#" or ": " in value:
        return ""
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def _frontmatter(output):
    blocks = []
    for block in re.findall(r"(?ms)^---[ \t]*\r?\n(.*?)^---[ \t]*\r?$", output):
        names = re.findall(r"(?m)^name:[ \t]*(.*)$", block)
        if len(names) != 1 or not (name := _scalar(names[0])):
            continue
        descriptions = list(re.finditer(r"(?m)^description:[ \t]*(.*)$", block))
        description = ""
        if len(descriptions) == 1:
            match = descriptions[0]
            value = match[1].strip()
            if value in (">", ">-"):
                lines = []
                for line in block[match.end():].splitlines()[1:]:
                    if line.strip() and not line.startswith(" "):
                        break
                    lines.append(line)
                if lines and all(s.strip() for s in lines) and len({len(s) - len(s.lstrip(' ')) for s in lines}) == 1:
                    description = " ".join(s.strip() for s in lines)
            else:
                description = _scalar(value)
        blocks.append((name, description))
    return blocks


def _command_paths(command):
    # Tokenize before splitting commands: quoted echo examples stay inert.
    if not isinstance(command, str) or any(s in command for s in ("<<", "$(", "`")):
        return []
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";\n|&<>")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    # Backslashes before SKILL.md identify Windows-style separators. Otherwise
    # retain POSIX escaping so `/my\ skills/hud/SKILL.md` stays one full path.
    windows_paths = re.search(r"\\SKILL\.md(?:['\"]|\s|$)", command) or command.lstrip().startswith("Get-Content ")
    lexer.escape = "" if windows_paths else "\\"
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    segments, segment = [], []
    for token in tokens + [";"]:
        if token and all(c in ";\n" for c in token):
            segments.append(segment)
            segment = []
        else:
            segment.append(token)
    paths = []
    for segment in segments:
        if segment[:1] == ["then"]:
            segment = segment[1:]
        if not segment or segment[0] not in ("cat", "sed", "head", "tail", "Get-Content"):
            continue
        if any(re.fullmatch(r"[|&<>]+", t) for t in segment):
            continue
        paths.extend(p for p in segment[1:] if not p.startswith("-") and skill_name(p))
    return paths


def read_skill_records(item) -> list[dict]:
    """Extract recorded metadata only; never open a logged path.

    A description needs a unique matching block and candidate path. Output
    order cannot disambiguate same-named skills read from different files.
    """
    if item.get("type", "commandExecution") != "commandExecution":
        return []
    if item.get("status") != "completed" or item.get("exitCode") != 0:
        return []
    output = item.get("aggregatedOutput") or ""
    if not isinstance(output, str) or not output.strip():
        return []
    blocks = _frontmatter(output)
    names = {name for name, _ in blocks}
    paths = []
    for action in item.get("commandActions") or []:
        if not isinstance(action, dict):
            continue
        if action.get("type") == "read" and isinstance(action.get("path"), str):
            paths.append(action["path"])
        elif action.get("type") == "unknown":
            for path in _command_paths(action.get("command")):
                name = skill_name(path)
                if name in names or name.rsplit(":", 1)[-1] in names:
                    paths.append(path)
    records = []
    for path in dict.fromkeys(paths):
        name = skill_name(path)
        if name:
            basename = PurePosixPath(path.replace("\\", "/")).parts[-2]
            source = name.rsplit(":", 1)[0] if name != basename else "local"
            records.append(dict(name=name, path=path, description="", source=source))
    for record in records:
        aliases = {record["name"], record["name"].rsplit(":", 1)[-1]}
        matches = [(name, desc) for name, desc in blocks if name in aliases]
        if len(matches) == 1:
            name, description = matches[0]
            candidates = [r for r in records if name in {r["name"], r["name"].rsplit(":", 1)[-1]}]
            if len(candidates) == 1:
                record["description"] = description
    return records


def read_skills(item):
    """Backward-compatible unique names, in recorded read order."""
    return list(dict.fromkeys(record["name"] for record in read_skill_records(item)))
