#!/usr/bin/env python3
"""Budgeted Razor read tools (Step 2: the ONLY lever under test).

Six tools, hard char budgets (chars/4 estimator, same as telemetry):
  razor_search        <= 8,000 chars (locate)
  razor_outline       <= 6,000 chars (map)
  razor_read          <= 4,000 chars inline (snippet; longer -> artifact)
  razor_read_artifact <= 4,000 chars per slice/grep (artifact read-back)
  razor_run_tests     <= 4,000 chars inline failures (full log -> artifact)
  razor_status        small budget report (excludes its own output)

Every tool returns a compact JSON string: structured, decision-ready,
with totals and truncation flags (never let the model assume a result is
complete when truncated=true). Server-side validation: repo-relative
paths only, ranges validated, artifacts addressed by id (never by path),
and razor_run_tests runs a FIXED command set at construction (it takes
no command argument, so it cannot be turned into a shell bypass).

Tool definitions are frozen per session: ablation_tool_defs() returns a
fresh deep copy per arm, and the driver never mutates it (mutating the
tool array mid-run would invalidate provider prefix caching).

Accounting convention: the runner notes every returned output's chars
into inline_chars, EXCEPT razor_status's own output (circular). Status
therefore reports the state before its own call. Artifact re-reads are
counted separately (artifact_reads, artifact_read_chars) so the net
saving of offloading can be checked: if re-reads approach the chars
saved by capping, the lever is not paying.
"""
import ast
import copy
import json
import os
import re
import subprocess

from tools import SYSTEM as NAIVE_SYSTEM
from tools import TOOLS as NAIVE_TOOLS
from tools import run_tool as naive_run_tool

SEARCH_MAX_CHARS = 8000
OUTLINE_MAX_CHARS = 6000
READ_MAX_CHARS = 4000
ARTIFACT_SLICE_MAX_CHARS = 4000
TEST_INLINE_MAX_CHARS = 4000
READ_MAX_LINES = 500
TEST_TIMEOUT_S = 600
# Phase 1 search result-count cap (§4.2): top N hits, ranking
# preserved. The Phase 1 driver passes this; Step 3 passes None.
SEARCH_MAX_RESULTS = 50

SKIP_DIRS = {".git", "__pycache__", "venv", ".venv", ".tox",
             "node_modules", "build", "dist", ".eggs", ".hg", ".svn"}

RAZOR_READ_PATH = (
    "You MUST use the razor_* tools for all code reading and test "
    "running. Native file reads and shell commands are NOT available in "
    "this harness. Workflow: (1) razor_outline or razor_search to locate "
    "relevant code. (2) razor_read for snippets (at most 4000 chars "
    "inline; longer content is saved as an artifact — read it back in "
    "slices with razor_read_artifact). (3) razor_run_tests to run the "
    "tests (failures inline, full log via razor_read_artifact). "
    "(4) Edit with edit_file. (5) Check budgets with razor_status. "
    "Do NOT try to read files or run commands any other way — those "
    "tools do not exist here. Limits are hard: search at most 8000 "
    "chars, outline at most 6000, read at most 4000, test output at "
    "most 4000 inline; totals and truncation flags are always reported, "
    "never assume a result is complete when truncated=true."
)


def _err(tool, msg, hint):
    return json.dumps({"tool": tool, "error": msg, "hint": hint},
                      separators=(",", ":"))


def _ok(obj):
    return json.dumps(obj, separators=(",", ":"))


def _resolve(repo, path):
    """Return (full_path, error_string_or_None). Repo-relative only."""
    if not path or not isinstance(path, str):
        return None, "path is required and must be a string"
    if os.path.isabs(path):
        return None, "path must be repo-relative, not absolute"
    norm = os.path.normpath(path)
    if norm.startswith(".."):
        return None, "path must stay inside the repo (no .. escapes)"
    return os.path.join(repo, norm), None


def _read_text(full):
    try:
        with open(full, errors="replace") as f:
            return f.read()
    except OSError as e:
        return None


def _py_files(repo):
    out = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for fn in sorted(files):
            if fn.endswith(".py"):
                out.append(os.path.relpath(os.path.join(root, fn), repo))
    return sorted(out)


