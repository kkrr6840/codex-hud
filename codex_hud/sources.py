"""Session-scoped sources. Never infer agents from process counts or global usage."""
from __future__ import annotations

import json
import ast
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import time
import tomllib
from datetime import datetime


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def atomic_json(path, value):
    path = Path(path)
    pending = path.with_suffix(f".{os.getpid()}.tmp")
    pending.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    pending.chmod(0o600)
    pending.replace(path)


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def descendants(pid):
    """PIDs are used solely to locate the CLI's open rollout, never as agents."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True,
                            text=True, timeout=2)
    children = {}
    for line in result.stdout.splitlines():
        try:
            child, parent = map(int, line.split())
        except ValueError:
            continue
        children.setdefault(parent, []).append(child)
    found, queue = {pid}, [pid]
    while queue and len(found) < 256:
        for child in children.get(queue.pop(), []):
            if child not in found:
                found.add(child)
                queue.append(child)
    return found


def bind_rollout(pid, home):
    """Bind by file ownership; same-cwd and most-recent-session matching are unsafe."""
    owned = descendants(pid)
    result = subprocess.run(["lsof", "-a", "-p", ",".join(map(str, owned)), "-Fn"],
                            capture_output=True, text=True, timeout=3)
    candidates = []
    session_dir = (Path(home) / "sessions").resolve()
    for line in result.stdout.splitlines():
        if not line.startswith("n") or not line.endswith(".jsonl"):
            continue
        path = Path(line[1:])
        if not path.is_relative_to(session_dir) or path in candidates:
            continue
        try:
            with path.open() as handle:
                meta = json.loads(handle.readline()).get("payload", {})
            # id is the thread id; session_id may be inherited by children.
            if meta.get("parent_thread_id") or isinstance(meta.get("source"), dict):
                continue
            if meta.get("id") and meta.get("source") in ("cli", "vscode"):
                candidates.append(path)
        except (OSError, ValueError):
            continue
    return candidates[0] if len(candidates) == 1 else None


class Diagnostic:
    """Per-invocation official diagnostics bind even an empty /new session.

    Session-loop creation also occurs for internal title generation and agents.
    Only persisted user threads are eligible; tool/turn span activity cannot
    switch the HUD's root. No prompts or credentials are extracted here.
    """
    START = re.compile(r"\bINFO\s+session_loop\{thread_id=([0-9a-f-]{36})\}:\s+codex_core::session:\s+new\s*$")
    PERSIST = re.compile(r'\bINFO\s+thread_spawn\{[^}]*\}:session_init:session_init\.thread_persistence\{[^}]*session_init\.ephemeral=(true|false)')

    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.current = None
        self.expect_user_loop = False

    def bind(self, home):
        try:
            with self.path.open("rb") as handle:
                if self.path.stat().st_size < self.offset:
                    self.offset, self.current, self.expect_user_loop = 0, None, False
                handle.seek(self.offset)
                while True:
                    line = handle.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    self.offset = handle.tell()
                    text = line.decode(errors="replace")
                    persistent = self.PERSIST.search(text)
                    if persistent:
                        self.expect_user_loop = persistent[1] == "false"
                        if self.expect_user_loop:
                            self.current = None
                    match = self.START.search(text)
                    if match and self.expect_user_loop:
                        self.current, self.expect_user_loop = match[1], False
            state = database(home, "state")
            if state and self.current:
                try:
                    row = state.execute("SELECT id,source,rollout_path FROM threads WHERE id=?", (self.current,)).fetchone()
                    if row and row["source"] in ("cli", "vscode"):
                        return row["id"], Path(row["rollout_path"])
                    if not row:
                        return self.current, None  # Empty /new is not persisted yet.
                finally:
                    state.close()
            elif state:
                state.close()
            if self.current:
                return self.current, None
            if self.expect_user_loop:
                return None, None  # Init underway; never reuse the previous rollout.
        except FileNotFoundError:
            pass
        return None


class Rollout:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.inode = None
        self.signature = None
        self.data = {}
        self.jobs = {}
        self.calls = {}
        self.tool_sessions = {}
        self.cells = {}
        self.activity = {}

    def refresh(self):
        try:
            stat = self.path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if signature == self.signature:
                return self.data
            rewritten = self.signature and stat.st_size == self.signature[2]
            if self.inode != stat.st_ino or stat.st_size < self.offset or rewritten:
                self.offset, self.data, self.inode = 0, {}, stat.st_ino
                self.jobs.clear()
                self.calls.clear()
                self.tool_sessions.clear()
                self.cells.clear()
                self.activity.clear()
            with self.path.open("rb") as handle:
                if self.offset == 0:
                    first = handle.readline()
                    if not first.endswith(b"\n"):
                        return self.data
                    try:
                        self.consume(json.loads(first))
                    except ValueError:
                        pass
                    # Resume large sessions without reparsing old conversations.
                    start = max(handle.tell(), stat.st_size - 2 * 1024 * 1024)
                    if start > handle.tell():
                        self.data["history_partial"] = True
                        handle.seek(start)
                        handle.readline()
                    self.offset = handle.tell()
                handle.seek(self.offset)
                while True:
                    line = handle.readline()
                    if not line or not line.endswith(b"\n"):
                        break  # A partially written JSON line is retried next refresh.
                    self.offset = handle.tell()
                    try:
                        self.consume(json.loads(line))
                    except (ValueError, TypeError):
                        continue
            self.signature = signature
            self.data.pop("error", None)
        except OSError as error:
            self.signature = None
            self.offset = 0
            self.data = {k: self.data[k] for k in ("id", "cwd", "model") if k in self.data}
            self.data.update(status="unknown", error=str(error))
        return self.data

    def consume(self, row):
        if not isinstance(row, dict):
            return
        kind, payload = row.get("type"), row.get("payload") or {}
        if not isinstance(payload, dict):
            return
        data = self.data
        if kind == "session_meta":
            data.update(id=payload.get("id"), cwd=payload.get("cwd"),
                        parent=payload.get("parent_thread_id"),
                        name=payload.get("agent_path") or payload.get("agent_nickname"),
                        created_at=timestamp(payload.get("timestamp") or row.get("timestamp")),
                        cli_version=payload.get("cli_version"), provider=payload.get("model_provider"),
                        added_dirs=payload.get("runtime_workspace_roots") or [])
        elif kind == "turn_context":
            for key in ("model", "cwd", "effort"):
                if payload.get(key):
                    data[key] = payload[key]
        elif kind == "response_item" and payload.get("type") == "agent_message":
            content = payload.get("content") or []
            text = "".join(x.get("text", "") for x in content if isinstance(x, dict))
            if "Message Type: NEW_TASK" in text and "Payload:\n" in text:
                task = text.split("Payload:\n", 1)[1].strip()
                if task:
                    data["task"] = task
                elif any(x.get("type") == "encrypted_content" for x in content if isinstance(x, dict)):
                    data["task_locked"] = True
        elif kind == "response_item" and payload.get("type") in ("custom_tool_call", "function_call"):
            self.tool_call(payload, row.get("timestamp"))
        elif kind == "response_item" and payload.get("type") in ("custom_tool_call_output", "function_call_output"):
            self.tool_output(payload, row.get("timestamp"))
        elif kind == "token_usage_record" and payload.get("thread_id") == data.get("id"):
            if isinstance(payload.get("thread_token_usage"), dict):
                data["usage"] = payload["thread_token_usage"]
                data["usage_scope"] = "thread"
            if isinstance(payload.get("usage"), dict) and isinstance(payload["usage"].get("input_tokens"), int):
                data["context_used"] = payload["usage"]["input_tokens"]
        elif kind == "event_msg":
            event = payload.get("type")
            if event == "token_count" and "rate_limits" in payload:
                # Quota-only events may have info=null. Replace the whole
                # snapshot so missing windows never reuse a previous quota.
                limits = payload["rate_limits"]
                data["rate_limits"] = limits if isinstance(limits, dict) else None
            if event == "token_count" and isinstance(payload.get("info"), dict):
                info = payload["info"]
                if data.get("usage_scope") != "thread" and isinstance(info.get("total_token_usage"), dict):
                    data["usage"] = info["total_token_usage"]
                last = info.get("last_token_usage")
                if data.get("usage_scope") != "thread" and isinstance(last, dict) and isinstance(last.get("input_tokens"), int):
                    data["context_used"] = last["input_tokens"]
                if info.get("model_context_window"):
                    data["context_window"] = info["model_context_window"]
            elif event == "task_started":
                data.update(status="running", started=timestamp(payload.get("started_at") or row.get("timestamp")),
                            ended=None, duration=None)
                if payload.get("model_context_window"):
                    data["context_window"] = payload["model_context_window"]
            elif event in ("task_complete", "turn_aborted"):
                data.update(status="completed" if event == "task_complete" else "idle",
                            ended=timestamp(payload.get("completed_at") or row.get("timestamp")))
                if isinstance(payload.get("duration_ms"), (int, float)):
                    data["duration"] = payload["duration_ms"] / 1000
                data["last_response_at"] = data.get("ended")
                for tool in self.activity.values():
                    if tool["status"] == "running":
                        tool["status"] = "unknown"
            elif event == "context_compacted":
                data.pop("context_used", None)
        elif kind == "compacted":
            data.pop("context_used", None)
            data["compactions"] = data.get("compactions", 0) + 1

    def tool_call(self, payload, when):
        """Decode literals only. Never evaluate agent-generated JavaScript."""
        source = payload.get("input") or payload.get("arguments") or ""
        name = payload.get("name", "")
        call = payload.get("call_id")
        if call:
            self.activity[call] = {"id": call, "name": name, "status": "running", "started": timestamp(when)}
            # Bound the activity view, without truncating separately collected
            # token totals or guessing tool runs from generated source code.
            if len(self.activity) > 200:
                self.activity.pop(next(iter(self.activity)))
            self.data["tools"] = list(self.activity.values())
        if name.removeprefix("functions.") == "update_plan":
            plan = read_literal_object(source).get("plan")
            if isinstance(plan, list):
                self.data["todos"] = [{"step": p["step"], "status": p["status"]} for p in plan if isinstance(p, dict) and isinstance(p.get("step"), str) and p.get("status") in ("pending", "in_progress", "completed")]
        requests = []
        if name == "exec_command":
            args = read_literal_object(source)
            if isinstance(args.get("cmd"), str):
                requests = [("exec_command", args["cmd"], None)]
        elif name in ("exec", "functions.exec"):
            requests = literal_tool_requests(source)
        elif name == "write_stdin":
            args = read_literal_object(source)
            if isinstance(args.get("session_id"), int):
                requests = [("write_stdin", args["session_id"], None)]
        elif name == "wait":
            args = read_literal_object(source)
            cell = str(args.get("cell_id", ""))
            if cell in self.cells:
                self.calls[call] = self.cells[cell]
        bindings = []
        for position, (kind, value, index) in enumerate(requests):
            if kind == "exec_command" and isinstance(value, str):
                identity = call if len(requests) == 1 else f"{call}:{position}"
                item = {"id": identity, "command": value, "status": "unknown"}
                job = offload_job({"thread_id": self.data.get("id"), "item_json": json.dumps(item),
                                  "created_at_ms": (timestamp(when) or 0) * 1000}, "gpt-6-astra")
                if job:
                    self.jobs[identity] = job
                    bindings.append((identity, index))
            elif kind == "write_stdin" and value in self.tool_sessions:
                bindings.append((self.tool_sessions[value], index))
        if bindings:
            self.calls[call] = bindings

    def tool_output(self, payload, when):
        tool = self.activity.get(payload.get("call_id"))
        if tool:
            tool.update(status="completed", ended=timestamp(when))
            output = payload.get("output")
            texts = [output] if isinstance(output, str) else [x.get("text", "") for x in output or [] if isinstance(x, dict)]
            for text in texts:
                for obj in json_objects(text):
                    if obj.get("isError") is True or (type(obj.get("exit_code")) is int and obj["exit_code"] != 0):
                        tool["status"] = "failed"
            if tool.get("started") is not None and tool.get("ended") is not None:
                tool["duration"] = max(0, tool["ended"] - tool["started"])
        bindings = self.calls.get(payload.get("call_id"))
        if not bindings:
            return
        output = payload.get("output") or ""
        texts = [output] if isinstance(output, str) else [x.get("text", "") for x in output if isinstance(x, dict)]
        for text in texts:
            cell = re.search(r"Script running with cell ID (\S+)", text)
            if cell:
                self.cells[cell[1]] = bindings
            for index, value in command_outputs(text):
                for identity, expected in bindings:
                    if (expected is not None and index != expected) or identity not in self.jobs:
                        continue
                    job = self.jobs[identity]
                    if type(value.get("session_id")) is int and "output" in value:
                        job["process_id"] = str(value["session_id"])
                        self.tool_sessions[value["session_id"]] = identity
                        if job["status"] != "running":
                            job["started"] = (timestamp(when) or time.time()) - value.get("wall_time_seconds", 0)
                        job["status"] = "running"
                    if type(value.get("exit_code")) is int and "output" in value:
                        response = response_json(value.get("output"))
                        job.update(status="completed" if response and response.get("ok") else "failed" if value.get("exit_code") or (response and not response.get("ok")) else "unknown",
                                   ended=timestamp(when), usage=(response or {}).get("usage"),
                                   duration=(response or {}).get("duration_seconds"),
                                   model=(response or {}).get("model") or job["model"],
                                   worker_id=(response or {}).get("thread_id"), error=(response or {}).get("error"),
                                   output=(response or {}).get("answer"))


def literal_tool_requests(source):
    """Read literal tool arguments and indexed Promise batches; never run JS."""
    literal = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`''')
    strings = list(literal.finditer(source))
    masked = literal.sub(lambda match: " " * len(match[0]), source)
    calls = list(re.finditer(r"\btools\.(\w+)\s*\(", masked))
    batches = list(re.finditer(r"Promise\.(?:all|allSettled)\s*\(\s*\[", masked))
    batch_start, batch_end = -1, -1
    if len(batches) == 1:
        batch_start = batches[0].end()
        depth = 1
        for at in range(batch_start, len(masked)):
            depth += (masked[at] == "[") - (masked[at] == "]")
            if depth == 0:
                batch_end = at
                break
    # Result indices refer to array entries, not the number of tool calls.
    # Count non-tool entries too, and decline entries with nested/multiple calls.
    entries = []
    if batch_end >= 0:
        start, nesting = batch_start, 0
        for at in range(batch_start, batch_end):
            char = masked[at]
            nesting += (char in "([{") - (char in ")]}")
            if char == "," and nesting == 0:
                entries.append((start, at))
                start = at + 1
        entries.append((start, batch_end))
        if any(masked[left:right].lstrip().startswith("...") for left, right in entries):
            entries = []
    requests = []
    for match in calls:
        index = None if len(calls) == 1 and not batches else -1
        for position, (left, right) in enumerate(entries):
            if left <= match.start() < right and sum(left <= call.start() < right for call in calls) == 1:
                prefix = masked[left:match.start()].strip()
                if prefix in ("", "await"):
                    index = position
                break
        start = match.end()
        while start < len(masked) and masked[start].isspace():
            start += 1
        if start >= len(masked) or masked[start] != "{":
            requests.append((match[1], None, index))
            continue
        depth, end, depths = 0, start, {}
        for end in range(start, len(masked)):
            depths[end] = depth
            depth += (masked[end] == "{") - (masked[end] == "}")
            if depth == 0:
                break
        body = source[start:end+1]
        pattern = r'''(?:\bcmd\b|"cmd"|'cmd')\s*:\s*("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')(?=\s*[,}])''' if match[1] == "exec_command" else r'''(?:\bsession_id\b|"session_id"|'session_id')\s*:\s*(\d+)(?=\s*[,}])'''
        value = None
        for field in re.finditer(pattern, body):
            at = start + field.start()
            if depths.get(at) != 1:
                continue
            if any(token.start() < at < token.end() for token in strings):
                continue
            try:
                value = ast.literal_eval(field[1])
            except (SyntaxError, ValueError):
                pass
            break
        requests.append((match[1], value, index))
    return requests


def command_outputs(text):
    """Skip nested foreign results after decoding an indexed output envelope."""
    if not isinstance(text, str):
        return
    decoder, at = json.JSONDecoder(), 0
    while (at := text.find("{", at)) >= 0:
        try:
            obj, length = decoder.raw_decode(text[at:])
        except ValueError:
            at += 1
            continue
        at += length
        if not isinstance(obj, dict):
            continue
        index = obj.get("i") if type(obj.get("i")) is int else None
        value = obj.get("value") if index is not None else obj
        if isinstance(value, dict) and "output" in value:
            yield index, value


def read_literal_object(source):
    try:
        obj = json.loads(source)
        return obj if isinstance(obj, dict) else {}
    except (TypeError, ValueError):
        return {}


def json_objects(text):
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", str(text)):
        try:
            obj, _ = decoder.raw_decode(text[match.start():])
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def database(home, prefix):
    files = list(Path(home).glob(f"{prefix}_*.sqlite"))
    if not files:
        return None
    path = max(files, key=lambda p: int(p.stem.rsplit("_", 1)[1]))
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def turn_state(turn, root=False, launched_at=None):
    if not turn:
        return "unknown"
    raw = turn.get("status")
    if raw in ("inProgress", "in_progress", "running"):
        # A killed CLI may leave an inProgress row. Do not resurrect old work.
        if launched_at and (turn.get("started_at") or 0) < launched_at - 1:
            return "unknown"
        return "running"
    if raw == "completed":
        return "idle" if root else "completed"
    if raw in ("interrupted", "cancelled"):
        return "idle"
    if raw == "failed":
        return "failed"
    return "unknown"


def command_text(item):
    text = item.get("command", "")
    # commandActions carries the inner command, avoiding shell -lc quoting.
    for action in item.get("commandActions") or []:
        if isinstance(action, dict) and isinstance(action.get("command"), str):
            return action["command"]
    try:
        # Ordinary commands need no shell unwrapping. Stop after the second
        # token instead of parsing long scripts, heredocs and task text.
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.get_token()
        if lexer.get_token() in ("-lc", "-c"):
            body = lexer.get_token()
            if body is not None and lexer.get_token() is None:
                return body
    except ValueError:
        pass
    return text


OFFLOAD = re.compile(r"(?:^|[\n;&|])\s*(?:python(?:3(?:\.\d+)?)?\s+)?(?:['\"])?[^\s'\"]*codex-x-codex[^\s'\"]*/scripts/offload\.py(?:['\"])?\s+run(?:\s|$)")


def response_json(output):
    """Accept a plugin response, not unrelated JSON or a cumulative global record."""
    for obj in json_objects(output or ""):
        if isinstance(obj, dict) and isinstance(obj.get("ok"), bool) and obj.get("outcome") in ("completed", "failed") and "usage" in obj:
            return obj
    return None


def offload_job(row, configured_model, launched_at=None):
    item = json.loads(row["item_json"])
    command = command_text(item)
    args = None
    for match in OFFLOAD.finditer(command):
        # Compound commands may prepare a task before the actual invocation.
        # Help is CLI inspection, never a model job or missing usage record.
        invocation = match.group().lstrip("\n;&| ") + command[match.end():].split("\n", 1)[0]
        try:
            candidate = shlex.split(invocation)
        except ValueError:
            continue
        if "--help" not in candidate and "-h" not in candidate:
            args = candidate
            break
    if args is None:
        return None
    def option(name, fallback):
        try:
            return args[args.index(name) + 1]
        except (ValueError, IndexError):
            return fallback
    started = (row.get("started_at_ms") or row.get("created_at_ms") or 0) / 1000
    ended = (row.get("completed_at_ms") or 0) / 1000 or None
    response = response_json(item.get("aggregatedOutput"))
    raw = item.get("status")
    status = "running" if raw in ("inProgress", "in_progress") else "unknown"
    if status == "running" and launched_at and started < launched_at - 1:
        status = "unknown"
    if response:
        status = "completed" if response.get("ok") else "failed"
    elif raw == "failed" or item.get("exitCode") not in (None, 0):
        status = "failed"
    return {"id": item.get("id"), "process_id": item.get("processId"), "owner": row["thread_id"], "status": status,
            "task": option("--task", "代码修改子任务" if (response or {}).get("mode", option("--mode", "read-only")) == "workspace-write" else "只读子任务"), "started": started, "ended": ended,
            "model": (response or {}).get("model") or option("--model", configured_model),
            "duration": (response or {}).get("duration_seconds"),
            "usage": (response or {}).get("usage"), "worker_id": (response or {}).get("thread_id"),
            "error": (response or {}).get("error"), "output": (response or {}).get("answer")}


class Collector:
    def __init__(self, home=None, root_id=None, rollout=None, runner=None, launched_at=None, diagnostic=None):
        self.home = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()
        self.root_id = root_id
        self.root_path = Path(rollout) if rollout else None
        self.runner = runner
        self.launched_at = launched_at
        self.readers = {}
        self.git_cache = (0, None, None)
        self.last_bind = 0
        self.watermarks = {}
        self.jobs = {}
        self.tasks = {}
        self.diagnostic = Diagnostic(diagnostic) if diagnostic else None
        self.extra_cache = {}
        self.skill_turn = None
        self.skill_reads = []
        self.skill_records = []

    def reader(self, path):
        path = str(path)
        if path not in self.readers:
            self.readers[path] = Rollout(path)
        return self.readers[path].refresh()

    def collect(self, options=None):
        now = time.time()
        errors = []
        if (self.runner or self.diagnostic) and now - self.last_bind >= 2:
            self.last_bind = now
            try:
                identified = self.diagnostic.bind(self.home) if self.diagnostic else None
                identity, path = identified if identified else (None, bind_rollout(self.runner, self.home) if self.runner and os.name != "nt" else None)
                if path != self.root_path or (identity and identity != self.root_id):
                    self.root_path, self.root_id = path, identity
                    self.readers.clear()
                    self.watermarks.clear()
                    self.tasks.clear()
                    self.jobs.clear()
                    self.skill_turn, self.skill_reads, self.skill_records = None, [], []
            except (OSError, sqlite3.Error, subprocess.SubprocessError) as error:
                errors.append(f"会话绑定: {error}")
                self.root_path, self.root_id = None, None
                self.readers.clear()
                self.watermarks.clear()
                self.tasks.clear()
                self.jobs.clear()
                self.skill_turn, self.skill_reads, self.skill_records = None, [], []
        root = dict(self.reader(self.root_path)) if self.root_path and (self.root_path.exists() or str(self.root_path) in self.readers) else {}
        if root.get("id"):
            self.root_id = root["id"]
        root["id"] = self.root_id
        agents, rows, tasks, turns = [], {}, {}, {}
        agents_known = False
        config_path = Path(os.environ.get("CODEX_X_CODEX_HOME", Path.home() / ".config/codex-x-codex")) / "config.json"
        config = read_json(config_path) if config_path.exists() else {}
        enabled = config.get("enabled", False) if isinstance(config, dict) else None
        offload = {"enabled": enabled, "model": config.get("model", "gpt-6-astra") if isinstance(config, dict) else "unknown", "jobs": []}
        if self.root_id:
            state = history = None
            try:
                state = database(self.home, "state")
                history = database(self.home, "thread_history")
                if state:
                    row = state.execute("SELECT * FROM threads WHERE id=?", (self.root_id,)).fetchone()
                    if row:
                        rows[self.root_id] = dict(row)
                        detail = dict(row)
                        root.update(session_name=detail.get("name") or detail.get("title"),
                                    provider=detail.get("model_provider") or root.get("provider"),
                                    cli_version=detail.get("cli_version") or root.get("cli_version"))
                        root["created_at"] = root.get("created_at") or detail.get("created_at")
                    pending, seen = [self.root_id], {self.root_id}
                    while pending:
                        for edge in state.execute("SELECT * FROM thread_spawn_edges WHERE parent_thread_id=?", (pending.pop(),)):
                            child = edge["child_thread_id"]
                            if child in seen:
                                continue
                            seen.add(child)
                            pending.append(child)
                            row = state.execute("SELECT * FROM threads WHERE id=?", (child,)).fetchone()
                            if row:
                                rows[child] = dict(row, edge_status=edge["status"], parent=edge["parent_thread_id"])
                else:
                    errors.append("agent 数据库不可用")
                if not self.root_path and rows.get(self.root_id, {}).get("rollout_path"):
                    self.root_path = Path(rows[self.root_id]["rollout_path"])
                    root.update(self.reader(self.root_path))
                for key in ("model", "cwd"):
                    if not root.get(key):
                        root[key] = rows.get(self.root_id, {}).get(key)
                # The thread record tracks the current selection immediately;
                # turn_context describes the last request and can lag a /model
                # change indefinitely. Never use the global default here: other
                # terminals can have independent model/effort selections.
                selected = rows.get(self.root_id, {})
                if isinstance(selected.get("model"), str) and selected["model"]:
                    root["model"] = selected["model"]
                    if "reasoning_effort" in selected:
                        root.pop("effort", None)
                        if selected["reasoning_effort"]:
                            root["effort"] = selected["reasoning_effort"]
                owners = list(rows) or [self.root_id]
                if history:
                    for owner in owners:
                        turn = history.execute("SELECT * FROM thread_turns WHERE thread_id=? ORDER BY rollout_ordinal DESC LIMIT 1", (owner,)).fetchone()
                        if turn:
                            turns[owner] = dict(turn)
                        if owner == self.root_id:
                            current_turn = dict(turn) if turn else {}
                            if current_turn.get("turn_id") != self.skill_turn:
                                self.skill_turn = current_turn.get("turn_id")
                                self.skill_reads = []
                                self.skill_records = []
                        watermark = self.watermarks.get(owner, -1)
                        # Separate the two indexed ranges. An OR here can make
                        # SQLite scan every old item in this thread each tick.
                        query = """SELECT * FROM thread_items
                            WHERE thread_id=? AND item_type IN ('collabAgentToolCall','commandExecution') AND rollout_ordinal>?
                            UNION
                            SELECT * FROM thread_items
                            WHERE thread_id=? AND item_type IN ('collabAgentToolCall','commandExecution') AND updated_at_ordinal>?
                            ORDER BY rollout_ordinal"""
                        parameters = (owner, watermark, owner, watermark)
                        if watermark < 0:
                            # The initial scan needs all history only once; do
                            # not load and deduplicate the same old rows twice.
                            query = "SELECT * FROM thread_items WHERE thread_id=? AND item_type IN ('collabAgentToolCall','commandExecution') AND (rollout_ordinal>? OR updated_at_ordinal>?) ORDER BY rollout_ordinal"
                            parameters = (owner, watermark, watermark)
                        for row in history.execute(query, parameters):
                            self.watermarks[owner] = max(self.watermarks.get(owner, -1), row["rollout_ordinal"], row["updated_at_ordinal"])
                            if row["item_type"] == "collabAgentToolCall":
                                item = json.loads(row["item_json"])
                                if item.get("prompt"):
                                    for receiver in item.get("receiverThreadIds") or []:
                                        self.tasks[receiver] = item["prompt"]
                            else:
                                if owner == self.root_id and "SKILL.md" in row["item_json"]:
                                    record = dict(row)
                                    same_turn = record.get("turn_id") == self.skill_turn if record.get("turn_id") else bool(turn and record.get("created_at_ms", 0) >= (turn["started_at"] or 0)*1000)
                                    if same_turn:
                                        from .skills import read_skill_records
                                        for skill in read_skill_records(json.loads(row["item_json"])):
                                            name = skill["name"]
                                            if name not in self.skill_reads:
                                                self.skill_reads.append(name)
                                            detail = {**skill, "owner": owner, "turn_id": self.skill_turn,
                                                      "read_at": (record.get("completed_at_ms") or record.get("created_at_ms") or 0) / 1000}
                                            previous = next((s for s in self.skill_records if s["path"] == skill["path"]), None)
                                            if previous is None:
                                                self.skill_records.append(detail)
                                            else:
                                                previous.update({k: v for k, v in detail.items() if k != "description" or v})
                                job = offload_job(dict(row), offload["model"], self.launched_at)
                                if job:
                                    self.jobs[(owner, job["id"])] = job
                if self.root_id in turns:
                    root["skills"] = list(self.skill_reads)
                    root["skill_reads"] = [dict(skill) for skill in self.skill_records]
                    root["skill_turn_id"] = self.skill_turn
                    root["status"] = turn_state(turns[self.root_id], True, self.launched_at)
                    root["started"] = turns[self.root_id].get("started_at")
                elif root.get("status") == "completed":
                    root["status"] = "idle"
                elif self.diagnostic and self.root_id and not turns.get(self.root_id):
                    root["status"] = "idle"
                for owner, row in rows.items():
                    if owner == self.root_id:
                        continue
                    detail = self.reader(row["rollout_path"]) if row.get("rollout_path") else {}
                    turn = turns.get(owner)
                    status = turn_state(turn, launched_at=self.launched_at) if turn else detail.get("status", "unknown")
                    if row.get("edge_status") == "closed" and status == "running":
                        status = "unknown"
                    visible = status not in ("completed", "failed") or (options or {}).get("agents_show_" + ("completed" if status == "completed" else "failed"), False)
                    if detail.get("error") and visible:
                        errors.append(f"{row.get('agent_nickname') or owner[:8]} 日志不可读")
                    agents.append({"id": owner, "parent": row.get("parent"),
                                   "name": row.get("agent_nickname") or row.get("agent_path") or owner[:8],
                                   "nickname": row.get("agent_nickname"), "model": row.get("model") or detail.get("model"),
                                   "task": self.tasks.get(owner) or detail.get("task") or row.get("first_user_message") or row.get("title") or ((row.get("agent_path") or "").removeprefix("/root/") + " (任务名；详细指令加密)" if detail.get("task_locked") else row.get("agent_path") or "任务未知"),
                                   "status": status, "turn_id": (turn or {}).get("turn_id"), "started": (turn or {}).get("started_at") or detail.get("started"),
                                   "ended": (turn or {}).get("completed_at") or detail.get("ended"),
                                   "duration": (turn["duration_ms"] / 1000 if turn and turn.get("duration_ms") is not None else detail.get("duration"))})
                    agents[-1]["usage"] = detail.get("usage")
                    if detail.get("error"):
                        agents[-1]["log_error"] = detail["error"]
                agents_known = state is not None
            except (sqlite3.Error, OSError, ValueError) as error:
                errors.append(f"数据源不可用: {error}")
            finally:
                if state:
                    state.close()
                if history:
                    history.close()
        offload["jobs"] = [v for (owner, _), v in self.jobs.items() if owner in rows or owner == self.root_id]
        process_ids = {j.get("process_id") for j in offload["jobs"] if j.get("process_id")}
        worker_ids = {j.get("worker_id") for j in offload["jobs"] if j.get("worker_id")}
        for reader in self.readers.values():
            if reader.data.get("id") not in rows and reader.data.get("id") != self.root_id:
                continue
            for job in reader.jobs.values():
                if job.get("process_id") in process_ids:
                    continue
                if job.get("worker_id") and job["worker_id"] in worker_ids:
                    continue
                if self.launched_at and job.get("status") == "running" and job.get("started", 0) < self.launched_at - 1:
                    job = dict(job, status="unknown")
                if reader.data.get("error") and job.get("status") == "running":
                    job = dict(job, status="unknown")
                offload["jobs"].append(job)
        agents.sort(key=lambda a: (a["status"] != "running", -(a.get("started") or 0)))
        offload["jobs"].sort(key=lambda a: (a["status"] != "running", -(a.get("started") or 0)))
        if root.get("error"):
            errors.append(f"会话日志不可读: {root['error']}")
        return {"time": now, "root": root, "agents": agents, "agents_known": agents_known, "offload": offload,
                "git": self.git(root.get("cwd")), "errors": errors,
                "environment": self.cached_extra("environment", self.environment) if (options or {}).get("show", {}).get("environment") else None,
                "memory": self.cached_extra("memory", system_memory) if (options or {}).get("show", {}).get("memory") else None}

    def cached_extra(self, name, reader):
        last, value = self.extra_cache.get(name, (0, None))
        if time.time() - last > 10:
            value = reader()
            self.extra_cache[name] = (time.time(), value)
        return value

    def environment(self):
        try:
            config = tomllib.loads((self.home / "config.toml").read_text(encoding="utf-8"))
            return {"mcp": sum(v.get("enabled", True) is not False for v in config.get("mcp_servers", {}).values() if isinstance(v, dict)),
                    "plugins": sum(v.get("enabled") is True for v in config.get("plugins", {}).values() if isinstance(v, dict)),
                    "instructions": int((self.home / "AGENTS.md").is_file())}
        except (OSError, ValueError, TypeError):
            return None

    def git(self, cwd):
        now = time.time()
        if self.git_cache[1] == cwd and now - self.git_cache[0] < 5:
            return self.git_cache[2]
        data = {"branch": None, "changes": None}
        if cwd:
            try:
                result = subprocess.run(["git", "-C", cwd, "status", "--porcelain=v1", "--branch"],
                                        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2,
                                        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
                if result.returncode == 0:
                    lines = result.stdout.splitlines()
                    branch = lines[0][3:].split("...")[0] if lines else "unknown"
                    for prefix in ("No commits yet on ", "Initial commit on "):
                        branch = branch.removeprefix(prefix)
                    changes = lines[1:]
                    data = {"branch": branch, "changes": len(changes),
                            "ahead": int((re.search(r"ahead (\d+)", lines[0]) or [None, 0])[1]),
                            "behind": int((re.search(r"behind (\d+)", lines[0]) or [None, 0])[1]),
                            "added": sum("A" in line[:2] for line in changes),
                            "modified": sum("M" in line[:2] for line in changes),
                            "deleted": sum("D" in line[:2] for line in changes),
                            "untracked": sum(line.startswith("??") for line in changes)}
                    marker = Path(cwd) / ".git"
                    if marker.is_file():
                        data["worktree"] = Path(cwd).name
                elif "not a git repository" not in result.stderr:
                    data["error"] = "Git 状态读取失败"
            except (OSError, subprocess.SubprocessError):
                data["error"] = "Git 状态读取失败"
        self.git_cache = now, cwd, data
        return data


def system_memory():
    """OS-reported physical RAM, separate from model context."""
    try:
        if Path("/proc/meminfo").exists():
            values = dict(re.findall(r"^(\w+):\s+(\d+)", Path("/proc/meminfo").read_text(), re.M))
            total, available = int(values["MemTotal"]) * 1024, int(values["MemAvailable"]) * 1024
        else:
            total = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=2))
            output = subprocess.check_output(["vm_stat"], text=True, timeout=2)
            page = int(re.search(r"page size of (\d+) bytes", output)[1])
            values = dict(re.findall(r"^(Pages [^:]+):\s+(\d+)", output, re.M))
            available = sum(int(values.get(k, 0)) for k in ("Pages free", "Pages inactive", "Pages speculative")) * page
        return {"total": total, "used": max(0, total - available)}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None
