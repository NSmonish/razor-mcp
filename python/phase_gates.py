#!/usr/bin/env python3
"""Phase 1 verification-first gates (redesign plan Phase 1; design:
DESIGN-phase1-gates.md). PURE LOGIC + LEDGER, no model calls, no
loop coupling: the loop imports from here, never the reverse.

Contents:
- append-only test ledger (schema §3) + flake detection (§2 rule 8)
- repro condition (§1a) + finish-gate evaluation (§1b)
- pre-existing-unrelated waiver logic (§2 rule 4) + to-fix set
  construction (§2 rules 3/6)
- dotted-to-node-ID conversion for MATCHING ONLY (§2 rule 4):
  longest-prefix walk to a .py file + "::" join, brackets
  preserved. Converted names are NEVER passed to pytest; they
  only classify failures for waiver decisions. Unmatched names
  fail closed (treated as to-fix; see is_to_fix).
- gate-state snapshot for razor_status (§4.3)
- refusal texts (exact, §1a/§1b)

FAIL-CLOSED RULES (stated, not hidden):
- An infra-error ledger entry (exit_code None) never satisfies
  any gate: not a repro, not green, not waivable. For finish
  evaluation it is skipped when looking for the latest usable
  entry on the current patch, so a harness check still runs.
- A failing finish run with zero parsed failed names (failing-
  unparsed) can never be waived: with no names, nothing can be
  proven pre-existing-unrelated.
- A dotted name that fails conversion is treated as to-fix.
  Membership is tested by is_to_fix: exact converted match, OR
  (for unmatched names only) same test-function name as an
  unmatched dotted name — because an unmapped to-fix item could
  be that test, and waiving it would be fail-open. This shadow
  rule is an INTERPRETATION of §2 rule 6 ("their failures block
  finish") for the partial-failure case; the design's dev data
  has zero conversion failures, so it never fires on dev.
  When ALL names fail conversion, the to-fix set falls back to
  the repro failed set (§2 rule 6), which makes waiver
  impossible for repro failures (they are in to-fix) and for
  new failures (they are not in the repro set) — finish then
  requires absolute green.
- patch_id on a git failure raises: callers must decide the
  fail-closed response (loop: agent-entry "unknown" patch that
  matches nothing, so finish falls through to a harness check;
  finish evaluation: refuse as infra error). Never silently
  report "pristine".
"""
import hashlib
import os
import re
import subprocess

# Exact refusal texts (design §1a/§1b — copy verbatim in claims).
EDIT_GATE_REFUSAL = (
    "EDIT_GATE: edit refused — no failing test run is recorded "
    "yet for this task. Run razor_run_tests to record the "
    "failure before editing. (An agent test run before your "
    "first edit satisfies this gate.)")

REPRO_UNAVAILABLE_TEMPLATE = (
    "REPRO UNAVAILABLE: {reason}; proceeding without recorded "
    "repro — run razor_run_tests before editing if you can.")

FINISH_CHECK_INFRA_TEMPLATE = (
    "FINISH_GATE: finish check could not run ({reason}). "
    "Run razor_run_tests yourself; finish again after a "
    "green run.")

FINISH_WAIVER_NOTE_TEMPLATE = (
    "finished (pre-existing failures waived: {names} — "
    "failing before your edits and unrelated to the failing "
    "tests for this task; see test ledger).")

TAIL_MAX_CHARS = 1500  # ledger tail cap (§3, as run_tests does today)
FINISH_REFUSAL_MAX_NAMES = 10  # failing names shown in a refusal (§1b)


# ---------------------------------------------------------------------------
# patch_id
# ---------------------------------------------------------------------------

def patch_id_of_diff(diff_text):
    """patch_id for an already-captured `git diff -- .` output:
    sha256 hex, or "pristine" for empty diff. Callers that got the
    diff from a FAILED git call must not pass "" here (that would
    fake pristine); use patch_id() and handle its exception."""
    if not diff_text:
        return "pristine"
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def patch_id(repo):
    """sha256 of `git diff -- .` at run time, or "pristine" for an
    empty diff. Raises RuntimeError on any git failure (fail
    closed — see module docstring)."""
    try:
        r = subprocess.run(["git", "diff", "--", "."], cwd=repo,
                           capture_output=True, text=True, timeout=60)
    except Exception as e:
        raise RuntimeError(f"git diff failed: {type(e).__name__}: {e}")
    if r.returncode != 0:
        raise RuntimeError(
            f"git diff failed: {(r.stderr or '').strip()[:200]}")
    return patch_id_of_diff(r.stdout)