def _sig(node):
    parts = []
    a = node.args
    parts += [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    parts += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    return "(" + ", ".join(parts) + ")"[:80]


def _symbols_of_file(full):
    """Return (symbols, skipped_bool). Symbols: {name, kind, line, sig}
    in source order; methods qualified as Class.method."""
    text = _read_text(full)
    if text is None:
        return [], True
    try:
        tree = ast.parse(text)
    except Exception:
        return [], True
    syms = []

    def walk(node, cls=None):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                syms.append({"name": child.name, "kind": "class",
                             "line": child.lineno, "sig": ""})
                walk(child, cls=child.name)
            elif isinstance(child, (ast.FunctionDef,
                                    ast.AsyncFunctionDef)):
                nm = f"{cls}.{child.name}" if cls else child.name
                syms.append({"name": nm, "kind": "func",
                             "line": child.lineno, "sig": _sig(child)})
                walk(child, cls=None)
            else:
                walk(child, cls=cls)

    walk(tree)
    syms.sort(key=lambda s: s["line"])
    return syms, False


class RazorState:
    """Per-arm tool state: repo, fixed test command, artifact store,
    and budget counters. One instance per arm, never shared across arms."""

    def __init__(self, repo, artifacts_dir, test_command=None,
                 search_result_cap=None):
        self.repo = os.path.abspath(repo)
        self.artifacts_dir = os.path.abspath(artifacts_dir)
        os.makedirs(self.artifacts_dir, exist_ok=True)
        self.test_command = test_command
        # Phase 1 search result-count cap (§4.2): None = no count
        # cap (Step 3 behavior, unchanged); an int caps returned
        # hits at that many (ranking preserved, total_hits still
        # the full count). Set by the Phase 1 driver only.
        self.search_result_cap = search_result_cap
        # Phase 1 gate state (§4.3): set by the driver's status
        # wrapper from the loop's gate context; None = no gates
        # block in status_dict (Step 3 behavior, unchanged).
        self.gate_state = None
        self.tool_calls = {}
        self.inline_chars = 0
        self.artifacts = {}
        self.artifact_seq = 0
        self.artifact_chars_total = 0
        self.artifact_reads = 0
        self.artifact_read_chars = 0
        # Step 3 masked-history store: full contents of tool results
        # that were masked out of the replayed history, kept separate
        # from tool-produced artifacts so status accounting stays
        # comparable with Step 2. Retrieved via razor_read_artifact.
        self.history = {}
        self.history_seq = 0
        self.history_chars_total = 0
        self.history_reads = 0
        self.history_read_chars = 0

    def new_artifact(self, kind, content):
        self.artifact_seq += 1
        aid = f"art-{self.artifact_seq:03d}"
        path = os.path.join(self.artifacts_dir, aid + ".txt")
        with open(path, "w") as f:
            f.write(content)
        self.artifacts[aid] = {"id": aid, "kind": kind, "path": path,
                               "total_chars": len(content)}
        self.artifact_chars_total += len(content)
        return aid

    def new_history(self, content):
        """Store a masked-out tool result for later re-read. Returns its
        id (hist-NNN). Called lazily by the loop's masking, only for
        results without their own tool artifact id."""
        self.history_seq += 1
        aid = f"hist-{self.history_seq:03d}"
        path = os.path.join(self.artifacts_dir, aid + ".txt")
        with open(path, "w") as f:
            f.write(content)
        self.history[aid] = {"id": aid, "kind": "history", "path": path,
                             "total_chars": len(content)}
        self.history_chars_total += len(content)
        return aid

    def note_inline(self, out):
        self.inline_chars += len(out)

    def set_gate_state(self, gates):
        """Phase 1 (§4.3): store the current gate snapshot for
        the next status_dict() call. Pass None to clear."""
        self.gate_state = gates

    def status_dict(self):
        d = {
            "tool": "razor_status",
            "tool_calls": dict(self.tool_calls),
            "inline_chars": self.inline_chars,
            "tokens_est": round(self.inline_chars / 4),
            "artifacts": [{"id": a["id"], "kind": a["kind"],
                           "total_chars": a["total_chars"]}
                          for a in self.artifacts.values()],
            "artifacts_created": len(self.artifacts),
            "artifact_chars_total": self.artifact_chars_total,
            "artifact_reads": self.artifact_reads,
            "artifact_read_chars": self.artifact_read_chars,
            "history_artifacts_created": len(self.history),
            "history_chars_total": self.history_chars_total,
            "history_reads": self.history_reads,
            "history_read_chars": self.history_read_chars,
            "caps": {"razor_search": SEARCH_MAX_CHARS,
                     "razor_outline": OUTLINE_MAX_CHARS,
                     "razor_read": READ_MAX_CHARS,
                     "razor_read_artifact": ARTIFACT_SLICE_MAX_CHARS,
                     "razor_run_tests": TEST_INLINE_MAX_CHARS},
            "note": "excludes this status call's own output",
        }
        # Phase 1 additions, present only when set (Step 3
        # status output is byte-identical otherwise).
        if self.search_result_cap is not None:
            d["caps"]["razor_search_max_results"] = \
                self.search_result_cap
        if self.gate_state is not None:
            d["gates"] = self.gate_state
        return d


def _fit_items(base, items, cap):
    """Greedily add items (each a dict) to base['items'] until the JSON
    would exceed cap. Returns (obj, truncated)."""
    out = dict(base)
    out["items"] = []
    # Reserve the longest flag spelling so callers setting
    # out["truncated"] afterwards cannot push the JSON over the cap
    # (true is one char shorter than false).
    out["truncated"] = False
    for it in items:
        out["items"].append(it)
        if len(json.dumps(out, separators=(",", ":"))) > cap:
            out["items"].pop()
            return out, True
    return out, False


def _fit_content(base, key, content, cap):
    """Fit a content string into base[key] so the JSON stays <= cap.
    Returns (obj, was_trimmed). was_trimmed=True means the returned
    string is NOT the full content — callers must report truncation
    honestly and, for reads, provide the artifact id. The fit is
    measured on the actual JSON string (escapes inflate newlines and
    quotes, so raw char counts would overshoot the cap)."""
    out = dict(base)
    out[key] = content
    if len(json.dumps(out, separators=(",", ":"))) <= cap:
        return out, False
    lo, hi = 0, len(content)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        out[key] = content[:mid]
        if len(json.dumps(out, separators=(",", ":"))) <= cap:
            lo = mid
        else:
            hi = mid - 1
    out[key] = content[:lo]
    return out, True


def tool_search(state, args):
    query = args.get("query", "")
    if not isinstance(query, str) or not query.strip():
        return _err("razor_search", "query is required and must be "
                    "non-empty", "pass a symbol name or text, e.g. "
                    "{'query': 'url_path_for'}"), "read"
    query = query.strip()
    pat_def = re.compile(r"^\s*(async\s+def|def|class)\s+"
                         + re.escape(query) + r"\b")
    pat_ident = re.compile(r"\b" + re.escape(query) + r"\b")
    hits = []
    scanned = 0
    for rel in _py_files(state.repo):
        text = _read_text(os.path.join(state.repo, rel))
        if text is None:
            continue
        scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if not s:
                continue
            if pat_def.match(line):
                tier, kind = 0, "def"
            elif pat_ident.search(line):
                tier, kind = 1, "ref"
            elif query.lower() in line.lower():
                tier, kind = 2, "ref"
            else:
                continue
            hits.append((tier, rel, i, kind, s[:120]))
    hits.sort(key=lambda h: (h[0], h[1], h[2]))
    all_hits = [{"path": p, "line": ln, "kind": k, "preview": pv}
                for _, p, ln, k, pv in hits]
    # Phase 1 search result-count cap (§4.2): slice AFTER the
    # definitions-first ranking, so ranking is preserved; the
    # full count is still reported as total_hits, and truncated
    # is True when the count cap (or the char fit) bit. None =
    # no count cap (Step 3 behavior, unchanged).
    full_hits = len(all_hits)
    count_capped = False
    cap = getattr(state, "search_result_cap", None)
    if cap is not None and len(all_hits) > cap:
        all_hits = all_hits[:cap]
        count_capped = True
    base = {"tool": "razor_search", "query": query,
            "total_hits": full_hits, "files_scanned": scanned}
    out, truncated = _fit_items(base, all_hits, SEARCH_MAX_CHARS)
    out["returned_hits"] = len(out["items"])
    out["hits"] = out.pop("items")
    out["truncated"] = truncated or count_capped
    return _ok(out), "read"


def tool_outline(state, args):
    path = args.get("path", "") or ""
    if path:
        full, err = _resolve(state.repo, path)
        if err:
            return _err("razor_outline", err, "pass a repo-relative "
                        "file or directory path, or omit path for the "
                        "whole repo"), "read"
        if os.path.isfile(full):
            if not full.endswith(".py"):
                return _err("razor_outline", "outline only supports "
                            "Python files", "pass a .py file or a "
                            "directory"), "read"
            targets = [(path, full)]
        elif os.path.isdir(full):
            targets = [(os.path.relpath(os.path.join(r, f), state.repo),
                        os.path.join(r, f))
                       for r, ds, fs in os.walk(full)
                       for f in sorted(fs) if f.endswith(".py")]
            targets.sort()
        else:
            return _err("razor_outline", f"no such file or directory: "
                        f"{path}", "check the path with razor_search "
                        "first, or omit path"), "read"
    else:
        targets = [(rel, os.path.join(state.repo, rel))
                   for rel in _py_files(state.repo)]
    entries = []
    total_symbols = 0
    skipped = 0
    for rel, full in targets:
        syms, bad = _symbols_of_file(full)
        if bad:
            skipped += 1
            continue
        total_symbols += len(syms)
        entries.append({"path": rel, "symbols": syms})
    base = {"tool": "razor_outline", "total_files": len(entries),
            "total_symbols": total_symbols,
            "files_skipped_unparseable": skipped}
    out, truncated = _fit_items(base, entries, OUTLINE_MAX_CHARS)
    # _fit_items uses key "items"; rename to "files" and handle a file
    # whose own symbol list overflows by trimming its symbols.
    files = out.pop("items")
    if truncated and entries and len(files) < len(entries):
        nxt = entries[len(files)]
        trimmed = {"path": nxt["path"], "symbols": [],
                   "file_truncated": True}
        for s in nxt["symbols"]:
            trimmed["symbols"].append(s)
            probe = dict(out)
            probe["files"] = files + [trimmed]
            if len(json.dumps(probe, separators=(",", ":"))) > \
                    OUTLINE_MAX_CHARS:
                trimmed["symbols"].pop()
                break
        if trimmed["symbols"]:
            files.append(trimmed)
    out["files"] = files
    out["truncated"] = truncated
    return _ok(out), "read"


def _find_symbol(state, symbol):
    """Return (rel_path, start_line, end_line) for a def/class name,
    repo-wide, deterministic order. None if not found. Matches plain
    and Class-qualified names (a def line never literally contains
    'Class.method', so matching goes through parsed symbols)."""
    for rel in _py_files(state.repo):
        full = os.path.join(state.repo, rel)
        found = _find_symbol_in_file(full, symbol)
        if found:
            return rel, found[0], found[1]
    return None


def _find_symbol_in_file(full, symbol):
    """Return (start_line, end_line) for a def/class name inside one
    file, or None. Matches plain and Class-qualified names."""
    syms, bad = _symbols_of_file(full)
    if bad:
        return None
    text = _read_text(full)
    n = len(text.splitlines()) if text else 0
    for s in syms:
        if s["name"] == symbol or s["name"].endswith("." + symbol):
            return s["line"], min(_node_end(full, s["line"]), n)
    return None


def _node_end(full, start_line):
    """End line of the def/class starting at start_line, via AST."""
    text = _read_text(full)
    if text is None:
        return start_line + 50
    try:
        tree = ast.parse(text)
    except Exception:
        return start_line + 50
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.lineno == start_line:
            return getattr(node, "end_lineno", None) or start_line + 50
    return start_line + 50


def tool_read(state, args):
    path = args.get("path", "") or ""
    symbol = args.get("symbol", "") or ""
    if symbol and path:
        full0, err0 = _resolve(state.repo, path)
        if err0:
            return _err("razor_read", err0, "pass a repo-relative path "
                        "inside the repo"), "read"
        if not os.path.isfile(full0):
            return _err("razor_read", f"not a file: {path}", "use "
                        "razor_search or razor_outline to find the right "
                        "path"), "read"
        found = _find_symbol_in_file(full0, symbol)
        if not found:
            return _err("razor_read", f"symbol not found in {path}: "
                        f"{symbol}", "try razor_search for the symbol "
                        "first"), "read"
        start, end = found
    elif symbol:
        found = _find_symbol(state, symbol)
        if not found:
            return _err("razor_read", f"symbol not found: {symbol}",
                        "try razor_search for the symbol first, then "
                        "read by path + line range"), "read"
        path, start, end = found
    else:
        if not path:
            return _err("razor_read", "path or symbol is required",
                        "pass {'path': 'pkg/mod.py', 'start_line': 1, "
                        "'end_line': 100} or {'symbol': 'func_name'}"), \
                "read"
        try:
            start = int(args.get("start_line", 1))
            end = int(args.get("end_line", start + 99))
        except (TypeError, ValueError):
            return _err("razor_read", "start_line/end_line must be "
                        "integers", "pass e.g. {'start_line': 1, "
                        "'end_line': 100}"), "read"
        if start < 1 or end < start:
            return _err("razor_read", "invalid range: need 1 <= "
                        "start_line <= end_line", "pass a valid range, "
                        "e.g. start_line 1, end_line 100"), "read"
        if end - start + 1 > READ_MAX_LINES:
            return _err("razor_read", f"range too large: {end - start + 1} "
                        f"lines (max {READ_MAX_LINES})", "narrow the "
                        "range, or read the saved artifact in slices "
                        "with razor_read_artifact"), "read"
    full, err = _resolve(state.repo, path)
    if err:
        return _err("razor_read", err, "pass a repo-relative path "
                    "inside the repo"), "read"
    if not os.path.isfile(full):
        return _err("razor_read", f"not a file: {path}", "use "
                    "razor_search or razor_outline to find the right "
                    "path"), "read"
    text = _read_text(full)
    if text is None:
        return _err("razor_read", f"cannot read file: {path}", "check "
                    "the path and try again"), "read"
    lines = text.splitlines()
    if start > len(lines):
        return _err("razor_read", f"start_line {start} beyond file "
                    f"({len(lines)} lines)", f"read within 1.."
                    f"{len(lines)}"), "read"
    end = min(end, len(lines))
    content = "\n".join(lines[start - 1:end])
    if len(lines) > end:
        content += "\n"
    base = {"tool": "razor_read", "path": path, "start_line": start,
            "end_line": end, "total_lines": len(lines),
            "total_range_chars": len(content), "artifact_id": None,
            "truncated": False}
    out, trimmed = _fit_content(base, "content", content, READ_MAX_CHARS)
    if trimmed:
        # Full range does not fit under the cap: artifact it and refit
        # with the id present, reporting truncated honestly.
        base["artifact_id"] = state.new_artifact("read", content)
        out, _ = _fit_content(base, "content", content, READ_MAX_CHARS)
        out["truncated"] = True
    else:
        out["truncated"] = False
    return _ok(out), "read"


def tool_read_artifact(state, args):
    aid = args.get("artifact_id", "") or ""
    rec = state.artifacts.get(aid)
    from_history = False
    if rec is None:
        rec = state.history.get(aid)
        from_history = rec is not None
    if not rec:
        known = sorted(state.artifacts.keys()) + \
            sorted(state.history.keys())
        return _err("razor_read_artifact", f"unknown artifact_id: "
                    f"{aid}", "use an id from a tool result or "
                    "razor_status; known ids: "
                    + (", ".join(known) if known else "(none yet)")), \
                "read"
    content = _read_text(rec["path"])
    if content is None:
        return _err("razor_read_artifact", f"artifact file missing: "
                    f"{aid}", "re-run the tool that produced it"), "read"
    total = len(content)
    grep = args.get("grep", "") or ""
    if grep:
        matches = [(i + 1, ln) for i, ln in
                   enumerate(content.splitlines())
                   if grep.lower() in ln.lower()]
        text = "\n".join(f"{n}: {ln}" for n, ln in matches)
        base = {"tool": "razor_read_artifact", "artifact_id": aid,
                "kind": rec["kind"], "grep": grep,
                "match_count": len(matches), "total_chars": total,
                "truncated": False, "has_more": False}
        out, truncated = _fit_content(base, "content", text,
                                      ARTIFACT_SLICE_MAX_CHARS)
        out["truncated"] = truncated
        out["has_more"] = truncated
        state.artifact_reads += 1
        state.artifact_read_chars += len(out["content"])
        if from_history:
            state.history_reads += 1
            state.history_read_chars += len(out["content"])
        return _ok(out), "read"
    try:
        start = int(args.get("start_char", 0))
        end = args.get("end_char", None)
        end = int(end) if end is not None else start + \
            ARTIFACT_SLICE_MAX_CHARS
    except (TypeError, ValueError):
        return _err("razor_read_artifact", "start_char/end_char must "
                    "be integers", "pass e.g. {'start_char': 0}"), "read"
    if start < 0 or end < start:
        return _err("razor_read_artifact", "invalid slice: need 0 <= "
                    "start_char <= end_char", "pass a valid slice"), \
                "read"
    if end - start > ARTIFACT_SLICE_MAX_CHARS:
        end = start + ARTIFACT_SLICE_MAX_CHARS
    piece = content[start:end]
    has_more = end < total
    base = {"tool": "razor_read_artifact", "artifact_id": aid,
            "kind": rec["kind"], "start_char": start,
            "end_char": end, "total_chars": total,
            # False is the longest bool spelling; the real value is
            # written after fitting, so it can only shrink the JSON.
            "has_more": False}
    out, trimmed = _fit_content(base, "content", piece,
                                ARTIFACT_SLICE_MAX_CHARS)
    # end_char must describe the chars actually returned (fitting may
    # trim the piece by JSON overhead), or paging would skip chars.
    out["end_char"] = start + len(out["content"])
    out["has_more"] = out["end_char"] < total
    state.artifact_reads += 1
    state.artifact_read_chars += len(out["content"])
    if from_history:
        state.history_reads += 1
        state.history_read_chars += len(out["content"])
    return _ok(out), "read"


def tool_run_tests(state, args):
    if not state.test_command:
        return _err("razor_run_tests", "no test command configured "
                    "for this run", "run this tool only inside a "
                    "harness arm, where the command is fixed"), "test"
    try:
        r = subprocess.run(state.test_command, shell=True,
                           cwd=state.repo, capture_output=True,
                           text=True, timeout=TEST_TIMEOUT_S)
        combined = (r.stdout or "") + (r.stderr or "")
        rc = r.returncode
    except subprocess.TimeoutExpired as e:
        partial = ""
        if isinstance(e.stdout, str):
            partial += e.stdout
        if isinstance(e.stderr, str):
            partial += e.stderr
        aid = state.new_artifact("test_log", partial or "TIMEOUT")
        return _ok({"tool": "razor_run_tests", "exit_code": -1,
                    "passed": False,
                    "failures_inline": "TIMEOUT after "
                    f"{TEST_TIMEOUT_S}s",
                    "total_chars": len(partial), "truncated": False,
                    "artifact_id": aid}), "test"
    lines = combined.splitlines()
    failed = [ln for ln in lines
              if re.match(r"^(FAILED|ERROR)\s", ln.strip())]
    if rc == 0:
        inline_text = "PASSED (exit 0)\n" + "\n".join(lines[-10:])
    else:
        inline_text = "\n".join(failed + lines[-30:])
    aid = state.new_artifact("test_log", combined)
    base = {"tool": "razor_run_tests", "exit_code": rc,
            "passed": rc == 0, "total_chars": len(combined),
            "artifact_id": aid, "truncated": False}
    out, truncated = _fit_content(base, "failures_inline", inline_text,
                                  TEST_INLINE_MAX_CHARS)
    out["truncated"] = truncated
    return _ok(out), "test"


def tool_status(state, args):
    return _ok(state.status_dict()), "other"


RAZOR_TOOL_DEFS = [
    {"type": "function", "function": {
        "name": "razor_search",
        "description": "Search Python source and test files for a symbol or text query. Use FIRST when you need to locate code (before reading files). Returns ranked hits (definitions first) with file:line and a one-line preview, at most 8000 chars, with total_hits and truncated. Do NOT use to read file contents — use razor_read for that. Do NOT use for test output — use razor_run_tests.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "razor_outline",
        "description": "Outline a file or directory: symbols (functions, classes, methods) with signatures and line numbers. Use FIRST to map unfamiliar code before reading it. At most 6000 chars, with total_files, total_symbols, truncated. Do NOT use to read code bodies — use razor_read. Omit path to outline the whole repo.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "razor_read",
        "description": "Read an exact line range (or a named symbol's definition) from one repo file. Use after razor_search/razor_outline identified the location. At most 4000 chars inline; longer ranges are saved as an artifact (artifact_id returned) — read it back in slices with razor_read_artifact. Do NOT use to dump whole files or explore the repo. Ranges over 500 lines are rejected; narrow the range.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"},
            "end_line": {"type": "integer"},
            "symbol": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "razor_read_artifact",
        "description": "Read back a capped tool output (test log, long read) by artifact id, as a char slice or grep. Use when a tool returned truncated=true with an artifact_id. At most 4000 chars per call; pass start_char to page, or grep to find lines. Never request the whole artifact at once. Do NOT use for repo files — use razor_read.",
        "parameters": {"type": "object", "properties": {
            "artifact_id": {"type": "string"},
            "start_char": {"type": "integer"},
            "end_char": {"type": "integer"},
            "grep": {"type": "string"}},
            "required": ["artifact_id"]}}},
    {"type": "function", "function": {
        "name": "razor_run_tests",
        "description": "Run the task's fixed test command (no command argument — the command is fixed by the harness). Use to reproduce the failure and to verify edits. Returns failures-only inline (at most 4000 chars) plus the full log as an artifact (read slices with razor_read_artifact). Do NOT run tests any other way. Run it before finishing.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "razor_status",
        "description": "Report your budget usage: tool calls, inline chars, estimated tokens, artifacts created, artifact re-reads, and the caps. Use to pace yourself before long reads. Read-only; small output.",
        "parameters": {"type": "object", "properties": {}}}},
]


