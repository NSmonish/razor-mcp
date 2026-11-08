#!/usr/bin/env python3
"""Task state file for Step 4 (the structural fix for replay, per
TRACKBACK-REPLAY.md: masking trims history, the state file BREAKS
accumulation by replacing the transcript with compact state + a small
recent window).

Method decision (recorded — this is what makes the state file safe
against the compaction constraint-loss warning, violations 0% -> ~30%
after one LLM compaction):
  - The state is written by the HARNESS, never by the model. Every field
    is extracted deterministically from verifiable artefacts: the
    frozen task prompt (goals, failing tests, constraints VERBATIM),
    the trajectory event log (tool calls + results), live `git diff`
    (current patch), and the last razor_run_tests result (test status).
  - NOTHING in the state is LLM-summarized. Constraints are copied
    verbatim from the task prompt and re-injected every call; they are
    never derived from agent output. Findings are compact structured
    facts (tool, path, counts), not prose summaries.
  - open_questions / next_steps are deterministic rules over
    (edits, test status), not agent reasoning: they can only name
    states the harness observed, never invent a plan.

Validation (every turn, before the state is sent): frozen fields must
match exactly, test status must equal the last recorded test result,
current patch must equal live `git diff` exactly, schema + types must
hold, and the JSON must fit STATE_MAX_CHARS. ANY failure -> the arm
falls back to the Step 3 masked view for the REST of the arm (sticky
fallback, flagged in the ledger). A fallback arm is still valid.

Recent window: the last WINDOW_TURNS complete turns verbatim
(turn-aligned, so provider atomicity holds: every tool_call in the
window has its tool result in the window).

Step 4 repair (F1): WINDOW_TURNS 3 -> 6. Step 4's 3-turn window
evaporated reasoning and code content the agent needed to act on
(PROGRESS-step4-fix.md, Cause 1): only the last 3 turns went
verbatim, older reasoning was gone (not stubbed, not summarized),
and findings were 120-char one-liners with no code content. 6 turns
restores 2x reasoning/code context per call while keeping prompts
well below Step 3's full-transcript level.

Step 4 repair (F3): the state also carries pinned verbatim excerpts
(most recent read per file, capped), the last assistant text
verbatim, and concrete next_steps naming the last-read source file
and the failing tests. Adaptive trimming (drop oldest excerpts
first, then reasoning, then oldest findings, then oldest
files_read) guarantees the ADDED fields never push a state over
the cap; the current_patch diff is never trimmed (validation must
compare it exactly to live git output), so a diff that alone
exceeds the cap still trips fallback exactly as before.

Step 4 repair (F4): the state records the last edit attempt
(last_edit: path, ok, error verbatim) and next_steps react to a
rejected edit instead of repeating a generic "edit" instruction.
Failed edits are never recorded as "edited" (see loop.py
_event_for).
"""
import json

STATE_VERSION = 1
STATE_MAX_CHARS = 12000
WINDOW_TURNS = 6  # Step 4 repair F1 (was 3)
MAX_FINDINGS = 20
MAX_FILES_READ = 30
MAX_DETAIL_CHARS = 120
# Step 4 repair F3: richer-state caps (headroom proven — Step 4 final
# states were mostly 2,000-4,500 chars, max 8,007, vs the 12,000 cap).
MAX_EXCERPTS = 4
EXCERPT_MAX_CHARS = 800
LAST_ASSISTANT_MAX_CHARS = 500

REQUIRED_FIELDS = {
    "version": int,
    "goal": str,
    "failing_tests": list,
    "constraints": list,
    "findings": list,
    "files_read": list,
    "current_patch": dict,
    "test_status": (dict, type(None)),
    "open_questions": list,
    "next_steps": list,
    "turn": int,
    "steps_used": int,
    # Step 4 repair F3/F4 fields (added by build_state; validated
    # for presence + type only, never for content — they are derived,
    # not frozen).
    "excerpts": list,
    "last_assistant_text": str,
    "last_edit": (dict, type(None)),
}


def extract_constraints(prompt):
    """Deterministic constraint extraction from a task prompt: every
    sentence starting with 'Do not ' is a frozen constraint, verbatim
    (including its terminator). No model, no inference: sentences that
    do not start with 'Do not ' are never constraints, and constraint
    text is never reworded. Returns [] when the prompt states no
    prohibitions."""
    import re
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", (prompt or "").strip()):
        s = sent.strip()
        if s.startswith("Do not "):
            out.append(s)
    return out


def _parse_patch_files(diff_text):
    """File paths from a unified diff (+++ b/<path> lines)."""
    files = []
    for line in (diff_text or "").splitlines():
        if line.startswith("+++ b/"):
            files.append(line[6:])
    return files