# ---------------------------------------------------------------------------
# Ledger entries
# ---------------------------------------------------------------------------

def new_entry(run_id, trigger, scope, command, patch, n_edits_at_run,
              exit_code, failed, counts, tail, turn, wall_seconds,
              artifact_id, format_fallback=False, error=None):
    """One test-ledger entry (§3). exit_code is int, -1 for
    timeout, or None for infra error (with error text, capped).
    failed is a list of parsed FAILED/ERROR node IDs (possibly
    empty for failing-unparsed runs). counts is {failed, passed}
    with None for unparseable totals (never invented)."""
    return {
        "run_id": run_id,
        "trigger": trigger,
        "scope": scope,
        "command": command,
        "patch_id": patch,
        "n_edits_at_run": n_edits_at_run,
        "exit_code": exit_code,
        "failed": list(failed or []),
        "counts": counts,
        "tail": (tail or "")[-TAIL_MAX_CHARS:],
        "turn": turn,
        "wall_seconds": wall_seconds,
        "artifact_id": artifact_id,
        "format_fallback": bool(format_fallback),
        "error": (str(error)[:200] if error is not None else None),
    }


def append_entry(ledger, entry):
    """Append-only append. Returns a flake note dict (§2 rule 8)
    when the new entry's green-ness differs from the latest prior
    entry on the same (scope, patch_id) with a known exit code,
    else None. Earlier entries are never mutated."""
    note = None
    if entry.get("exit_code") is not None:
        for prev in reversed(ledger):
            if prev.get("scope") == entry.get("scope") \
                    and prev.get("patch_id") == entry.get("patch_id") \
                    and prev.get("exit_code") is not None:
                if (prev["exit_code"] == 0) \
                        != (entry["exit_code"] == 0):
                    note = {
                        "patch_id": entry["patch_id"],
                        "scope": entry["scope"],
                        "prev_run_id": prev["run_id"],
                        "prev_exit": prev["exit_code"],
                        "run_id": entry["run_id"],
                        "new_exit": entry["exit_code"],
                        "turn": entry.get("turn"),
                    }
                break
    ledger.append(entry)
    return note


def latest_on_patch(ledger, scope, patch):
    """Latest entry on (scope, patch) with a KNOWN exit code
    (infra-error entries never satisfy a gate, so they are
    skipped here — the caller then falls through to a harness
    check). None when no usable entry exists."""
    for entry in reversed(ledger):
        if entry.get("scope") == scope \
                and entry.get("patch_id") == patch \
                and entry.get("exit_code") is not None:
            return entry
    return None


def latest_entry(ledger):
    """Latest entry overall (any patch, including error entries),
    or None when the ledger is empty."""
    return ledger[-1] if ledger else None


# ---------------------------------------------------------------------------
# Repro condition (§1a)
# ---------------------------------------------------------------------------

REPRO_TRIGGERS = ("precheck", "agent")
REPRO_WAIVER_KINDS = ("repro_infra", "repro_passed_no_bug")


def _repro_entry(ledger):
    """First ledger entry satisfying the repro shape (§1a):
    trigger precheck/agent, scope scoped, patch pristine, exit
    non-None and non-zero (parsed names, or failing-unparsed).
    None when no such entry exists."""
    for entry in ledger:
        if entry.get("trigger") in REPRO_TRIGGERS \
                and entry.get("scope") == "scoped" \
                and entry.get("patch_id") == "pristine" \
                and entry.get("exit_code") is not None \
                and entry["exit_code"] != 0:
            return entry
    return None


def repro_satisfied(ledger, waivers):
    """True when a recorded failing repro exists OR a repro
    waiver is recorded (§1a + §2 rules 1/2)."""
    if _repro_entry(ledger) is not None:
        return True
    for w in (waivers or []):
        if w.get("kind") in REPRO_WAIVER_KINDS:
            return True
    return False