def _def_by_name(name):
    for t in NAIVE_TOOLS:
        if t["function"]["name"] == name:
            return t
    raise KeyError(name)


def ablation_tool_defs():
    """Fresh deep copy per arm: the 6 Razor tools + edit_file + finish
    (naive semantics, reused). Never mutate the returned list mid-run:
    the tool array is frozen per session (provider prefix caching)."""
    return copy.deepcopy(RAZOR_TOOL_DEFS + [_def_by_name("edit_file"),
                                            _def_by_name("finish")])


def ablation_system():
    return NAIVE_SYSTEM + "\n\n" + RAZOR_READ_PATH


STEP3_READ_PATH = (
    "History masking is ON: older tool results in your history are "
    "replaced by short stubs (tool, size, artifact id). The most recent "
    "10 tool results stay verbatim. Nothing is lost: a stub's full "
    "content is re-readable with razor_read_artifact using its "
    "artifact id — re-read a stub whenever you need a masked result "
    "again, in slices or by grep, never by re-running the whole search. "
    "Your own edits are never masked. "
    "Final-phase guard: the last 8 steps of your 30-step budget are "
    "reserved for edit_file, razor_run_tests, and finish ONLY. In those "
    "steps, read tools (razor_search, razor_outline, razor_read, "
    "razor_read_artifact) are refused by the harness — so do your "
    "exploration early, and leave room to edit and test. If you see a "
    "GUARD refusal, edit the code, run the tests, or finish; do not "
    "call read tools again."
)