def derive_next_steps(n_edits, test_status, last_read_path=None,
                      last_edit=None):
    """Deterministic next-step rules over observed harness state only.
    Backward-compatible: with last_read_path=None and last_edit=None
    the outputs are exactly the Step 4 originals (existing tests
    rely on the test_status-None, exit-0, and failed-test strings).
    Step 4 repair F3/F4: when a last edit was REJECTED, the first
    step reacts to the rejection verbatim instead of repeating a
    generic edit instruction; when no edit has landed yet and a
    source file was last read, the edit step names that file
    concretely instead of "the source file identified in findings"."""
    steps = []
    if last_edit is not None and not last_edit.get("ok", True):
        steps.append(
            f"Last edit to {last_edit.get('path', '')} was rejected: "
            f"{last_edit.get('error', '')}; re-read the exact lines "
            f"in {last_edit.get('path', '')} before retrying")
    if test_status is None:
        steps.append("Reproduce the failure with razor_run_tests")
        return steps
    if test_status.get("exit_code") == 0:
        return ["Call finish"]
    failed = test_status.get("failed") or []
    if n_edits == 0 and last_read_path:
        steps.append(f"Edit {last_read_path} to fix the failing tests")
    if failed:
        steps.append(f"Fix failing tests: {', '.join(failed[:3])}")
        steps.append("Re-run razor_run_tests")
    else:
        steps.append("Fix the failing tests")
        steps.append("Re-run razor_run_tests")
    return steps


def derive_open_questions(n_edits, test_status):
    """Deterministic open questions over observed harness state only."""
    if test_status is None:
        return ["Which source location causes the failing tests?"]
    failed = test_status.get("failed") or []
    if test_status.get("exit_code") == 0:
        return []
    if failed:
        return [f"How to fix failing tests: {', '.join(failed[:3])}"]
    return ["Why do the tests still fail?"]


def _state_size(state):
    return len(json.dumps(state, separators=(",", ":")))


def _trim_to_cap(state):
    """Adaptive trimming (Step 4 repair F3): drop oldest excerpts
    first (list is most-recent-first, so pop from the end), then
    reasoning, then oldest findings, then oldest files_read — until
    the state fits STATE_MAX_CHARS. The current_patch diff is NEVER
    trimmed (validation compares it exactly to live git output); if
    the diff alone exceeds the cap, the state stays oversize and
    validation fails -> fallback, exactly as before the repair."""
    while _state_size(state) > STATE_MAX_CHARS:
        if state["excerpts"]:
            state["excerpts"].pop()
        elif state["last_assistant_text"]:
            state["last_assistant_text"] = ""
        elif state["findings"]:
            state["findings"].pop(0)
        elif state["files_read"]:
            state["files_read"].pop(0)
        else:
            break
    return state


def build_state(frozen, events, diff_text, diff_stat, last_test,
                turn, steps_used, n_edits, excerpts=None,
                last_assistant_text="", last_edit=None):
    """Build the state dict deterministically. frozen: {goal,
    failing_tests, constraints} (verbatim task fields). events: list of
    {turn, kind, detail, path, artifact?}. diff_text/diff_stat: live
    git output. last_test: {exit_code, failed} or None. excerpts:
    most-recent-first list of {path, lines, content} (most recent
    read per file, from the loop) — capped at MAX_EXCERPTS and
    EXCERPT_MAX_CHARS each. last_assistant_text: the agent's last
    non-empty text, verbatim up to LAST_ASSISTANT_MAX_CHARS.
    last_edit: {path, ok, error} of the last edit attempt, or None.
    Never calls a model. New parameters are optional so existing
    callers (Step 4 tests, old drivers) keep working."""
    findings = []
    for ev in events[-MAX_FINDINGS:]:
        f = {"turn": ev.get("turn"),
             "kind": ev.get("kind"),
             "detail": (ev.get("detail") or "")[:MAX_DETAIL_CHARS]}
        # Step 4 repair F2: findings carry the artifact id so ANY
        # older result is re-readable by reference in state mode.
        if ev.get("artifact"):
            f["artifact"] = ev["artifact"]
        findings.append(f)
    files_read = []
    for ev in events:
        p = ev.get("path")
        # edit_failed counts as file-relevant (the agent attempted
        # that file), preserving Step 4's coverage now that failed
        # edits have their own kind (F4) instead of "edit".
        if p and ev.get("kind") in ("read", "edit", "edit_failed") \
                and p not in files_read:
            files_read.append(p)
    files_read = files_read[-MAX_FILES_READ:]
    norm_excerpts = []
    for ex in (excerpts or [])[:MAX_EXCERPTS]:
        norm_excerpts.append({
            "path": ex.get("path", ""),
            "lines": ex.get("lines", ""),
            "excerpt": (ex.get("content", "") or "")[:EXCERPT_MAX_CHARS]})
    last_read_path = norm_excerpts[0]["path"] if norm_excerpts else None
    state = {
        "version": STATE_VERSION,
        "goal": frozen["goal"],
        "failing_tests": list(frozen["failing_tests"]),
        "constraints": list(frozen["constraints"]),
        "findings": findings,
        "files_read": files_read,
        "current_patch": {
            "files": _parse_patch_files(diff_text),
            "stat": (diff_stat or "").strip(),
            "diff": diff_text or "",
        },
        "test_status": (dict(last_test) if last_test is not None else None),
        "open_questions": derive_open_questions(n_edits, last_test),
        "next_steps": derive_next_steps(n_edits, last_test,
                                        last_read_path, last_edit),
        "turn": turn,
        "steps_used": steps_used,
        "excerpts": norm_excerpts,
        "last_assistant_text": (last_assistant_text or "")[
            :LAST_ASSISTANT_MAX_CHARS],
        "last_edit": (dict(last_edit) if last_edit is not None else None),
    }
    return _trim_to_cap(state)