def repro_outcome(ledger, waivers):
    """Repro evidence for reporting (§3 repro_outcome): the
    repro waiver kind when a waiver satisfies repro, else the
    repro entry's exit/failed, else None."""
    for w in (waivers or []):
        if w.get("kind") in REPRO_WAIVER_KINDS:
            return {"waiver": w["kind"]}
    entry = _repro_entry(ledger)
    if entry is not None:
        return {"exit_code": entry["exit_code"],
                "failed": list(entry["failed"])}
    return None


def repro_failed_set(ledger):
    """Node IDs failed in the repro run (first repro entry's
    failed list), or the empty set when no repro entry exists
    (e.g. repro satisfied only by waiver — then nothing can be
    proven pre-existing, and waiver evaluation fails closed)."""
    entry = _repro_entry(ledger)
    return set(entry["failed"]) if entry is not None else set()


# ---------------------------------------------------------------------------
# Dotted-to-node-ID conversion (MATCHING ONLY, §2 rule 4)
# ---------------------------------------------------------------------------

def _split_dotted(name):
    """Split a dotted name on '.' OUTSIDE [...] brackets, so
    parametrized ids like test_x[a.b] keep their parameter
    intact. Brackets are preserved in the returned parts."""
    parts = []
    depth = 0
    cur = []
    for ch in name:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        if ch == "." and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return parts


def _slash_outside_brackets(name):
    """True when name contains '/' outside [...] brackets.
    A '/' inside a parameter (e.g. test_x[/parameter-Query])
    is legitimate test-id syntax, not a path separator."""
    depth = 0
    for ch in name:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(0, depth - 1)
        elif ch == "/" and depth == 0:
            return True
    return False


def convert_dotted(name, repo):
    """Convert one parent-list name to a pytest node ID for
    set-matching ONLY. Returns the node ID, or None when it
    cannot be mapped (fail closed). Never returns a name that
    would be passed to pytest by this module — callers must
    not use the result as a CLI argument.

    Rules:
    - already a node ID ("::" present): returned unchanged when
      its file part exists under repo, else None.
    - dotted identifier: longest-prefix walk — for k from the
      full part count down, try "<prefix>.py"; the first
      existing file wins, remainder joined with "::". The
      design's walk is .py-only by its own wording: a
      package __init__.py does NOT satisfy it (falling back
      to an __init__ with a multi-segment remainder would
      invent a module segment that does not exist — e.g.
      pkg.nope.test_a must NOT map to pkg/__init__.py).
    - absolute paths, ".." components, empty parts, and
      path-like names (a '/' outside brackets) are rejected
      (None). A '/' inside brackets is parameter syntax, not
      a path separator, and is allowed.
    """
    if not name or not isinstance(name, str):
        return None
    if "::" in name:
        file_part = name.split("::", 1)[0]
        if not file_part or os.path.isabs(file_part):
            return None
        if ".." in file_part.split("/"):
            return None
        if os.path.isfile(os.path.join(repo, file_part)):
            return name
        return None
    if _slash_outside_brackets(name):
        return None
    parts = _split_dotted(name)
    if any(p in ("", ".", "..") for p in parts):
        return None
    for k in range(len(parts), 0, -1):
        prefix = "/".join(parts[:k])
        cand = prefix + ".py"
        if os.path.isfile(os.path.join(repo, cand)):
            rest = parts[k:]
            return cand + ("::" + "::".join(rest) if rest else "")
    return None


def _func_name(node_id):
    """Test-function name of a node ID or dotted name: last ::
    component for node IDs, last dot-component (bracket-aware)
    for dotted names, parameters stripped. Used only for the
    unmatched-shadow rule in is_to_fix."""
    if "::" in node_id:
        last = node_id.split("::")[-1]
    elif "." in node_id:
        last = _split_dotted(node_id)[-1]
    else:
        last = node_id
    return last.split("[")[0]


def build_to_fix(dotted_names, repo, repro_failed):
    """To-fix set construction (§2 rules 3/6). Returns a dict:
    - to_fix: set of converted node IDs (empty on fallback)
    - unmatched: dotted names that failed conversion
    - fallback_to_repro: True when the dotted list is empty
      (rule 3) or ALL names failed conversion (rule 6) — the
      caller then uses repro_failed as the to-fix set.
    - effective_to_fix: the set is_to_fix tests against
      (to_fix, or repro_failed on fallback).
    """
    dotted = list(dotted_names or [])
    if not dotted:
        return {"to_fix": set(), "unmatched": [],
                "fallback_to_repro": True,
                "effective_to_fix": set(repro_failed)}
    converted = set()
    unmatched = []
    for name in dotted:
        c = convert_dotted(name, repo)
        if c is not None:
            converted.add(c)
        else:
            unmatched.append(name)
    if len(unmatched) == len(dotted):
        return {"to_fix": set(), "unmatched": unmatched,
                "fallback_to_repro": True,
                "effective_to_fix": set(repro_failed)}
    return {"to_fix": converted, "unmatched": unmatched,
            "fallback_to_repro": False,
            "effective_to_fix": set(converted)}