def step3_system():
    return ablation_system() + "\n\n" + STEP3_READ_PATH


# Phase 1 system text (design §4.6 + Appendix A): the masking
# paragraph is copied verbatim from STEP3_READ_PATH (deliberate
# duplication, NOT a refactor of STEP3_READ_PATH — Step 3's
# string must stay byte-identical), and the final-phase-guard
# paragraph is REPLACED by the phase-gate paragraph (Appendix A
# text, verbatim). Everything else (ablation_system) is shared.
PHASE1_READ_PATH = (
    "History masking is ON: older tool results in your history are "
    "replaced by short stubs (tool, size, artifact id). The most recent "
    "10 tool results stay verbatim. Nothing is lost: a stub's full "
    "content is re-readable with razor_read_artifact using its "
    "artifact id — re-read a stub whenever you need a masked result "
    "again, in slices or by grep, never by re-running the whole search. "
    "Your own edits are never masked. "
    "Phase gates are ON. Before your first edit, the harness "
    "has recorded a failing test run (the repro summary above) "
    "— ground your first edit in that observed failure, not in "
    "guesses. You may read and edit freely, but you may NOT "
    "finish until the module tests pass on your current patch: "
    "if you finish without a green module run on your current "
    "patch, the harness runs the module tests itself and "
    "refuses finish with the failing names — fix them and "
    "finish again. Edits that break Python syntax are rejected "
    "with the error and not applied. A refused finish does not "
    "end the task; it costs you a turn, so verify before you "
    "finish."
)


