#!/usr/bin/env python3
"""Naive tool set for the harness — identical semantics to the paid
baseline's plain_agent (same tools, same caps, same errors). NO Razor
tools, no budgets beyond naive defaults, no masking, no offloading.

Tools: read_file (max 400 lines), run_command (300s, output truncated),
edit_file (exactly one occurrence, never a test file), finish.
"""
import json
import os
import re
import subprocess

READ_MAX_LINES = 400
OUT_MAX_CHARS = 15000

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "Read lines of a file in the repo (1-based, inclusive). Max 400 lines. Defaults: start_line 1, end_line start+399.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a shell command in the repo directory (e.g. pytest). Timeout 300s. Output truncated.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace exactly one occurrence of old_text with new_text in a source file. Test files cannot be edited.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "End the task. Call only after running the relevant tests.",
        "parameters": {"type": "object", "properties": {
            "summary": {"type": "string"}},
            "required": ["summary"]}}},
]

SYSTEM = (
    "You are a software engineer fixing a bug in a Python repository. "
    "Use the tools to read code, run tests, and edit source files. "
    "Fix the failing test(s) named in the task without breaking other tests. "
    "Do NOT edit any test file. Do NOT use the network. Do NOT guess: read "
    "the failing test output and the relevant source first. Run the relevant "
    "tests before finishing. When done, call finish with a short summary. "
    "Keep edits minimal and focused on the failing behaviour."
)

# Gemini text-JSON tool protocol (same reason as plain_agent: native
# function-call history is rejected by Gemini on replay).
GEMINI_TOOL_PROTOCOL = (
    "\n\nYou do NOT have native tool calls. To use a tool, output exactly "
    "one fenced block and nothing else outside it:\n"
    "```tool\n{\"tool\": \"read_file\", \"args\": {\"path\": \"...\", "
    "\"start_line\": 1, \"end_line\": 100}}\n```\n"
    "Available tools:\n"
    "- read_file {path, start_line, end_line}: read lines of a repo file.\n"
    "- run_command {command}: run a shell command in the repo directory.\n"
    "- edit_file {path, old_text, new_text}: replace exactly one occurrence "
    "of old_text with new_text in a source file (never a test file).\n"
    "- finish {summary}: end the task after running the relevant tests.\n"
    "After each tool result, output your next single tool block. One tool "
    "per reply."
)


def parse_tool_block(content):
    """Return (name, args) from a ```tool JSON block, or None."""
    m = re.search(r"```tool\s*(\{.*?\})\s*```", content or "", re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return None
    if isinstance(obj, dict) and obj.get("tool"):
        return obj["tool"], obj.get("args") or {}
    return None


def trunc(s):
    if len(s) <= OUT_MAX_CHARS:
        return s
    return (s[:2000] + "\n...[truncated middle]...\n"
            + s[-(OUT_MAX_CHARS - 2500):])


def run_tool(repo, name, args):
    """Returns (output_text, kind) where kind in read/test/edit/other."""
    if name == "read_file":
        p = os.path.normpath(args["path"])
        if p.startswith("..") or os.path.isabs(args["path"]):
            return "ERROR: path must be repo-relative", "other"
        full = os.path.join(repo, p)
        try:
            with open(full) as f:
                lines = f.readlines()
        except Exception as e:
            return f"ERROR: {e}", "other"
        s = int(args.get("start_line", args.get("line_start", 1)))
        e = int(args.get("end_line", args.get("line_end", s + 399)))
        if e - s + 1 > READ_MAX_LINES:
            e = s + READ_MAX_LINES - 1
        out = "".join(lines[s - 1:e])
        return trunc(out) if out else "(empty range)", "read"
    if name == "run_command":
        try:
            r = subprocess.run(args["command"], shell=True, cwd=repo,
                               capture_output=True, text=True, timeout=300)
            out = trunc((r.stdout or "") + (r.stderr or ""))
            return f"exit={r.returncode}\n{out}", "test"
        except subprocess.TimeoutExpired:
            return "ERROR: command timed out after 300s", "test"
    if name == "edit_file":
        p = os.path.normpath(args["path"])
        if p.startswith("tests/") or p.startswith("test/") or "/tests/" in p:
            return "ERROR: test files cannot be edited", "other"
        if p.startswith("..") or os.path.isabs(args["path"]):
            return "ERROR: path must be repo-relative", "other"
        full = os.path.join(repo, p)
        try:
            with open(full) as f:
                content = f.read()
        except Exception as e:
            return f"ERROR: {e}", "other"
        old, new = args["old_text"], args["new_text"]
        n = content.count(old)
        if n != 1:
            return f"ERROR: old_text occurs {n} times (need exactly 1)", "other"
        with open(full, "w") as f:
            f.write(content.replace(old, new))
        return "OK: edit applied", "edit"
    return f"ERROR: unknown tool {name}", "other"