def is_to_fix(failed_id, to_fix_info):
    """True when a failed node ID belongs to the to-fix set:
    exact converted match, OR (partial-conversion case only)
    the same test-function name as an unmatched dotted name
    (shadow rule — see module docstring). On fallback
    (empty/all-failed), membership is against the repro set."""
    effective = to_fix_info.get("effective_to_fix") or set()
    if failed_id in effective:
        return True
    if to_fix_info.get("fallback_to_repro"):
        return False
    fname = _func_name(failed_id)
    for raw in to_fix_info.get("unmatched") or []:
        if _func_name(raw) == fname:
            return True
    return False


# ---------------------------------------------------------------------------
# Finish-gate evaluation (§1b)
# ---------------------------------------------------------------------------

def evaluate_finish(ledger, waivers, patch, to_fix_info, repro_failed):
    """Pure finish-gate evaluation, in §1b order. Returns
    (decision, detail):
    - ("refuse_no_repro", {}): no repro and no waiver — finish
      is refused as an edit-gate failure, never evaluated
      further.
    - ("check_needed", {}): no usable entry on the current
      patch — the caller runs the harness finish check, then
      re-evaluates.
    - ("execute", {"entry": e}): latest usable entry on the
      current patch is green (exit 0).
    - ("waiver", {"entry": e, "waived": [...]}): latest entry
      fails ONLY on pre-existing-unrelated names (not in
      to-fix AND failed identically in repro) — finish
      executes WITH a recorded waiver.
    - ("refuse", {"entry": e, "failed": [...]}): latest entry
      fails on at least one non-waivable name (including the
      failing-unparsed case: zero parsed names).
    """
    if not repro_satisfied(ledger, waivers):
        return "refuse_no_repro", {}
    entry = latest_on_patch(ledger, "scoped", patch)
    if entry is None:
        return "check_needed", {}
    if entry["exit_code"] == 0:
        return "execute", {"entry": entry}
    failed = list(entry.get("failed") or [])
    if failed and all((not is_to_fix(f, to_fix_info))
                      and f in repro_failed for f in failed):
        return "waiver", {"entry": entry, "waived": failed}
    return "refuse", {"entry": entry, "failed": failed}


def finish_refusal_text(entry):
    """Refusal text for a non-green latest entry (§1b): failing
    names capped at 10 (+K more), full-log artifact id when the
    entry carries one."""
    failed = list(entry.get("failed") or [])
    n = len(failed)
    shown = failed[:FINISH_REFUSAL_MAX_NAMES]
    names = ", ".join(shown)
    if n > FINISH_REFUSAL_MAX_NAMES:
        names += f" +{n - FINISH_REFUSAL_MAX_NAMES} more"
    text = (f"FINISH_GATE: finish refused — module tests fail "
            f"on the current patch ({n} failed: {names}). "
            f"Fix the failures, then finish again; the harness "
            f"re-checks on your next finish attempt.")
    aid = entry.get("artifact_id")
    if aid:
        text += f" Full log: artifact {aid}."
    return text


def waiver_note_text(waived):
    """Finish-result note when finish executes with a
    pre-existing waiver (§1b step 4)."""
    return FINISH_WAIVER_NOTE_TEMPLATE.format(
        names=", ".join(waived))


# ---------------------------------------------------------------------------
# Gate-state snapshot for razor_status (§4.3)
# ---------------------------------------------------------------------------