def phase1_system():
    return ablation_system() + "\n\n" + PHASE1_READ_PATH


STEP4_READ_PATH = (
    "State file is ON: each call includes a TASK STATE message "
    "(goals, verbatim constraints, findings, current patch, test "
    "status, next steps) written by the harness from verifiable "
    "artefacts — never summarized by the model — plus the last 3 "
    "turns verbatim. Older transcript is replaced by the state: do "
    "not expect to see old tool results in full; re-read an artifact "
    "or re-run a test if any state fact looks wrong. The constraints "
    "in the state are verbatim and authoritative: never violate them, "
    "and never edit test files. "
    "Loop guard: repeating the identical tool call three times in a "
    "row, or re-running the tests three times with the identical "
    "failing outcome and no edit in between, is refused by the "
    "harness. If you see a LOOP_GUARD refusal, vary your approach: "
    "different file, symbol, or query, or edit the code. "
    "Final-phase guard (unchanged from Step 3): the last 8 steps are "
    "reserved for edit_file, razor_run_tests, and finish ONLY."
)


def step4_system():
    return step3_system() + "\n\n" + STEP4_READ_PATH


def make_razor_runner(state):
    """Return a loop-compatible runner (repo, name, args) -> (out, kind).
    Unknown names return an error (the loop also pre-checks the allowed
    set and records bypass attempts). edit_file reuses naive semantics
    exactly (including the test-file rejection)."""

    def runner(repo, name, args):
        state.tool_calls[name] = state.tool_calls.get(name, 0) + 1
        if name == "razor_search":
            out, kind = tool_search(state, args)
        elif name == "razor_outline":
            out, kind = tool_outline(state, args)
        elif name == "razor_read":
            out, kind = tool_read(state, args)
        elif name == "razor_read_artifact":
            out, kind = tool_read_artifact(state, args)
        elif name == "razor_run_tests":
            out, kind = tool_run_tests(state, args)
        elif name == "razor_status":
            out, kind = tool_status(state, args)
            return out, kind  # status excludes its own output chars
        elif name == "edit_file":
            out, kind = naive_run_tool(state.repo, "edit_file", args)
        else:
            out, kind = (_err("unknown", f"unknown tool {name}",
                              "available tools: razor_search, "
                              "razor_outline, razor_read, "
                              "razor_read_artifact, razor_run_tests, "
                              "razor_status, edit_file, finish"), "other")
        state.note_inline(out)
        return out, kind

    return runner


def adoption_share(tool_mix, bypass_attempts):
    """Share of read operations that went through Razor tools.
    Read ops = razor_search/outline/read/read_artifact calls; native
    reads = bypass attempts (calls to tools outside the allowed set).
    Returns a dict; adoption is 0.0 when no read op occurred at all
    (the lever was not tested)."""
    razor_reads = sum(int(tool_mix.get(t, 0)) for t in
                      ("razor_search", "razor_outline", "razor_read",
                       "razor_read_artifact"))
    native_reads = len(bypass_attempts or [])
    denom = razor_reads + native_reads
    return {"razor_reads": razor_reads, "native_reads": native_reads,
            "adoption": (razor_reads / denom) if denom > 0 else 0.0}