def state_message(state):
    """The message sent to the model carrying the state. Role 'user':
    it is harness-maintained context, not a new task instruction."""
    return {
        "role": "user",
        "content": ("TASK STATE (harness-maintained, updated each turn; "
                    "constraints below are verbatim and authoritative; "
                    "older transcript is replaced by this state plus "
                    "the recent turns that follow — re-read artifacts "
                    "or re-run tests if any state fact looks wrong):\n"
                    + json.dumps(state, separators=(",", ":"))),
    }


def validate_state(state, frozen, diff_text, diff_stat, last_test):
    """Validate a built state against frozen task fields, the last test
    result, and live git output. Returns (ok, reasons list). Any
    failure means the arm must fall back to the full (masked) view."""
    reasons = []
    if not isinstance(state, dict):
        return False, ["state is not a dict"]
    if state.get("version") != STATE_VERSION:
        reasons.append(f"version != {STATE_VERSION}")
    for field, typ in REQUIRED_FIELDS.items():
        if field not in state:
            reasons.append(f"missing field: {field}")
        elif not isinstance(state[field], typ):
            reasons.append(f"field {field} has wrong type")
    if reasons:
        return False, reasons
    if state["goal"] != frozen["goal"]:
        reasons.append("goal does not match frozen task goal")
    if state["failing_tests"] != list(frozen["failing_tests"]):
        reasons.append("failing_tests do not match frozen task list")
    if state["constraints"] != list(frozen["constraints"]):
        reasons.append("constraints are not verbatim frozen constraints")
    want_test = dict(last_test) if last_test is not None else None
    if state["test_status"] != want_test:
        reasons.append("test_status does not match last test result")
    patch = state["current_patch"]
    if patch.get("diff") != (diff_text or ""):
        reasons.append("current_patch diff does not match live git diff")
    if patch.get("files") != _parse_patch_files(diff_text):
        reasons.append("current_patch files do not match live git diff")
    if (patch.get("stat") or "").strip() != (diff_stat or "").strip():
        reasons.append("current_patch stat does not match live git diff")
    try:
        size = len(json.dumps(state, separators=(",", ":")))
    except Exception:
        return False, ["state is not JSON-serializable"]
    if size > STATE_MAX_CHARS:
        reasons.append(f"state JSON {size} chars exceeds cap "
                       f"{STATE_MAX_CHARS}")
    return (len(reasons) == 0), reasons


def recent_window(messages, window_turns=WINDOW_TURNS):
    """Last `window_turns` complete turns from stored messages, verbatim.
    messages[0] is system, messages[1] is the task user prompt; turns
    start at each assistant message and run to the next assistant
    message. Returns the window messages (turn-aligned: every tool_call
    in the window has its tool result in the window)."""
    turns = []
    current = None
    for msg in messages[2:]:
        if msg.get("role") == "assistant":
            if current is not None:
                turns.append(current)
            current = [msg]
        elif current is not None:
            current.append(msg)
        # Messages before the first assistant message (none expected
        # after system+task) are dropped: they belong to no turn.
    if current is not None:
        turns.append(current)
    window = []
    for turn in turns[-window_turns:] if window_turns > 0 else []:
        window.extend(turn)
    return window