def gate_snapshot(ledger, waivers, n_edits, patch, to_fix_info,
                  repro_failed):
    """Gates block for razor_status. patch may be None (git
    unavailable): then nothing is current-patch and finish_gate
    blocks with a patch-unavailable reason (never a silent
    pass). Phase derivation (documented interpretation — the
    design lists localize as a value, but precheck-reuse means
    arms start post-repro, so the pre-repro phase is reported
    as "repro"):
    - "repro": no repro recorded and no waiver
    - "finish": latest usable entry on the current patch green
    - "verify": edits exist but no green on the current patch
    - "edit": repro recorded, zero edits, no green yet
    latest_module is the latest entry overall (any patch,
    including error entries) with patch_current flag, or None
    when the ledger is empty."""
    repro = repro_satisfied(ledger, waivers)
    if patch is None:
        current = None
    else:
        current = latest_on_patch(ledger, "scoped", patch)
    if not repro:
        phase = "repro"
    elif current is not None and current["exit_code"] == 0:
        phase = "finish"
    elif n_edits > 0:
        phase = "verify"
    else:
        phase = "edit"
    last = latest_entry(ledger)
    if last is None:
        latest_module = None
    else:
        latest_module = {
            "exit_code": last.get("exit_code"),
            "failed": len(last.get("failed") or []),
            "patch_current": (patch is not None
                              and last.get("patch_id") == patch),
        }
    if not repro:
        finish_gate = {"state": "block",
                       "reason": "no recorded failing repro"}
    elif patch is None:
        finish_gate = {"state": "block",
                       "reason": "patch unavailable"}
    elif current is None:
        finish_gate = {"state": "block",
                       "reason": "no test run on current patch "
                                 "(harness will check on finish)"}
    elif current["exit_code"] == 0:
        finish_gate = {"state": "pass",
                       "reason": "green on current patch"}
    else:
        decision, detail = evaluate_finish(
            ledger, waivers, patch, to_fix_info, repro_failed)
        if decision == "waiver":
            finish_gate = {"state": "pass",
                           "reason": "pre-existing failures only "
                                     "(waivable)"}
        else:
            finish_gate = {
                "state": "block",
                "reason": (f"{len(detail.get('failed') or [])} "
                           f"failing on current patch")}
    return {"phase": phase,
            "repro_recorded": repro,
            "latest_module": latest_module,
            "finish_gate": finish_gate}


# ---------------------------------------------------------------------------
# Loop/driver shared context object
# ---------------------------------------------------------------------------

class PhaseGateContext:
    """Mutable gate state shared between the driver and the
    loop (one per arm). to_fix_info is REQUIRED (from
    build_to_fix): there is no neutral default — an unknown
    to-fix set would make waiver evaluation fail open, so the
    caller must always supply the real one. The loop appends
    agent test entries and updates n_edits; the driver's
    finish_check_fn appends harness entries; the driver's
    status wrapper reads snapshot() for razor_status.
    finish_check_fn, when set, is called as fn(patch_id,
    n_edits_at_run, turn, wall_seconds) and MUST append its
    entry to self.ledger itself (via append_entry, collecting
    flake notes) and return the entry; infra-error entries
    carry exit_code None + error text. run_id assignment:
    next_run_id() (max + 1)."""

    def __init__(self, to_fix_info, ledger=None, waivers=None,
                 repro_failed=None, test_command=None,
                 repro_summary=None, finish_check_fn=None,
                 lint_enabled=True):
        self.ledger = list(ledger) if ledger else []
        self.waivers = list(waivers) if waivers else []
        self.to_fix_info = to_fix_info
        self.repro_failed = set(repro_failed or [])
        self.test_command = test_command
        self.repro_summary = repro_summary
        self.finish_check_fn = finish_check_fn
        self.lint_enabled = lint_enabled
        self.n_edits = 0
        self.harness_finish_check_runs = 0
        self.flake_notes = []

    def next_run_id(self):
        if not self.ledger:
            return 1
        return max(e.get("run_id", 0) for e in self.ledger) + 1

    def append(self, entry):
        note = append_entry(self.ledger, entry)
        if note is not None:
            self.flake_notes.append(note)
        return note

    def snapshot(self, repo):
        """Gates dict for razor_status (§4.3). A git failure
        yields patch None (blocked, never passed)."""
        try:
            patch = patch_id(repo)
        except Exception:
            patch = None
        return gate_snapshot(self.ledger, self.waivers,
                             self.n_edits, patch, self.to_fix_info,
                             self.repro_failed)

    def snapshot_with_patch(self, patch):
        return gate_snapshot(self.ledger, self.waivers,
                             self.n_edits, patch, self.to_fix_info,
                             self.repro_failed)
