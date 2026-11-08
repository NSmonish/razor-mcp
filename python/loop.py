#!/usr/bin/env python3
"""The owned agent loop (Option C: Razor owns the loop).

assemble context -> call model -> execute tool -> append result -> repeat.
Identical run parameters to the paid baseline: temp 0.0, 30-step budget,
same system prompt, same naive tool set, full history replay every call
(that quadratic replay is what the naive harness is supposed to measure).

Differences from baseline/plain_agent.py (all deliberate, all for the
harness role): the model is injectable (stub / free-lane / paid backends
share this loop, so later ablations are paired); the loop returns a
result dict instead of writing files (the driver owns checkpointing);
telemetry adds turns, tool mix, and stop reason; an overall arm deadline
guards pathological runs; and a paid cost guard can abort an arm before
a model call (used by the paid driver to guarantee the $1.00 ceiling).
On an unexpected crash the loop returns a partial result (tokens counted
so far) instead of losing the arm's spend — an improvement over the
baseline, where a crashed arm recorded $0.

Step 3 additions (opt-in only; when the new parameters are absent the
loop behaves exactly as in Steps 1-2):
- History masking: when mask_recent_n is set, each model call receives a
  masked copy of the history (see masking.py) — the stored history is
  never mutated, and every stub carries a retrievable artifact id.
- Step-allocation guard: when guard_tools / guard_start_step are set,
  read tools at or past guard_start_step are refused by the harness
  (guard message, trigger counted, not counted in tool_mix) so the
  final steps stay reserved for edit/test/finish.

Step 4 additions (opt-in only; when state_config is absent the loop
behaves exactly as in Steps 1-3):
- State file: when state_config is set, each model call receives
  [system, task, state message] + the last window_turns complete turns
  verbatim (see state.py), INSTEAD of the full (masked) transcript.
  The state is built deterministically by the harness (never by the
  model) and validated every turn; ANY validation failure triggers a
  sticky fallback to the Step 3 masked view for the rest of the arm.
- Loop guard: when loop_guard is set, deterministic repetition is
  refused: L1 = same tool+args as the previous two EXECUTED tool
  calls; L2 = same test outcome as the previous two test runs with no
  edit since the earlier of the two. Refusals consume a step, are
  counted in loop_guard_triggers (separate from allocation
  guard_triggers), and are NOT counted in tool_mix. Check order:
  allocation guard first (preserves Step 3 counts), then loop guard.

Step 4 repair (F1-F6, approved; see PROGRESS-step4-fix.md Step 2):
- F1: WINDOW_TURNS 3 -> 6 (state.py constant; the driver passes it
  through window_turns, so the loop needs no window change).
- F2: in state mode, EVERY maskable tool result gets a hist- id at
  execution time (via mask_sink) when it has no tool artifact id of
  its own, and findings carry that id — restoring Step 3's
  lossless-by-reference property inside state mode. Step 3 masked
  mode is untouched (lazy minting in masked_view, as before).
- F3: the loop tracks most-recent-read-per-file excerpts and the
  last non-empty assistant text and passes them to build_state.
- F4: edit_file failures are recorded (last_edit + failed_edits)
  and _event_for no longer mislabels them as "edited".
- F5 (EXPLICIT CONFIG CHANGE): guard refusals (allocation + loop)
  NO LONGER consume the 30 productive-step budget. A separate
  refusal budget (REFUSAL_BUDGET = 10/arm) counts them; exhausting
  it stops the arm with stop_reason "refusal_budget". Consequences
  to state wherever steps are compared: `steps` is now productive
  steps, read_op_share's denominator is productive steps, and the
  allocation guard's step-23 threshold counts productive steps.
- F6 (telemetry only): loop triggers split into L1/L2 counts;
  failed-edit counts and reasons logged in the result.

Step 3 + test-after-edit enforcement (opt-in only; when
test_after_edit is absent/False the loop behaves exactly as before):
- An edit_file call may not follow another executed edit_file call
  with no intervening executed razor_run_tests call, and finish may
  not follow an executed edit_file call with no intervening executed
  razor_run_tests call. Refusals follow the F5 pattern (tool result,
  no productive step, counted in refusals against REFUSAL_BUDGET,
  not in tool_mix) and are counted separately in
  test_after_edit_refusals (finish refusals additionally in
  test_after_edit_finish_refusals). Only an executed test run clears
  the pending-edit flag; reads, searches, and refusals do not.
  Check order: allocation guard first (preserves Step 3 counts),
  then enforcement, then loop guard.

Step 3 + read-budget (opt-in only; when read_budget is absent/None
the loop behaves exactly as before):
- A read-budget cap: when read_budget is set, read tools
  (razor_search, razor_outline, razor_read, razor_read_artifact —
  the same set as Step 3's allocation guard) are refused once the
  arm has EXECUTED read_budget read ops. Refused reads do NOT
  increment the count (only executed reads count) and follow the
  F5 pattern (tool result, no productive step, counted in refusals
  against REFUSAL_BUDGET, not in tool_mix), counted separately in
  read_budget_refusals. Check order: allocation guard first
  (preserves Step 3 counts), then read-budget, then enforcement,
  then loop guard.

Step 3 + harness auto-test (opt-in only; when auto_test_fn is
absent/None the loop behaves exactly as before):
- Harness-driven verification: after an assistant message that
  executed a SUCCESSFUL edit_file call (result starts with "OK"
  per the F4 convention) with no executed razor_run_tests call
  after the last such edit IN THAT MESSAGE, the harness runs the
  issue's failing tests itself (via the driver-supplied
  auto_test_fn) and feeds back a failures-only summary. The
  summary is delivered as a USER message (provider invariants: a
  tool message needs a tool_call_id referencing a prior
  assistant tool_call, and auto-test has none; the loop already
  uses user messages for non-tool-result notes).
- Fixed-signal format (proposal §2b, REPLACING the original
  1000-char names-only cap): per failure, in order — resolved
  test id, failing assertion expected-vs-actual, traceback tail
  (last repo frames) — plus a one-line summary (counts, exit
  code) and a delta vs the previous auto-test run (newly
  passing / still failing / newly failing; omitted on the first
  run). Failures sharing a traceback signature are grouped with
  a count. Budgets: per failure ~800 tokens, total ~3000 tokens
  (chars/4 approximation); truncation is head+tail with explicit
  markers, never head-only, and the summary + delta lines are
  never truncated away. Never invents ids, assertions, or tails.
- No-progress cutoff (proposal §2c): auto-test stops FIRING
  after 3 consecutive informative (fail/pass) runs with zero
  newly-passing tests; the arm continues and manual testing
  remains available. The first run never counts; error/skipped
  runs are neutral (they neither advance nor reset the streak).
- Signal validity (proposal §3, computed per run): INVALID iff
  runner/infra error, exit code 4, or a failing outcome with
  zero resolved ids. The ablation-level validity gate (invalid
  <= 10% overall AND no arm > 50% invalid) is evaluated at
  REPORT time by the driver, never inside the loop.
- Firing rule (exactly once per message, after the tool-call
  loop): fires iff the message executed >= 1 successful edit
  with no executed manual test after the last such edit in that
  message. Failed edits never trigger (no new code to verify);
  reads, searches, manual tests, and finish never trigger. A
  manual test in the same message after the edit suppresses it
  (verification already happened); a multi-edit message gets ONE
  auto-test on the final patch state. It does NOT fire when the
  message finished the arm (no next call would read the summary),
  hit the step budget, or exhausted the refusal budget.
- Step accounting (the point of the lever): the auto-test runs
  INSIDE the edit's turn and consumes NO productive step, is NOT
  in tool_mix, does NOT count toward n_test_runs (the agent's own
  test metric), does NOT consume refusal budget, and does NOT
  move the allocation-guard threshold (which counts productive
  steps). It DOES cost tokens (the capped summary, replayed
  every subsequent call) and wall time (the test subprocess,
  capped by the driver's timeout). It does NOT clear
  edit_unverified: enforcement, when on, still demands an
  agent-run test (auto-test is harness verification, not agent
  testing). It does NOT trigger loop guard L2: auto-test runs
  are never appended to test_runs, and by construction every
  auto-test follows an edit, so L2's "no edit since" clause
  could not hold anyway.
- Masking: auto-test summaries are NEVER masked (maskable False),
  like edits and guard refusals. Verification-critical by design
  (masking would reintroduce the information loss the lever
  removes, precisely on long arms); bounded cost (cap x ~2-4
  edits/arm is small vs arm prompt totals); and it keeps masking
  accounting clean (summaries do not inflate masked_chars_saved
  or re-read economics, so Step 3 comparisons stay
  apples-to-apples). Summaries ARE tracked separately as
  auto_test_chars in anatomy (fresh content, subtracted from
  replay in telemetry.anatomy) so replay estimates stay honest.
- Failure modes: an auto-test infrastructure error (runner
  crash, timeout, non-dict return) degrades to an "auto-test
  error" note and the arm proceeds; manual testing remains
  available. A syntax/import error from the edit itself is a
  NORMAL failure summary (the test runner ran and reported it),
  not an infra error. Auto-test is independent of the
  enforcement and read-budget flags (it touches none of their
  counters); in the auto-test config both are OFF.
- Telemetry: auto_test_runs (count), auto_test_outcomes (one
  {outcome, exit_code, failed, fallback, valid,
  assertion_present, delta} per run: pass/fail/error/skipped),
  auto_test_summary_chars (total summary chars),
  auto_test_cutoff_fired / auto_test_cutoff_run /
  auto_test_no_progress_streak. The future ablation measures
  verification as (agent tests + auto tests) vs Step 3's agent
  tests alone, plus the proposal §3 validity/fallback/
  assertion rates from outcomes.
- Known limitation: an edit immediately followed by finish in
  the SAME message gets no auto-test (the arm ends; no call
  would read the summary). Auto-test guarantees verification
  feedback for edits the agent continues from, not for edits
  it finishes on without testing.

Step 3 + PAUSE-style pre-first-edit plan (opt-in only; when
plan_fn is absent/None the loop behaves exactly as before):
- Intercept: before the FIRST edit_file call EXECUTES (refusals
  `continue` before the hook, so a refused edit never triggers
  the planner), the loop asks the driver-supplied plan_fn for ONE
  repair plan from the pre-edit transcript — all messages before
  the current assistant message, plus any tool results already
  appended during the current message (they precede the edit in
  execution order); the current assistant message itself (the
  edit decision) is excluded. The plan is appended to history
  BEFORE the edit executes, so it is present for every later
  call; the first edit itself was already decided, so the plan
  guides subsequent turns (PAUSE resumes the same executor
  rather than vetoing the edit). A failed first edit still gets
  a plan: the hook is pre-execution, so the plan exists before
  any edit attempt, successful or not. The planner fires at most
  once per arm.
- Plan delivery: the plan text is appended ONCE as a user
  message with meta maskable False (never masked, like edits
  and guard refusals). It is NOT re-injected every call: a
  never-masked message is already replayed verbatim in every
  subsequent call's history, so separate re-injection would
  duplicate tokens for zero information.
- Step accounting: the planner call consumes NO productive step,
  no tool_mix entry, and no executor model call (it is a separate
  backend call, not via the executor backend). It DOES cost
  tokens (planner input + output, recorded in telemetry) and wall
  time (bounded by the driver's backend timeout).
- Advisory, never a gate: the plan does NOT refuse edits outside
  planned sites and does NOT refuse finish. At arm end, a
  string/diff-based finish checklist (no model call) compares
  the final diff against planned sites and the last executed
  test run against planned failing tests; the outcome is recorded
  in telemetry and finishing always proceeds. LIMITATION, STATED
  PLAINLY: the agent does not see the checklist before finishing
  (finish ends the arm), so in this lever the checklist is
  measurement for the ablation (plan adherence + coverage), not
  intervention. A confirm-turn variant (finish -> checklist note
  -> agent gets another turn -> second finish always succeeds)
  was rejected for this lever: it costs an extra model call per
  arm and changes finish semantics; measure first, intervene
  later only if the measurement earns it.
- Failure modes: a planner exception, error dict, empty text, or
  a plan with zero parseable sites degrades to Step 3 as-is
  (flagged in telemetry); the arm never crashes for planner
  reasons. A planner that lists wrong sites is harmless: the
  executor may deviate freely (advisory only). Transcript over
  the driver's cap is truncated per the driver's rule, never a
  reason to fail the arm.
- Telemetry: planner_runs, planner_model, planner input/output
  tokens, plan_parse_ok, plan_sites, plan_tests, finish_checklist.
  The future ablation measures whether the plan was USED (sites
  touched, tests covered, edits outside the plan), not just
  whether solves moved.
"""
import ast
import json as _json
import os
import re
import subprocess
import time

from masking import MASKABLE_TOOLS, masked_view
from phase_gates import (EDIT_GATE_REFUSAL,  # noqa: E402
                         FINISH_CHECK_INFRA_TEMPLATE, append_entry,
                         evaluate_finish, finish_refusal_text,
                         new_entry, patch_id, repro_outcome,
                         repro_satisfied, waiver_note_text)
from telemetry import aggregate_calls, classify_call, parse_usage
from state import (WINDOW_TURNS, build_state, recent_window,
                   state_message, validate_state)
from tools import (GEMINI_TOOL_PROTOCOL, SYSTEM, TOOLS, parse_tool_block,
                   run_tool)

MAX_STEPS = 30
# Step 4 repair F5: separate refusal budget (guard refusals no longer
# consume productive steps; exhausting this stops the arm with
# stop_reason "refusal_budget", guaranteeing termination).
REFUSAL_BUDGET = 10
# Step 3 + read-budget: the read tools counted against the budget
# (identical to Step 3's allocation-guard set by construction, so
# the budget and the guard count the same operations).
READ_BUDGET_TOOLS = {"razor_search", "razor_outline", "razor_read",
                     "razor_read_artifact"}
CALL_TIMEOUT_S = 1200  # fail-fast: under the 30-min stuck-call cap
# Step 3 + harness auto-test (fixed-signal re-run, proposal §2b):
# the summary fed back after a successful edit is a RICH failure
# report, not a names-only note. Token budget uses the codebase's
# chars/4 approximation (documented as approximate, not exact
# tokenization): per failure ~800 tokens (~3200 chars), total
# ~3000 tokens (~12000 chars). The old 1000-char names-only cap is
# REPLACED by these budgets — a names-only summary cannot carry
# the assertion + traceback tail + delta that make feedback
# actionable, and the original arm's failure was unactionable
# signal, not too much signal. Truncation is head+tail (never
# head-only: head-only truncation loses the failing assertion,
# which sits mid-output), with explicit markers; the summary line
# and delta line are never truncated away. AUTO_TEST_TAIL_LINES is
# kept for the legacy-dict path (driver fns that return only
# tail_lines, e.g. scripted test fns); rich driver results carry
# per-failure sections instead.
AUTO_TEST_PER_FAILURE_MAX_CHARS = 3200  # ~800 tokens
AUTO_TEST_SUMMARY_MAX_CHARS = 12000  # ~3000 tokens total
AUTO_TEST_TAIL_LINES = 10
# Step 3 + PAUSE-style pre-first-edit plan: prefix for the plan
# message appended to history (never masked). The advisory status
# is explicit to the executor: the plan guides, it never restricts.
PLAN_MESSAGE_PREFIX = ("REPAIR PLAN (advisory — you may deviate from "
                       "it; finishing is never refused for deviating):\n")


def _head_tail_truncate(text, budget):
    """Truncate `text` to at most `budget` chars, head+tail, NEVER
    head-only: keep the first half and the last half, joined by an
    explicit marker line. Cut points are moved to the nearest
    newlines so no line is ever split (a split assertion line is
    worse than a dropped one). If the text has no usable newlines
    in range, fall back to a plain char split with the marker.
    Returns text unchanged when it already fits."""
    if len(text) <= budget:
        return text
    marker = "\n...[truncated]...\n"
    half = (budget - len(marker)) // 2
    if half <= 0:
        return text[:budget - 20] + "...[truncated]"
    head = text[:half]
    nl = head.rfind("\n")
    if nl > 0:
        head = head[:nl]
    tail = text[len(text) - half:]
    nl = tail.find("\n")
    if nl >= 0:
        tail = tail[nl + 1:]
    out = head + marker + tail
    if len(out) > budget:  # newline adjustment overshot; char-cut
        out = (out[:budget - 20] + "...[truncated]")
    return out


def _auto_test_validity(outcome, exit_code, failed):
    """Per-run signal validity (proposal §3, exhaustive criteria):
    INVALID iff (i) runner/infra error, or (ii) exit code 4 (pytest
    usage error — the original arm's failure shape), or (iii) a
    failing outcome with zero resolved test ids (a "still failing"
    with no names is not feedback). Skipped runs meet none of the
    three criteria and are valid by definition (they carry no
    signal, but they are not degraded signal). Pass runs are valid
    even with an empty failed list (nothing failed)."""
    if outcome == "error":
        return False
    if exit_code == 4:
        return False
    if outcome == "fail" and not failed:
        return False
    return True


def _auto_test_group_failures(failures):
    """Group failure dicts by traceback signature (same signature =
    one group, count attached, representative first). Failures
    without a signature form singleton groups. Order is first-seen
    order; grouping never reorders across signatures and never
    drops an id (every member id is listed)."""
    groups = []
    index = {}
    for f in failures:
        sig = f.get("signature") or ""
        if sig and sig in index:
            groups[index[sig]]["members"].append(f)
        else:
            g = {"signature": sig, "members": [f]}
            if sig:
                index[sig] = len(groups)
            groups.append(g)
    return groups


def _auto_test_failure_text(f):
    """One failure's rendered detail: id, assertion, traceback
    tail — in that order, each only when present. Nothing here is
    invented: every id/assertion/tail comes from runner output
    parsed by the driver; an unparseable failure renders its id
    alone (honest, and assertion_present stays False for it)."""
    parts = [f.get("test_id", "")]
    assertion = (f.get("assertion") or "").strip()
    if assertion:
        parts.append(f"Assertion: {assertion}")
    tail = (f.get("traceback_tail") or "").strip()
    if tail:
        parts.append(f"Traceback tail:\n{tail}")
    return "\n".join(parts)


def _auto_test_delta_line(delta):
    """Delta line vs the previous auto-test run (None on the first
    run in an arm: no delta exists yet, so no line). Rendered only
    when at least one of the three lists is non-empty; an all-empty
    delta (e.g. pass after pass) carries no information and is
    omitted rather than noise."""
    if delta is None:
        return ""
    newly = list(delta.get("newly_passing") or [])
    still = list(delta.get("still_failing") or [])
    new = list(delta.get("newly_failing") or [])
    if not newly and not still and not new:
        return ""
    def fmt(name, xs):
        return f"{name}: {', '.join(xs)}" if xs else f"{name}: none"
    return ("Delta: " + fmt("newly passing", newly) + "; "
            + fmt("still failing", still) + "; "
            + fmt("newly failing", new) + ".")


def _format_auto_test_summary(at, delta=None, label="AUTO-TEST"):
    """Fixed-signal auto-test summary (proposal §2b). Layout, in
    order: [scoped-files fallback] prefix when the run covered the
    scoped files; one-line summary (counts X failed / Y passed,
    exit code); delta line vs the previous auto-test run (omitted
    on the first run); per-failure detail (id, assertion
    expected-vs-actual, traceback tail), grouped by traceback
    signature with a count. Budgets: per failure ~800 tokens
    (AUTO_TEST_PER_FAILURE_MAX_CHARS), total ~3000 tokens
    (AUTO_TEST_SUMMARY_MAX_CHARS); truncation is head+tail with
    explicit markers, and the summary + delta lines are never
    truncated away (only the failure detail is). `at` is the
    driver's result dict: {exit_code, failed_names, failures
    [{test_id, assertion, traceback_tail, signature}], counts
    {failed, passed}, tail_lines, n_failing} or {error} /
    {skipped}; legacy dicts without "failures" render names +
    tail lines under the same total cap. An optional fallback=True
    adds the prefix FIRST so truncation cannot drop it: the agent
    must know this feedback covered the scoped files, not just
    the failing list. Never invents names, assertions, or tails.
    `label` renames the summary header (default "AUTO-TEST",
    which preserves every existing caller byte-identically;
    the Phase 1 repro summary passes label="REPRO" — same
    formatter, same budgets, different run kind)."""
    err = at.get("error")
    if err:
        return (f"{label} error: {err}; continue with manual "
                f"razor_run_tests if needed.")
    if at.get("skipped"):
        return f"{label} skipped: no failing tests listed."
    prefix = "[scoped-files fallback] " if at.get("fallback") else ""
    rc = at.get("exit_code")
    counts = at.get("counts") or {}
    n_failed = counts.get("failed")
    if n_failed is None:
        n_failed = len(at.get("failed_names") or [])
    n_passed = counts.get("passed")
    if n_passed is None and not at.get("fallback"):
        n = at.get("n_failing", 0)
        n_passed = max(0, n - n_failed) if n else None
    if rc == 0:
        if n_passed is None:
            line = f"{label}: exit 0 — 0 failed."
        else:
            line = (f"{label}: exit 0 — 0 failed / "
                    f"{n_passed} passed.")
        dline = _auto_test_delta_line(delta)
        text = prefix + line + ("\n" + dline if dline else "")
        return text
    if n_passed is None:
        line = f"{label}: exit {rc} — {n_failed} failed."
    else:
        line = (f"{label}: exit {rc} — {n_failed} failed / "
                f"{n_passed} passed.")
    dline = _auto_test_delta_line(delta)
    head = prefix + line + ("\n" + dline if dline else "")
    names = list(at.get("failed_names") or [])
    failures = at.get("failures")
    if not names:
        # Non-zero exit but no FAILED lines (e.g. pytest exit 4
        # usage error, or a collection error in a format the parser
        # did not recognize): the tail lines carry the signal;
        # never invent names. This is also the validity-invalid
        # shape (iii) — the driver/loop flags it. (Covers both the
        # legacy dicts without "failures" and rich parses with an
        # empty failures list, e.g. exit 4.)
        tail = list(at.get("tail_lines") or [])[-AUTO_TEST_TAIL_LINES:]
        detail = "\n".join(tail)
        if detail:
            detail = ("Failing tests did not pass "
                      "(no FAILED lines in output).\n" + detail)
        else:
            detail = ("Failing tests did not pass "
                      "(no FAILED lines in output).")
        detail = _head_tail_truncate(
            detail, AUTO_TEST_SUMMARY_MAX_CHARS - len(head) - 1)
        return head + "\n" + detail
    if not failures:
        # Legacy path: driver fns returning only names + tail
        # lines (e.g. scripted test fns), or an inconsistent rich
        # dict with names but no parsed failures. Names first,
        # then the shared tail, under the total cap.
        tail = list(at.get("tail_lines") or [])[-AUTO_TEST_TAIL_LINES:]
        detail = ", ".join(names)
        if tail:
            detail += "\n" + "\n".join(tail)
        detail = _head_tail_truncate(
            detail, AUTO_TEST_SUMMARY_MAX_CHARS - len(head) - 1)
        return head + "\n" + detail
    groups = _auto_test_group_failures(failures)
    blocks = []
    for g in groups:
        members = g["members"]
        rep = members[0]
        block = _auto_test_failure_text(rep)
        block = _head_tail_truncate(
            block, AUTO_TEST_PER_FAILURE_MAX_CHARS)
        if len(members) > 1:
            others = ", ".join(m.get("test_id", "")
                                for m in members[1:])
            block = (f"FAILURE (x{len(members)} same-signature): "
                     + block + "\nSame-signature failures: " + others)
        else:
            block = "FAILURE: " + block
        blocks.append(block)
    detail = "\n\n".join(blocks)
    detail = _head_tail_truncate(
        detail, AUTO_TEST_SUMMARY_MAX_CHARS - len(head) - 1)
    return head + "\n" + detail


def _finish_checklist(repo, plan_sites, plan_tests, test_runs):
    """Advisory-only finish checklist (no model call, never a
    refusal): compares the final diff against the planned sites
    and the last executed test run against the planned failing
    tests. Site match: exact repo-relative path, or either side as
    a path suffix of the other (the planner may write a partial or
    repo-prefixed path). Test coverage: covered iff the latest
    executed test run exited 0 — scoped green implies the failing
    tests pass, because the failing tests live in the scoped files
    by eval construction; any other outcome leaves the test
    uncovered (it may not have run at all, and the checklist never
    claims otherwise). Returns a dict; unknown=True when the diff
    is unavailable. Computed at arm end (see run_loop): the agent
    never acts on it in the current arm, so it is measurement for
    the ablation, not intervention."""
    try:
        d = subprocess.run(["git", "diff", "--name-only"], cwd=repo,
                           capture_output=True, text=True, timeout=60)
        if d.returncode != 0:
            raise RuntimeError("git diff --name-only failed")
        touched = {ln.strip() for ln in d.stdout.splitlines()
                   if ln.strip()}
    except Exception:
        return {"sites_touched": 0,
                "sites_total": len(plan_sites),
                "sites_untouched": [s.get("file", "")
                                    for s in plan_sites],
                "tests_covered": [], "tests_total": len(plan_tests),
                "tests_uncovered": list(plan_tests),
                "passed": False, "unknown": True}

    def _touched(site_file):
        if not site_file:
            return False
        for name in touched:
            if (name == site_file
                    or name.endswith("/" + site_file)
                    or site_file.endswith("/" + name)):
                return True
        return False

    untouched = [s.get("file", "") for s in plan_sites
                 if not _touched(s.get("file", ""))]
    last_ok = bool(test_runs) and test_runs[-1][0] == 0
    covered = list(plan_tests) if last_ok else []
    uncovered = [] if last_ok else list(plan_tests)
    return {"sites_touched": len(plan_sites) - len(untouched),
            "sites_total": len(plan_sites),
            "sites_untouched": untouched,
            "tests_covered": covered,
            "tests_total": len(plan_tests),
            "tests_uncovered": uncovered,
            "passed": (not untouched and not uncovered),
            "unknown": False}


def _default_diff_fn(repo):
    """Live (git diff, git diff --stat) for the repo. Raises on failure
    (a failed diff must trigger fallback, never silent stale state)."""
    d = subprocess.run(["git", "diff", "--", "."], cwd=repo,
                       capture_output=True, text=True, timeout=60)
    s = subprocess.run(["git", "diff", "--stat", "--", "."], cwd=repo,
                       capture_output=True, text=True, timeout=60)
    if d.returncode != 0 or s.returncode != 0:
        raise RuntimeError("git diff failed")
    return d.stdout, s.stdout


def _parse_failed_names(failures_inline):
    """Test ids from FAILED/ERROR lines, in order, deduped."""
    names = []
    for line in (failures_inline or "").splitlines():
        m = re.match(r"^(FAILED|ERROR)\s+(\S+)", line.strip())
        if m and m.group(2) not in names:
            names.append(m.group(2))
    return names


def _lint_snapshot(repo, path):
    """Pre-edit lint snapshot for one edit_file call (Phase 1,
    design §4.1). Returns {"skip": reason|None, "old": bytes|None,
    "full": path|None}. Skip reasons: "non_python" (§2 rule 10),
    "preexisting" (§2 rule 9: the file does not parse BEFORE the
    edit), "unreadable" (cannot be read — the edit itself will
    fail, or the file cannot be reverted safely, so lint stands
    down rather than false-rejecting). The naive edit tool never
    creates files, so "old" is bytes on every path that can reach
    a successful edit; the None case is defensive only."""
    raw = path or ""
    p = os.path.normpath(raw)
    if not p.endswith(".py"):
        return {"skip": "non_python", "old": None, "full": None}
    if p.startswith("..") or os.path.isabs(raw):
        return {"skip": "unreadable", "old": None, "full": None}
    full = os.path.join(repo, p)
    try:
        with open(full, "rb") as f:
            old = f.read()
    except Exception:
        return {"skip": "unreadable", "old": None, "full": full}
    try:
        ast.parse(old)
    except SyntaxError:
        return {"skip": "preexisting", "old": old, "full": full}
    return {"skip": None, "old": old, "full": full}


def _event_for(fn, args, out, kind, turn):
    """Compact deterministic trajectory event for the state file, or
    None for calls that carry no finding (status, finish, errors)."""
    if fn in ("razor_status", "finish"):
        return None
    try:
        obj = _json.loads(out) if isinstance(out, str) else None
    except Exception:
        obj = None
    if fn == "razor_search" and isinstance(obj, dict):
        detail = (f"query={obj.get('query', args.get('query', ''))} "
                  f"hits={obj.get('total_hits')}")
        return {"turn": turn, "kind": "search", "detail": detail,
                "path": None}
    if fn == "razor_outline" and isinstance(obj, dict):
        detail = (f"path={args.get('path', '')} "
                  f"symbols={obj.get('total_symbols')}")
        return {"turn": turn, "kind": "outline", "detail": detail,
                "path": args.get("path") or None}
    if fn == "razor_read" and isinstance(obj, dict):
        detail = (f"path={obj.get('path', args.get('path', ''))} "
                  f"lines={obj.get('start_line')}-"
                  f"{obj.get('end_line')}")
        return {"turn": turn, "kind": "read", "detail": detail,
                "path": obj.get("path", args.get("path"))}
    if fn == "razor_read_artifact" and isinstance(obj, dict):
        detail = (f"artifact={obj.get('artifact_id')} "
                  f"chars={obj.get('total_chars')}")
        return {"turn": turn, "kind": "artifact_read", "detail": detail,
                "path": None}
    if fn == "razor_run_tests" and isinstance(obj, dict):
        failed = _parse_failed_names(obj.get("failures_inline", ""))
        detail = (f"exit={obj.get('exit_code')} "
                  f"failed={failed[:3]}"
                  + (f"+{len(failed) - 3}" if len(failed) > 3 else ""))
        return {"turn": turn, "kind": "test", "detail": detail,
                "path": None}
    if fn == "edit_file":
        # Step 4 repair F4: a REJECTED edit must never be recorded
        # as "edited" (Step 4's _event_for mapped every edit_file
        # call to "edited {path}", so the state told the agent it
        # had edited a file whose patch was empty). Success is
        # exactly out.startswith("OK") — the edit tool returns
        # "OK: edit applied" on success and "ERROR: ..." otherwise
        # (kind "other" on failure). The failure's error is carried
        # verbatim in detail (it fits MAX_DETAIL_CHARS: these errors
        # are one-liners) and in the state's last_edit field.
        if isinstance(out, str) and out.startswith("OK"):
            return {"turn": turn, "kind": "edit",
                    "detail": f"edited {args.get('path', '')}",
                    "path": args.get("path") or None}
        return {"turn": turn, "kind": "edit_failed",
                "detail": out if isinstance(out, str) else "",
                "path": args.get("path") or None}
    if isinstance(obj, dict) and obj.get("error"):
        return None
    return {"turn": turn, "kind": "other",
            "detail": (out[:120] if isinstance(out, str) else ""),
            "path": None}


def _tool_artifact_id(out):
    """Artifact id embedded in a JSON tool result, or None."""
    try:
        obj = _json.loads(out)
    except Exception:
        return None
    if isinstance(obj, dict):
        aid = obj.get("artifact_id")
        return aid if isinstance(aid, str) and aid else None
    return None


def run_loop(repo, user_prompt, backend, lane, model,
             system_extra="", max_steps=MAX_STEPS, temperature=0.0,
             deadline_s=5400, cost_guard=None,
             tool_defs=None, tool_runner=None, system_text=None,
             mask_recent_n=None, mask_sink=None,
             guard_tools=None, guard_start_step=None,
             state_config=None, loop_guard=False,
             test_after_edit=False, read_budget=None,
             auto_test_fn=None, plan_fn=None, phase_gates=None):
    """Run one arm. cost_guard(prompt_so_far, completion_so_far) returns
    None to proceed or a stop_reason string to abort before the next call
    (paid mode only). Returns a result dict (same keys as the baseline
    plain-agent result, plus turns, tool_mix, stop_reason, lane, model,
    crashed). Optional tool_defs/tool_runner/system_text override the
    naive toolset (Step 2 ablation: Razor tools as the only read path);
    defaults preserve Step 1 behavior exactly. Tool calls naming a tool
    outside the allowed set are recorded in bypass_attempts and answered
    with an error (the runner is never called for them).
    Step 3 options: mask_recent_n (keep this many recent maskable tool
    results verbatim, mask older ones — see masking.py; mask_sink is
    called lazily as sink(tool, content) -> artifact_id for results
    without their own artifact id) and guard_tools/guard_start_step
    (read tools at or past guard_start_step are refused by the harness
    so the final steps stay reserved for edit/test/finish).
    Step 4 options: state_config (dict with frozen {goal,
    failing_tests, constraints} verbatim task fields, optional
    window_turns (default 3) and diff_fn (default: live git diff);
    when set, each call sends [system, task, state] + recent window
    instead of the masked transcript, with sticky fallback to the
    masked view on any validation failure) and loop_guard (deterministic
    L1/L2 repetition refusals, counted separately).
    Test-after-edit option: test_after_edit (an edit_file call is
    refused when the previous executed edit has no intervening
    executed razor_run_tests call, and finish is refused when the
    last executed edit has no test run after it; enforced, not
    advisory — see the module docstring).
    Read-budget option: read_budget (max EXECUTED read ops per arm
    across razor_search, razor_outline, razor_read, and
    razor_read_artifact; further read calls are refused with an
    actionable message directing the agent to edit, test, or
    finish; refusals follow F5 accounting and are counted
    separately in read_budget_refusals — see the module
    docstring).
    Harness auto-test option: auto_test_fn (a driver-supplied
    zero-arg callable returning {exit_code, failed_names,
    failures [{test_id, assertion, traceback_tail, signature}],
    counts {failed, passed}, tail_lines, n_failing} or {error} /
    {skipped}; legacy dicts without "failures"/"counts" still
    render names + tail lines; see the module docstring) — when
    set, a message that executed a successful edit with no
    executed manual test after it gets one harness-run
    failing-test verification, fed back as a rich failures-only
    user message (summary + delta + grouped failure detail)
    consuming no productive step, subject to the no-progress
    cutoff (proposal §2c).
    PAUSE-style plan option: plan_fn (a driver-supplied callable
    taking the pre-edit transcript — a list of messages — and
    returning {plan_text, sites, tests, model, input_tokens,
    output_tokens} or {error}; see the module docstring) — when
    set, the loop asks it for ONE repair plan immediately before
    the first edit_file call executes, appends the plan once as a
    never-masked user message, and records plan telemetry plus an
    advisory-only finish checklist. The plan never refuses an edit
    or a finish; any planner failure degrades to Step 3 as-is.
    Phase 1 verification-first gates option: phase_gates (a
    phase_gates.PhaseGateContext, or None). When set, the loop
    enforces the design's gates (DESIGN-phase1-gates.md §1,
    check order Appendix B): the edit gate (no edit before a
    recorded failing repro or repro waiver; refusal text exact,
    F5 accounting, counted in edit_gate_refusals), the finish
    gate (repro + latest entry on the current patch green, or
    pre-existing waiver, else a harness finish-check run via
    the context's finish_check_fn and re-evaluation; refusals
    counted in finish_gate_refusals), lint-on-edit for .py
    files (ast.parse after edit, revert + F4-style error on
    failure, "(syntax OK)" on success, skips recorded in
    lint_skipped), and append-only test-ledger recording for
    every executed agent test run. The context's repro_summary
    is sent as a never-masked user message after the task
    prompt, before the first backend call. When phase_gates is
    None (all prior drivers) the loop behaves exactly as
    before: every Phase 1 branch is guarded on it, and the
    result dict carries neutral defaults for the gate fields.
    Phase 1 assumes masked-transcript mode (state_config None):
    the repro message is part of the transcript, not the state
    window."""
    system = system_text if system_text is not None \
        else SYSTEM + system_extra
    if lane == "gemini" and system_text is None:
        system += GEMINI_TOOL_PROTOCOL
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user_prompt}]
    # Parallel metadata for masking: None for non-tool messages, else a
    # dict {tool, maskable, artifact_id, gemini_prefix}. Kept aligned
    # with messages by add_msg; never sent to the model.
    metas = [None, None]

    def add_msg(msg, meta=None):
        messages.append(msg)
        metas.append(meta)

    # Phase 1: the repro summary is a harness directive — appended
    # after the task prompt, before the first backend call, never
    # masked (maskable False, like guard refusals). It is the
    # edit gate's information payload, not a replacement task
    # list (the prompt's dotted failing list is unchanged).
    if phase_gates is not None and phase_gates.repro_summary:
        add_msg({"role": "user", "content": phase_gates.repro_summary},
                {"tool": "repro", "maskable": False})

    defs = tool_defs if tool_defs is not None else TOOLS
    runner = tool_runner if tool_runner is not None else run_tool
    allowed = {f["function"]["name"] for f in defs}
    bypass_attempts = []
    guard_triggers = 0
    loop_guard_triggers = 0
    masked_distinct = {}
    # Step 4 repair F5/F6 tracking.
    refusals = 0  # guard refusals (allocation + loop), NOT steps
    loop_guard_l1 = 0
    loop_guard_l2 = 0
    # Test-after-edit enforcement tracking (opt-in; the flag is
    # maintained unconditionally but only read when enabled).
    edit_unverified = False  # an executed edit with no test since
    test_after_edit_refusals = 0
    test_after_edit_finish_refusals = 0
    # Read-budget tracking (opt-in; the counters are maintained
    # unconditionally but only read when read_budget is set).
    read_ops_used = 0  # executed read-tool calls in the budget set
    read_budget_refusals = 0
    # Phase 1 verification-first gates tracking (opt-in; the
    # counters are maintained unconditionally but only read
    # when phase_gates is set).
    edit_gate_refusals = 0
    finish_gate_refusals = 0
    lint_rejects = 0
    lint_skipped = []  # [{file, reason}] — §2 rules 9/10 + unreadable
    # Harness auto-test tracking (opt-in; maintained unconditionally
    # but only read when auto_test_fn is set).
    auto_test_runs = 0
    auto_test_outcomes = []  # [{outcome, exit_code, failed,
    # fallback, valid, assertion_present, delta}]
    auto_test_summary_chars = 0
    auto_test_chars = 0  # anatomy: summary chars (fresh content)
    # Fixed-signal re-run state (proposal §2c/§3): delta vs the
    # previous auto-test run, and the no-progress cutoff. The first
    # run in an arm has no delta and never counts toward the
    # cutoff; only informative (fail/pass) runs move the streak —
    # error/skipped runs are neutral. The cutoff stops FIRING, it
    # never fails the arm.
    prev_auto_test_failed = None  # None = no prior run yet
    auto_test_no_progress_streak = 0
    auto_test_cutoff_fired = False
    auto_test_cutoff_run = None
    msg_pending_auto_test = False  # reset per message: set by a
    # SUCCESSFUL edit, cleared by an executed manual test in the
    # same message; failed edits leave it as-is
    # PAUSE-style plan tracking (opt-in; maintained unconditionally
    # but only read when plan_fn is set). The planner fires at most
    # once per arm, before the first edit executes.
    plan_generated = False
    planner_runs = 0
    planner_model = None
    planner_input_tokens = 0
    planner_output_tokens = 0
    plan_parse_ok = False
    planner_error = None
    plan_sites = []  # [{file, detail}] from the driver's parser
    plan_tests = []  # [test name] from the driver's parser
    plan_text_chars = 0
    plan_raw_on_failure = ""  # truncated plan text, parse failures only
    # Deferred plan delivery (adjacency fix): the plan user message
    # is BUFFERED at the hook and flushed only after every tool
    # result of the current assistant message — never between an
    # assistant tool_calls message and its tool results, which
    # OpenAI rejects with 400 (and the poisoned transcript then
    # 400s every retry, killing the arm via llm_error).
    pending_plan_msg = None
    failed_edits = []  # [{path, error}] — F6 telemetry (F4: last_edit)
    # Step 4 state tracking (only used when state_config is set).
    state_fallback = False
    state_fallback_turn = None
    state_fallback_reasons = []
    state_sizes = []
    state_saved_chars = 0
    state_message_chars = 0
    state_events = []
    state_last_test = None
    state_last_edit = None  # F4: {path, ok, error} of last edit attempt
    # Step 4 repair F3: most recent read per file (insertion-ordered
    # dict, oldest first; reversed at build time = most-recent-first)
    # and the agent's last non-empty assistant text, verbatim.
    recent_reads = {}
    last_assistant_text = ""
    executed_calls = []  # (tool_name, args_json) for L1, executed only
    test_runs = []  # (exit_code, failed_tuple, n_edits_at_run) for L2
    per_call_prompt, per_call_completion = [], []
    # Phase 0 instrumentation (telemetry only): per-call usage
    # breakdown (cached/reasoning) and per-call executed-tool lists,
    # aligned by index with per_call_prompt. These lists are appended
    # at exactly the same sites as the prompt/completion lists, so
    # every model call has exactly one entry in each list. Nothing
    # here changes prompts, tools, masking, guard, or grading.
    per_call_cached, per_call_reasoning, per_call_tools = [], [], []
    read_chars = test_chars = edit_chars = other_chars = 0
    assistant_chars = 0
    n_edits = n_tests = steps = 0
    tool_mix = {}
    finished = False
    finish_summary = ""
    final_finish_note = None  # Phase 1: waiver note, else None
    llm_error = None
    stop_reason = None
    crashed = None
    consecutive_errors = 0
    t_start = time.time()
    try:
        while True:
            if stop_reason is not None:
                break
            if time.time() - t_start > deadline_s:
                stop_reason = "timeout"
                break
            if cost_guard is not None:
                reason = cost_guard(sum(per_call_prompt),
                                    sum(per_call_completion))
                if reason:
                    stop_reason = reason
                    break
            if lane == "gemini":
                time.sleep(7)  # Gemini client 6s floor (per-process pacing)
            if state_config is not None and not state_fallback:
                # State mode: build + validate the state, then send
                # [system, task, state] + recent window instead of the
                # transcript. ANY validation failure -> sticky fallback
                # to the Step 3 masked view for the rest of the arm.
                diff_fn = state_config.get("diff_fn") or _default_diff_fn
                try:
                    diff_text, diff_stat = diff_fn(repo)
                    diff_error = None
                except Exception as e:
                    diff_text, diff_stat, diff_error = "", "", str(e)[:200]
                if diff_error is not None:
                    state_fallback = True
                    state_fallback_turn = len(per_call_prompt) + 1
                    state_fallback_reasons = [
                        f"git diff unavailable: {diff_error}"]
                else:
                    st = build_state(
                        state_config["frozen"], state_events,
                        diff_text, diff_stat, state_last_test,
                        len(per_call_prompt), steps, n_edits,
                        excerpts=list(reversed(
                            recent_reads.values())),
                        last_assistant_text=last_assistant_text,
                        last_edit=state_last_edit)
                    ok, reasons = validate_state(
                        st, state_config["frozen"], diff_text, diff_stat,
                        state_last_test)
                    if not ok:
                        state_fallback = True
                        state_fallback_turn = len(per_call_prompt) + 1
                        state_fallback_reasons = reasons
                if state_fallback:
                    # Fallback view: Step 3 masked view (or full history
                    # when masking is off — the driver always passes
                    # mask_recent_n in Step 4).
                    if mask_recent_n is not None:
                        call_messages, masked_now = masked_view(
                            messages, metas, mask_recent_n, mask_sink)
                        for idx, orig, stub in masked_now:
                            if idx not in masked_distinct:
                                masked_distinct[idx] = (orig, stub)
                    else:
                        call_messages = messages
                else:
                    sm = state_message(st)
                    window = recent_window(
                        messages,
                        state_config.get("window_turns", WINDOW_TURNS))
                    call_messages = [messages[0], messages[1], sm] + window
                    state_sizes.append(len(sm["content"]))
                    state_message_chars += len(sm["content"])
                    if mask_recent_n is not None:
                        # Accounting only: chars the Step 3 masked
                        # view would have sent, vs the state view
                        # actually sent. Step 4 repair F2: pass the
                        # real sink (was sink=None) so any maskable
                        # result still lacking an id gets one here
                        # exactly as Step 3 mints them; with eager
                        # minting at execution time this is normally
                        # a no-op, but it closes the gap for results
                        # produced before state mode engaged.
                        counter_view, _ = masked_view(
                            messages, metas, mask_recent_n, mask_sink)
                        masked_chars = sum(
                            len(m.get("content", "")) for m in counter_view)
                        view_chars = sum(
                            len(m.get("content", "")) for m in call_messages)
                        state_saved_chars += max(
                            0, masked_chars - view_chars)
            elif mask_recent_n is not None:
                # Masked copy only: stored history is never mutated.
                call_messages, masked_now = masked_view(
                    messages, metas, mask_recent_n, mask_sink)
                for idx, orig, stub in masked_now:
                    if idx not in masked_distinct:
                        masked_distinct[idx] = (orig, stub)
            else:
                call_messages = messages
            resp, meta = backend.complete(
                call_messages, None if lane == "gemini" else defs,
                temperature)
            if resp is None:
                consecutive_errors += 1
                steps += 1
                if consecutive_errors > 3:
                    llm_error = meta.get("error", "unknown llm error")
                    stop_reason = "llm_error"
                    break
                add_msg({"role": "user", "content":
                    f"That model call failed with: "
                    f"{meta.get('error', '')[:400]} Please retry with "
                    f"corrected tool parameters."})
                continue
            consecutive_errors = 0
            msg = resp["choices"][0]["message"]
            usage = resp.get("usage", {})
            per_call_prompt.append(int(usage.get("prompt_tokens", 0)))
            per_call_completion.append(int(usage.get("completion_tokens", 0)))
            # Phase 0: capture the provider breakdown from the SAME
            # usage dict (no new request, no prompt change). Absent
            # fields stay None (unknown), never 0 — see parse_usage.
            _usage = parse_usage(usage)
            per_call_cached.append(_usage["cached_input_tokens"])
            per_call_reasoning.append(_usage["reasoning_tokens"])
            per_call_tools.append([])
            content = msg.get("content") or ""
            assistant_chars += len(content)
            # Step 4 repair F3: keep the last NON-EMPTY assistant
            # text (tool-call-only messages carry "" and must not
            # erase the agent's last stated reasoning).
            if content:
                last_assistant_text = content
            if lane == "gemini":
                parsed = parse_tool_block(content)
                tool_calls = ([("gemini", parsed[0], parsed[1])]
                              if parsed else [])
                add_msg({"role": "assistant", "content": content})
            else:
                raw = msg.get("tool_calls") or []
                tool_calls = []
                for tc in raw:
                    try:
                        import json as _json
                        args = _json.loads(
                            tc["function"].get("arguments") or "{}")
                    except Exception:
                        args = {}
                    tool_calls.append((tc.get("id", ""),
                                       tc["function"]["name"], args))
                    assistant_chars += len(
                        tc["function"].get("arguments") or "")
                # Never store an empty tool_calls list: OpenAI rejects
                # assistant messages with "tool_calls": [] (400
                # "empty array"), and re-sending such a message turns
                # one text-only reply into a fatal retry spiral
                # (Step 4 arm 1: llm_error after 4 consecutive 400s).
                # A text-only reply is stored with no tool_calls key.
                if raw:
                    add_msg({"role": "assistant", "content": content,
                             "tool_calls": raw})
                else:
                    add_msg({"role": "assistant", "content": content})
            if not tool_calls:
                add_msg({"role": "user", "content":
                    "Please continue using tools, or call finish when done."})
                steps += 1
                if steps >= max_steps:
                    stop_reason = "step_budget"
                continue
            done = False
            msg_pending_auto_test = False
            finish_note = None  # Phase 1: waiver note for this message
            # Index of the current assistant message: the plan
            # transcript is everything except it (messages before
            # it, plus tool results appended during this message
            # before the edit — they precede the edit in execution
            # order). At this point the last message is always the
            # just-appended assistant message (the no-tool-calls
            # path `continue`s before reaching here).
            turn_msg_idx = len(messages) - 1
            for tc_id, fn, args in tool_calls:
                # Step 4 repair F5: steps counts PRODUCTIVE steps
                # only. The increment moved out of the loop top:
                # finish and executed calls increment below, guard
                # refusals increment `refusals` instead (separate
                # REFUSAL_BUDGET, "refusal_budget" stop reason).
                if (test_after_edit and fn == "finish"
                        and edit_unverified):
                    # Test-after-edit enforcement (finish case):
                    # finishing with an unverified edit is refused.
                    # F5 accounting: the refusal is NOT a productive
                    # step, counts toward the shared refusal budget,
                    # and is counted separately (finish refusals
                    # additionally flagged distinctly).
                    test_after_edit_refusals += 1
                    test_after_edit_finish_refusals += 1
                    refusals += 1
                    out = (f"TEST_AFTER_EDIT: finish refused — your "
                           f"last edit has no razor_run_tests run "
                           f"after it. Run razor_run_tests to verify "
                           f"the edit before finishing.")
                    if lane == "gemini":
                        add_msg({"role": "user", "content":
                                 f"Tool {fn} result:\n{out}"},
                                {"tool": fn, "maskable": False,
                                 "gemini_prefix":
                                     f"Tool {fn} result:\n"})
                    else:
                        add_msg({"role": "tool", "name": fn,
                                 "tool_call_id": tc_id, "content": out},
                                {"tool": fn, "maskable": False})
                    other_chars += len(out)
                    if refusals >= REFUSAL_BUDGET:
                        stop_reason = "refusal_budget"
                        break
                    if steps >= max_steps:
                        break
                    continue
                if phase_gates is not None and fn == "finish":
                    # Phase 1 finish gate (design §1b, Appendix B
                    # order: after enforcement-finish — OFF in
                    # Phase 1 — and before the allocation guard,
                    # which is also OFF in Phase 1). Refusals
                    # follow F5 exactly: no productive step,
                    # shared REFUSAL_BUDGET, separately counted
                    # in finish_gate_refusals. A refused finish
                    # does NOT end the arm (unlike an executed
                    # finish): the agent gets another turn.
                    cur_patch_error = ""
                    try:
                        cur_patch = patch_id(repo)
                    except Exception as _e:
                        cur_patch = None
                        cur_patch_error = str(_e)[:200]
                    if cur_patch is None:
                        finish_gate_refusals += 1
                        refusals += 1
                        out = FINISH_CHECK_INFRA_TEMPLATE.format(
                            reason="patch unavailable: "
                                   + cur_patch_error)
                        if lane == "gemini":
                            add_msg({"role": "user", "content":
                                     f"Tool {fn} result:\n{out}"},
                                    {"tool": fn, "maskable": False,
                                     "gemini_prefix":
                                         f"Tool {fn} result:\n"})
                        else:
                            add_msg({"role": "tool", "name": fn,
                                     "tool_call_id": tc_id,
                                     "content": out},
                                    {"tool": fn, "maskable": False})
                        other_chars += len(out)
                        if refusals >= REFUSAL_BUDGET:
                            stop_reason = "refusal_budget"
                            break
                        if steps >= max_steps:
                            break
                        continue
                    decision, detail = evaluate_finish(
                        phase_gates.ledger, phase_gates.waivers,
                        cur_patch, phase_gates.to_fix_info,
                        phase_gates.repro_failed)
                    if decision == "check_needed":
                        # The harness runs the scoped command
                        # itself (no productive step, no model
                        # call); the fn appends its entry to the
                        # ledger and returns it. Then re-evaluate.
                        if phase_gates.finish_check_fn is None:
                            check_entry = {
                                "exit_code": None,
                                "error": "no finish-check runner "
                                         "configured"}
                        else:
                            check_entry = phase_gates.finish_check_fn(
                                cur_patch, n_edits,
                                len(per_call_prompt),
                                round(time.time() - t_start, 1))
                        if check_entry.get("exit_code") is None:
                            finish_gate_refusals += 1
                            refusals += 1
                            out = FINISH_CHECK_INFRA_TEMPLATE.format(
                                reason=(check_entry.get("error")
                                        or "unknown error"))
                            if lane == "gemini":
                                add_msg({"role": "user", "content":
                                         f"Tool {fn} result:\n{out}"},
                                        {"tool": fn, "maskable": False,
                                         "gemini_prefix":
                                             f"Tool {fn} result:\n"})
                            else:
                                add_msg({"role": "tool", "name": fn,
                                         "tool_call_id": tc_id,
                                         "content": out},
                                        {"tool": fn, "maskable": False})
                            other_chars += len(out)
                            if refusals >= REFUSAL_BUDGET:
                                stop_reason = "refusal_budget"
                                break
                            if steps >= max_steps:
                                break
                            continue
                        decision, detail = evaluate_finish(
                            phase_gates.ledger, phase_gates.waivers,
                            cur_patch, phase_gates.to_fix_info,
                            phase_gates.repro_failed)
                        if decision == "check_needed":
                            # Unreachable in practice (the check
                            # appended a usable entry); fail
                            # closed rather than executing finish.
                            decision, detail = "refuse", {
                                "entry": check_entry, "failed": []}
                    if decision == "execute":
                        pass  # fall through to finish execution
                    elif decision == "waiver":
                        phase_gates.waivers.append({
                            "kind": "finish_preexisting",
                            "reason": "latest module run on current "
                                      "patch fails only on "
                                      "pre-existing-unrelated tests",
                            "tests": list(detail.get("waived") or []),
                            "patch_id": cur_patch,
                            "turn": len(per_call_prompt)})
                        finish_note = waiver_note_text(
                            detail.get("waived") or [])
                        # fall through to finish execution
                    elif decision == "refuse_no_repro":
                        finish_gate_refusals += 1
                        refusals += 1
                        out = EDIT_GATE_REFUSAL
                        if lane == "gemini":
                            add_msg({"role": "user", "content":
                                     f"Tool {fn} result:\n{out}"},
                                    {"tool": fn, "maskable": False,
                                     "gemini_prefix":
                                         f"Tool {fn} result:\n"})
                        else:
                            add_msg({"role": "tool", "name": fn,
                                     "tool_call_id": tc_id,
                                     "content": out},
                                    {"tool": fn, "maskable": False})
                        other_chars += len(out)
                        if refusals >= REFUSAL_BUDGET:
                            stop_reason = "refusal_budget"
                            break
                        if steps >= max_steps:
                            break
                        continue
                    elif decision == "refuse":
                        finish_gate_refusals += 1
                        refusals += 1
                        out = finish_refusal_text(detail["entry"])
                        if lane == "gemini":
                            add_msg({"role": "user", "content":
                                     f"Tool {fn} result:\n{out}"},
                                    {"tool": fn, "maskable": False,
                                     "gemini_prefix":
                                         f"Tool {fn} result:\n"})
                        else:
                            add_msg({"role": "tool", "name": fn,
                                     "tool_call_id": tc_id,
                                     "content": out},
                                    {"tool": fn, "maskable": False})
                        other_chars += len(out)
                        if refusals >= REFUSAL_BUDGET:
                            stop_reason = "refusal_budget"
                            break
                        if steps >= max_steps:
                            break
                        continue
                if fn == "finish":
                    steps += 1
                    tool_mix[fn] = tool_mix.get(fn, 0) + 1
                    # Phase 0: finish is executed here, not at the
                    # choke point below (which the break skips).
                    per_call_tools[-1].append(fn)
                    finished = True
                    finish_summary = args.get("summary", "")
                    done = True
                    if finish_note:
                        final_finish_note = finish_note
                    finish_text = finish_note or "finished"
                    if lane == "gemini":
                        add_msg({"role": "user", "content":
                                 "Tool finish result: "
                                 + finish_text},
                                {"tool": "finish", "maskable": False})
                    else:
                        add_msg({"role": "tool", "name": "finish",
                                 "tool_call_id": tc_id,
                                 "content": finish_text},
                                {"tool": "finish", "maskable": False})
                    break
                if (guard_tools and fn in guard_tools
                        and guard_start_step is not None
                        and steps >= guard_start_step):
                    # Step-allocation guard (Step 3): enforced refusal.
                    # Step 4 repair F5: refusals are NOT counted in
                    # tool_mix (as before) and NO LONGER consume a
                    # productive step; they count toward the separate
                    # refusal budget instead. The step-23 threshold
                    # now counts productive steps.
                    guard_triggers += 1
                    refusals += 1
                    out = (f"GUARD: step {steps} of {max_steps} — read "
                           f"tools are disabled in the final phase "
                           f"(steps {guard_start_step}-{max_steps} are "
                           f"reserved for edit_file, razor_run_tests, "
                           f"and finish). Edit the code, run the tests, "
                           f"or finish; do not call read tools again.")
                    if lane == "gemini":
                        add_msg({"role": "user", "content":
                                 f"Tool {fn} result:\n{out}"},
                                {"tool": fn, "maskable": False,
                                 "gemini_prefix":
                                     f"Tool {fn} result:\n"})
                    else:
                        add_msg({"role": "tool", "name": fn,
                                 "tool_call_id": tc_id, "content": out},
                                {"tool": fn, "maskable": False})
                    other_chars += len(out)
                    if refusals >= REFUSAL_BUDGET:
                        stop_reason = "refusal_budget"
                        break
                    if steps >= max_steps:
                        break
                    continue
                if (read_budget is not None
                        and fn in READ_BUDGET_TOOLS
                        and read_ops_used >= read_budget):
                    # Read-budget guard (Step 3 + read-budget):
                    # enforced refusal once the arm has executed
                    # read_budget read ops. F5 accounting: the
                    # refusal is NOT a productive step, counts
                    # toward the shared refusal budget, and is
                    # counted separately from allocation/loop/
                    # enforcement refusals. Refused reads do NOT
                    # increment read_ops_used (only executed reads
                    # count). Order: allocation guard first
                    # (preserves Step 3 counts), then read-budget,
                    # then enforcement, then loop guard.
                    read_budget_refusals += 1
                    refusals += 1
                    out = (f"READ_BUDGET: read refused — you have "
                           f"used {read_ops_used} of {read_budget} "
                           f"read operations this arm (reads: "
                           f"razor_search, razor_outline, "
                           f"razor_read, razor_read_artifact). Do "
                           f"not call read tools again; they will "
                           f"all be refused. Edit the code, run "
                           f"razor_run_tests, or finish.")
                    if lane == "gemini":
                        add_msg({"role": "user", "content":
                                 f"Tool {fn} result:\n{out}"},
                                {"tool": fn, "maskable": False,
                                 "gemini_prefix":
                                     f"Tool {fn} result:\n"})
                    else:
                        add_msg({"role": "tool", "name": fn,
                                 "tool_call_id": tc_id, "content": out},
                                {"tool": fn, "maskable": False})
                    other_chars += len(out)
                    if refusals >= REFUSAL_BUDGET:
                        stop_reason = "refusal_budget"
                        break
                    if steps >= max_steps:
                        break
                    continue
                if (test_after_edit and fn == "edit_file"
                        and edit_unverified):
                    # Test-after-edit enforcement (edit case): an
                    # edit with no intervening test run since the
                    # last executed edit is refused. F5 accounting:
                    # the refusal is NOT a productive step, counts
                    # toward the shared refusal budget, and is
                    # counted separately from allocation/loop
                    # refusals. Order: allocation guard first
                    # (preserves Step 3 counts), then enforcement,
                    # then loop guard.
                    test_after_edit_refusals += 1
                    refusals += 1
                    out = (f"TEST_AFTER_EDIT: edit refused — your "
                           f"last edit has no razor_run_tests run "
                           f"after it. Run razor_run_tests to verify "
                           f"the edit before editing again.")
                    if lane == "gemini":
                        add_msg({"role": "user", "content":
                                 f"Tool {fn} result:\n{out}"},
                                {"tool": fn, "maskable": False,
                                 "gemini_prefix":
                                     f"Tool {fn} result:\n"})
                    else:
                        add_msg({"role": "tool", "name": fn,
                                 "tool_call_id": tc_id, "content": out},
                                {"tool": fn, "maskable": False})
                    other_chars += len(out)
                    if refusals >= REFUSAL_BUDGET:
                        stop_reason = "refusal_budget"
                        break
                    if steps >= max_steps:
                        break
                    continue
                if loop_guard and fn != "finish":
                    # L1: same tool+args as the previous two EXECUTED
                    # calls (consecutive repetition). L2: same test
                    # outcome as the previous two test runs with no
                    # edit since the earlier of the two. Refusals are
                    # NOT executed calls, so they never satisfy L1/L2
                    # themselves. Allocation guard was already checked
                    # above, so its counts stay Step 3-comparable.
                    args_json = _json.dumps(args, sort_keys=True)
                    loop_hit = None
                    loop_hit_kind = None  # F6: L1/L2 split
                    if (len(executed_calls) >= 2
                            and executed_calls[-1] == (fn, args_json)
                            and executed_calls[-2] == (fn, args_json)):
                        loop_hit = (f"LOOP_GUARD: tool {fn} with "
                                    f"identical arguments was already "
                                    f"executed twice in a row. Do not "
                                    f"repeat it a third time — try a "
                                    f"different file, symbol, query, "
                                    f"or approach, or edit the code.")
                        loop_hit_kind = "L1"
                    if (loop_hit is None and fn == "razor_run_tests"
                            and len(test_runs) >= 2
                            and test_runs[-1][:2] == test_runs[-2][:2]
                            and n_edits == test_runs[-2][2]):
                        loop_hit = (f"LOOP_GUARD: the last two test "
                                    f"runs produced the identical "
                                    f"outcome with no edit in between. "
                                    f"Do not re-run the tests again "
                                    f"unchanged — edit the code first, "
                                    f"or try a different approach.")
                        loop_hit_kind = "L2"
                    if loop_hit is not None:
                        loop_guard_triggers += 1
                        # F5: loop refusals count toward the refusal
                        # budget, not productive steps. F6: split.
                        refusals += 1
                        if loop_hit_kind == "L1":
                            loop_guard_l1 += 1
                        elif loop_hit_kind == "L2":
                            loop_guard_l2 += 1
                        if lane == "gemini":
                            add_msg({"role": "user", "content":
                                     f"Tool {fn} result:\n{loop_hit}"},
                                    {"tool": fn, "maskable": False,
                                     "gemini_prefix":
                                         f"Tool {fn} result:\n"})
                        else:
                            add_msg({"role": "tool", "name": fn,
                                     "tool_call_id": tc_id,
                                     "content": loop_hit},
                                    {"tool": fn, "maskable": False})
                        other_chars += len(loop_hit)
                        if refusals >= REFUSAL_BUDGET:
                            stop_reason = "refusal_budget"
                            break
                        if steps >= max_steps:
                            break
                        continue
                if phase_gates is not None and fn == "edit_file":
                    # Phase 1 edit gate (design §1a, Appendix B
                    # order: after loop guard, before the plan
                    # hook — preserving the invariant that a
                    # refused edit never triggers the planner).
                    # F5 accounting exactly: no productive step,
                    # shared REFUSAL_BUDGET, separately counted
                    # in edit_gate_refusals. In the normal path
                    # the precheck entry satisfies the gate
                    # before the first backend call, so any
                    # refusal here is an anomaly (precheck/
                    # ledger failure); the refusal is actionable
                    # (an agent test run that fails clears it).
                    if not repro_satisfied(phase_gates.ledger,
                                           phase_gates.waivers):
                        edit_gate_refusals += 1
                        refusals += 1
                        out = EDIT_GATE_REFUSAL
                        if lane == "gemini":
                            add_msg({"role": "user", "content":
                                     f"Tool {fn} result:\n{out}"},
                                    {"tool": fn, "maskable": False,
                                     "gemini_prefix":
                                         f"Tool {fn} result:\n"})
                        else:
                            add_msg({"role": "tool", "name": fn,
                                     "tool_call_id": tc_id,
                                     "content": out},
                                    {"tool": fn, "maskable": False})
                        other_chars += len(out)
                        if refusals >= REFUSAL_BUDGET:
                            stop_reason = "refusal_budget"
                            break
                        if steps >= max_steps:
                            break
                        continue
                # PAUSE-style pre-first-edit plan (opt-in): before
                # the FIRST edit_file call executes — refusals above
                # `continue` before reaching here, so a refused edit
                # never triggers the planner — ask plan_fn for ONE
                # repair plan from the pre-edit transcript. The plan
                # is BUFFERED here and flushed only after every tool
                # result of the current assistant message (see the
                # flush below the tool-call loop): it must NEVER sit
                # between an assistant tool_calls message and its
                # tool results — OpenAI rejects that with 400, and
                # the poisoned transcript then 400s every retry,
                # killing the arm via llm_error (all 5 first paid
                # plan arms died exactly this way). Agent-visible
                # timing is unchanged by the deferral: the agent
                # first sees the plan on the call after the first
                # edit in both designs, since the old code appended
                # it before execution but the agent only ever read
                # it on the next backend call, after the edit result
                # anyway. The first edit itself was already decided,
                # so the plan guides subsequent turns (PAUSE resumes
                # the same executor rather than vetoing the edit).
                # A failed first edit still gets a plan: the hook is
                # pre-execution, so the plan exists before any edit
                # attempt, successful or not.
                # The planner consumes NO productive step, no
                # tool_mix entry, and no executor model call. Any
                # planner failure degrades to Step 3 as-is: the arm
                # never crashes for planner reasons, and the plan is
                # never a gate (it cannot refuse an edit or a
                # finish — the finish checklist is advisory
                # telemetry only, computed at arm end).
                # planner_runs counts planner INVOCATIONS (attempts);
                # plan_parse_ok marks the ones that produced an
                # appended plan. A failed/malformed plan still counts
                # as a run (its tokens, if any, are recorded) but
                # never appends a plan message.
                if (plan_fn is not None and not plan_generated
                        and fn == "edit_file"):
                    plan_generated = True
                    planner_runs += 1
                    try:
                        plan = plan_fn(messages[:turn_msg_idx]
                                       + messages[turn_msg_idx + 1:])
                        if not isinstance(plan, dict):
                            plan = {"error":
                                    "planner returned non-dict"}
                    except Exception as e:
                        plan = {"error":
                                f"{type(e).__name__}: {e}"[:200]}
                    planner_model = plan.get("model")
                    try:
                        planner_input_tokens += int(
                            plan.get("input_tokens") or 0)
                        planner_output_tokens += int(
                            plan.get("output_tokens") or 0)
                    except (TypeError, ValueError):
                        pass
                    if plan.get("error"):
                        planner_error = str(plan["error"])[:200]
                    else:
                        ptext = plan.get("plan_text") or ""
                        psites = plan.get("sites") or []
                        if ptext and psites:
                            plan_parse_ok = True
                            plan_sites = psites
                            plan_tests = plan.get("tests") or []
                            plan_text_chars = len(ptext)
                            # Deferred, not appended here: flushing
                            # now would wedge this user message
                            # between the assistant's tool_calls and
                            # their tool results (OpenAI 400).
                            pending_plan_msg = (
                                PLAN_MESSAGE_PREFIX + ptext)
                        else:
                            planner_error = (
                                "planner plan unparseable "
                                "(empty text or zero sites)")
                            if ptext:
                                plan_raw_on_failure = ptext[:2000]
                # Phase 1 lint-on-edit snapshot (design §4.1):
                # for .py edits, snapshot pre-edit bytes and
                # pre-parse (a pre-existing syntax error skips
                # lint for that file). The snapshot is taken
                # per tool call, immediately before execution;
                # the post-edit check below runs only for
                # executed edits whose result starts with "OK".
                lint_snap = None
                if (phase_gates is not None
                        and phase_gates.lint_enabled
                        and fn == "edit_file"):
                    lint_snap = _lint_snapshot(
                        repo, args.get("path", ""))
                # Productive step (F5): executed calls only — guard
                # refusals above never reach here.
                steps += 1
                tool_mix[fn] = tool_mix.get(fn, 0) + 1
                # Phase 0: record the executed tool on the current
                # call's list (refusals above `continue` before
                # reaching here, so they are never recorded as
                # executed — matching tool_mix exactly).
                per_call_tools[-1].append(fn)
                if fn not in allowed:
                    bypass_attempts.append(fn)
                    out = (f"ERROR: tool {fn} is not available in this "
                           "harness. Use the razor_* tools for reads and "
                           "tests, edit_file for edits, finish to end.")
                    kind = "other"
                else:
                    out, kind = runner(repo, fn, args)
                # Phase 1 lint-on-edit check (design §4.1/§4.5):
                # a successful Python edit is parsed; on parse
                # failure the file is restored byte-identical
                # (deleted when the edit created it — defensive
                # only, the naive edit tool never creates files)
                # and the result becomes an F4-style edit failure
                # (kind "other", no n_edits increment, and the
                # F4 block below sets edit_unverified=True, so
                # the existing rejected-edit recovery flow
                # handles it). A successful Python edit appends
                # "(syntax OK)". Skips are recorded, never
                # silent (§2 rules 9/10).
                if (lint_snap is not None
                        and fn == "edit_file"
                        and out.startswith("OK")):
                    if lint_snap["skip"] is not None:
                        lint_skipped.append({
                            "file": args.get("path", ""),
                            "reason": lint_snap["skip"]})
                    else:
                        try:
                            with open(lint_snap["full"], "rb") as f:
                                new_bytes = f.read()
                        except Exception:
                            new_bytes = None
                        if new_bytes is None:
                            lint_skipped.append({
                                "file": args.get("path", ""),
                                "reason": "unreadable"})
                        else:
                            try:
                                ast.parse(new_bytes)
                            except SyntaxError as se:
                                try:
                                    if lint_snap["old"] is None:
                                        os.remove(lint_snap["full"])
                                    else:
                                        with open(lint_snap["full"],
                                                  "wb") as f:
                                            f.write(lint_snap["old"])
                                except Exception:
                                    pass
                                out = (f"ERROR: edit rejected by lint "
                                       f"— {se} (no change applied)")
                                kind = "other"
                                lint_rejects += 1
                            else:
                                out = out + " (syntax OK)"
                # Read-budget: only EXECUTED read-tool calls in the
                # budget set consume the budget. Bypass errors and
                # refusals never reach here, so they cannot.
                if fn in READ_BUDGET_TOOLS and fn in allowed:
                    read_ops_used += 1
                if kind == "read":
                    read_chars += len(out)
                elif kind == "test":
                    test_chars += len(out)
                    n_tests += 1
                elif kind == "edit":
                    edit_chars += len(out)
                    if out.startswith("OK"):
                        n_edits += 1
                else:
                    other_chars += len(out)
                # Step 4 repair F4: record the last edit attempt
                # (success or rejection verbatim) for the state file;
                # F6: failed-edit reasons for telemetry. Note the
                # edit tool returns kind "other" on failure, so this
                # check is on fn + out, not on kind.
                if fn == "edit_file":
                    edit_ok = out.startswith("OK")
                    state_last_edit = {
                        "path": args.get("path", ""),
                        "ok": edit_ok,
                        "error": "" if edit_ok else out}
                    if not edit_ok:
                        failed_edits.append({
                            "path": args.get("path", ""),
                            "error": out})
                    # Test-after-edit: ANY executed edit (success or
                    # rejection) leaves an unverified edit pending.
                    # Documented consequence, not a hidden change:
                    # the F4 recovery flow (re-read, then retry after
                    # a rejected edit) now includes a test run
                    # between rejection and retry, because only an
                    # executed test run clears the flag.
                    edit_unverified = True
                    if edit_ok:
                        # Harness auto-test: only a SUCCESSFUL
                        # edit earns verification (a failed edit
                        # changed no code; testing it wastes a
                        # run). A failed edit leaves the flag
                        # as-is: an earlier successful edit in the
                        # same message still needs verification.
                        msg_pending_auto_test = True
                if phase_gates is not None:
                    phase_gates.n_edits = n_edits
                # Step 4 repair F3: most recent read per file
                # (verbatim content) for state excerpts.
                if fn == "razor_read" and kind == "read":
                    try:
                        robj = _json.loads(out)
                    except Exception:
                        robj = None
                    if isinstance(robj, dict) and not robj.get("error") \
                            and robj.get("content") is not None:
                        rpath = robj.get("path") or args.get("path")
                        if rpath:
                            recent_reads.pop(rpath, None)
                            recent_reads[rpath] = {
                                "path": rpath,
                                "lines": (f"{robj.get('start_line')}-"
                                          f"{robj.get('end_line')}"),
                                "content": robj.get("content", "")}
                # Step 4 repair F2 (state mode only): mint a hist-
                # id for every maskable result that has no tool
                # artifact id of its own, so findings can reference
                # it. Step 3 masked mode keeps lazy minting in
                # masked_view and is untouched by this branch.
                meta_aid = _tool_artifact_id(out)
                if (state_config is not None and mask_sink is not None
                        and fn in MASKABLE_TOOLS and fn in allowed
                        and meta_aid is None):
                    try:
                        meta_aid = mask_sink(fn, out)
                    except Exception:
                        meta_aid = None
                meta = {"tool": fn,
                        "maskable": fn in MASKABLE_TOOLS
                        and fn in allowed,
                        "artifact_id": meta_aid}
                if lane == "gemini":
                    meta["gemini_prefix"] = f"Tool {fn} result:\n"
                    add_msg({"role": "user", "content":
                             f"Tool {fn} result:\n{out}"}, meta)
                else:
                    add_msg({"role": "tool", "name": fn,
                             "tool_call_id": tc_id, "content": out}, meta)
                # Step 4 trajectory tracking (executed calls only;
                # refusals never reach here). Turn = completed model
                # calls so far (per_call_prompt is appended on receipt,
                # before tool execution).
                executed_calls.append(
                    (fn, _json.dumps(args, sort_keys=True)))
                if fn == "razor_run_tests" and kind == "test":
                    # Test-after-edit: a test run verifies the pending
                    # edit, pass or fail (verification means running
                    # tests, not passing them). Refused test calls
                    # never reach here, so they cannot clear the flag.
                    # The same executed test also suppresses the
                    # harness auto-test for this message (the edit
                    # already has its verification).
                    edit_unverified = False
                    msg_pending_auto_test = False
                    try:
                        tobj = _json.loads(out)
                        failed = tuple(_parse_failed_names(
                            tobj.get("failures_inline", "")))
                        rc = tobj.get("exit_code")
                    except Exception:
                        rc, failed = None, ()
                    if rc is not None:
                        test_runs.append((rc, failed, n_edits))
                        state_last_test = {"exit_code": rc,
                                           "failed": list(failed)}
                if (phase_gates is not None
                        and fn == "razor_run_tests"
                        and kind == "test"):
                    # Phase 1 ledger: every EXECUTED agent test
                    # run is appended (design §3). The entry's
                    # patch is computed now (a git failure yields
                    # "unknown", which matches nothing — so a
                    # later finish falls through to a harness
                    # check, fail closed). Counts carry failed
                    # only (the inline result has no totals);
                    # passed stays None, never invented.
                    try:
                        _tobj = _json.loads(out)
                    except Exception:
                        _tobj = {}
                    _rc = _tobj.get("exit_code")
                    _failed = list(_parse_failed_names(
                        _tobj.get("failures_inline", "")))
                    try:
                        _tpatch = patch_id(repo)
                    except Exception:
                        _tpatch = "unknown"
                    phase_gates.append(new_entry(
                        phase_gates.next_run_id(), "agent",
                        "scoped", phase_gates.test_command,
                        _tpatch, n_edits, _rc, _failed,
                        {"failed": len(_failed), "passed": None},
                        (_tobj.get("failures_inline", "") or "")[:1500],
                        len(per_call_prompt),
                        round(time.time() - t_start, 1),
                        _tobj.get("artifact_id"),
                        format_fallback=False,
                        error=(None if _rc is not None else
                               "unparseable test result")))
                ev = _event_for(fn, args, out, kind,
                                len(per_call_prompt))
                if ev is not None:
                    # F2: findings carry the artifact id.
                    if meta_aid:
                        ev["artifact"] = meta_aid
                    state_events.append(ev)
                if steps >= max_steps:
                    break
            # Deferred plan flush (adjacency fix): the buffered plan
            # user message goes here, AFTER every tool result of the
            # current assistant message and BEFORE the next backend
            # call — so the agent still sees it before its next
            # decision, and no tool_calls message is ever followed
            # by a non-tool message before its results. This runs
            # whether the for loop ended normally or broke (step
            # budget, refusal budget); if the arm ends here, the
            # flushed message is simply never sent, which is
            # harmless (telemetry was recorded at the hook).
            if pending_plan_msg is not None:
                add_msg({"role": "user", "content": pending_plan_msg},
                        {"tool": "plan", "maskable": False})
                pending_plan_msg = None
            if done:
                stop_reason = "finished"
                break
            # Harness auto-test (Step 3 + auto-test, opt-in): after
            # a message that executed a successful edit with no
            # executed manual test after it, run the issue's
            # failing tests ONCE and feed back only a capped
            # failures-only summary as a user message. Consumes NO
            # productive step (that is the point), is NOT in
            # tool_mix or n_test_runs, does NOT consume refusal
            # budget, does NOT move the allocation-guard threshold
            # (productive steps unchanged), does NOT clear
            # edit_unverified, and never enters test_runs (so it
            # cannot trigger loop-guard L2). Skipped when the arm
            # finished (handled above), hit the step budget, or
            # exhausted refusals in this message. Any infra error
            # degrades to an "auto-test error" note; the arm
            # proceeds and manual testing remains available.
            if (auto_test_fn is not None and msg_pending_auto_test
                    and stop_reason is None and steps < max_steps
                    and not auto_test_cutoff_fired):
                try:
                    at = auto_test_fn()
                    if not isinstance(at, dict):
                        at = {"error": "auto-test returned non-dict"}
                except Exception as e:
                    at = {"error":
                          f"{type(e).__name__}: {e}"[:200]}
                outcome = ("error" if at.get("error") else
                           "skipped" if at.get("skipped") else
                           "pass" if at.get("exit_code") == 0
                           else "fail")
                failed = list(at.get("failed_names") or [])
                # Delta vs the previous auto-test run (proposal
                # §2b): pure set comparison on resolved ids.
                # First run: delta None (no previous run exists)
                # and the streak is untouched. Error/skipped runs
                # carry no delta and leave both the previous set
                # and the streak untouched (neutral, not progress
                # and not no-progress).
                delta = None
                if outcome in ("pass", "fail"):
                    cur = set(failed)
                    if prev_auto_test_failed is not None:
                        prev = prev_auto_test_failed
                        delta = {
                            "newly_passing": sorted(prev - cur),
                            "still_failing": sorted(prev & cur),
                            "newly_failing": sorted(cur - prev)}
                        if delta["newly_passing"]:
                            auto_test_no_progress_streak = 0
                        else:
                            auto_test_no_progress_streak += 1
                    prev_auto_test_failed = cur
                summary = _format_auto_test_summary(at, delta)
                auto_test_runs += 1
                # No-progress cutoff (proposal §2c): 3 consecutive
                # informative runs with zero newly-passing tests
                # stop FIRING (the arm continues; manual testing
                # remains available). Recorded at the run index
                # that trips it.
                if (auto_test_no_progress_streak >= 3
                        and not auto_test_cutoff_fired):
                    auto_test_cutoff_fired = True
                    auto_test_cutoff_run = auto_test_runs
                failures = at.get("failures")
                if failures is not None:
                    assertion_present = bool(
                        outcome == "fail" and any(
                            (f.get("assertion") or "").strip()
                            or (f.get("traceback_tail") or "").strip()
                            for f in failures))
                else:
                    # Legacy dicts: tail lines are the only
                    # traceback evidence available.
                    assertion_present = bool(
                        outcome == "fail"
                        and at.get("tail_lines"))
                auto_test_outcomes.append({
                    "outcome": outcome,
                    "exit_code": at.get("exit_code"),
                    "failed": failed,
                    # Phase 0: True when the run covered the scoped
                    # files because no failing name resolved; absent
                    # (False) for all pre-fix records and scripted
                    # fns that do not set it.
                    "fallback": bool(at.get("fallback")),
                    # Proposal §3 validity (exhaustive criteria;
                    # see _auto_test_validity).
                    "valid": _auto_test_validity(
                        outcome, at.get("exit_code"), failed),
                    "assertion_present": assertion_present,
                    "delta": delta})
                auto_test_summary_chars += len(summary)
                auto_test_chars += len(summary)
                add_msg({"role": "user", "content": summary},
                        {"tool": "auto_test", "maskable": False})
            # Guard against overwriting refusal_budget (F5): when a
            # refusal exhausted the refusal budget, steps is still
            # below max_steps (refusals don't consume steps), but
            # never let a later check clobber an already-set reason.
            if stop_reason is None and steps >= max_steps:
                stop_reason = "step_budget"
                break
    except Exception as e:  # never lose a partial arm's tokens
        crashed = f"{type(e).__name__}: {e}"[:500]
        stop_reason = "crash"
    if stop_reason is None:
        stop_reason = "step_budget" if steps >= max_steps else "unknown"
    try:
        patch = subprocess.run(["git", "diff", "--", "."], cwd=repo,
                               capture_output=True, text=True,
                               timeout=60).stdout
    except Exception:
        patch = ""
    # Phase 1 final gate state (audit surface): computed from a
    # fresh git diff, NOT from the patch string above — a failed
    # git call there yields patch "" (which patch_id_of_diff
    # would misread as pristine). A git failure here yields
    # patch None (blocked, never passed).
    gate_state_final = None
    if phase_gates is not None:
        try:
            _final_patch = patch_id(repo)
        except Exception:
            _final_patch = None
        gate_state_final = phase_gates.snapshot_with_patch(
            _final_patch)
    # PAUSE-style finish checklist (advisory telemetry only, never
    # a refusal): computed at arm end for any arm with a valid
    # plan, from the final diff + the last executed test run.
    # Finishing always proceeds; the agent does not see this
    # before finishing (finish ends the arm), so the checklist
    # measures plan adherence for the ablation rather than
    # intervening in the current arm.
    finish_checklist = None
    if plan_parse_ok:
        finish_checklist = _finish_checklist(
            repo, plan_sites, plan_tests, test_runs)
    # Phase 0 instrumentation (telemetry only): per-call breakdown
    # stream + call-class proxy + per-arm aggregates. Computed from
    # the parallel per-call lists, so alignment with per_call_prompt
    # is by construction. On a crash mid-arm the lists still align
    # (usage and tool entries are appended at the same sites).
    _per_call_usage = [
        {"prompt_tokens": p, "completion_tokens": c,
         "cached_input_tokens": ca, "reasoning_tokens": re}
        for p, c, ca, re in zip(per_call_prompt, per_call_completion,
                                per_call_cached, per_call_reasoning)]
    _per_call_classes = [classify_call(t) for t in per_call_tools]
    _usage_totals = aggregate_calls(_per_call_usage, _per_call_classes)
    return {
        "lane": lane, "model": model, "steps": steps,
        "turns": len(per_call_prompt),
        "tool_mix": tool_mix,
        "stop_reason": stop_reason,
        "wall_seconds": round(time.time() - t_start, 1),
        "finished": finished, "finish_summary": finish_summary,
        "llm_error": llm_error, "crashed": crashed,
        "prompt_tokens": sum(per_call_prompt),
        "completion_tokens": sum(per_call_completion),
        "per_call_prompt_tokens": per_call_prompt,
        "per_call_completion_tokens": per_call_completion,
        "per_call_cached_tokens": per_call_cached,
        "per_call_reasoning_tokens": per_call_reasoning,
        "per_call_tools": per_call_tools,
        "per_call_classes": _per_call_classes,
        "per_call_usage": _per_call_usage,
        "usage_totals": _usage_totals,
        "n_llm_calls": len(per_call_prompt),
        "n_edits": n_edits, "n_test_runs": n_tests,
        "valid": bool(n_edits >= 1 or n_tests >= 1),
        "anatomy_chars": {
            "system_plus_issue": len(system) + len(user_prompt),
            "reads": read_chars, "test_logs": test_chars,
            "edits_and_other_tool_outputs": edit_chars + other_chars,
            "assistant_text_and_tool_args": assistant_chars,
            "state_message_chars": state_message_chars,
            "auto_test_chars": auto_test_chars,
            "plan_chars": plan_text_chars},
        "patch": patch,
        "bypass_attempts": bypass_attempts,
        # Step 4 repair F5/F6 telemetry. `steps` above is now
        # productive steps (guard refusals excluded); `refusals`
        # counts allocation + loop refusals against REFUSAL_BUDGET.
        "refusals": refusals,
        "read_budget_refusals": read_budget_refusals,
        "test_after_edit_refusals": test_after_edit_refusals,
        "test_after_edit_finish_refusals":
            test_after_edit_finish_refusals,
        "loop_guard_l1": loop_guard_l1,
        "loop_guard_l2": loop_guard_l2,
        "failed_edits": len(failed_edits),
        "failed_edit_reasons": failed_edits,
        # Step 3 + harness auto-test telemetry (separate from the
        # agent's own test metric: auto-test runs are NOT in
        # n_test_runs, tool_mix, or test_runs).
        "auto_test_runs": auto_test_runs,
        "auto_test_outcomes": auto_test_outcomes,
        "auto_test_summary_chars": auto_test_summary_chars,
        # Fixed-signal re-run: no-progress cutoff state (proposal
        # §2c). cutoff_run is the 1-based auto-test run index that
        # tripped it, or None when it never fired.
        "auto_test_cutoff_fired": auto_test_cutoff_fired,
        "auto_test_cutoff_run": auto_test_cutoff_run,
        "auto_test_no_progress_streak": auto_test_no_progress_streak,
        # Step 3 + PAUSE-style plan telemetry (separate from the
        # agent's own metrics: the planner call is NOT in
        # n_llm_calls, tool_mix, or test_runs).
        "planner_runs": planner_runs,
        "planner_model": planner_model,
        "planner_input_tokens": planner_input_tokens,
        "planner_output_tokens": planner_output_tokens,
        "planner_parse_ok": plan_parse_ok,
        "planner_error": planner_error,
        "plan_sites": plan_sites,
        "plan_tests": plan_tests,
        "plan_text_chars": plan_text_chars,
        "plan_raw_on_failure": plan_raw_on_failure,
        "finish_checklist": finish_checklist,
        "state": {
            "enabled": state_config is not None,
            "window_turns": (state_config.get("window_turns",
                                               WINDOW_TURNS)
                              if state_config is not None else None),
            "fallback": state_fallback,
            "fallback_turn": state_fallback_turn,
            "fallback_reasons": state_fallback_reasons,
            "state_sizes": state_sizes,
            "state_chars_total": state_message_chars,
            "state_saved_chars": state_saved_chars,
        },
        "loop_guard_triggers": loop_guard_triggers,
        "masking": {
            "enabled": mask_recent_n is not None,
            "keep_recent": mask_recent_n,
            "masked_results": len(masked_distinct),
            "masked_chars_saved": sum(
                o - s for o, s in masked_distinct.values()),
        },
        "guard_triggers": guard_triggers,
        "read_ops": sum(tool_mix.get(t, 0) for t in
                        ("razor_search", "razor_outline", "razor_read",
                         "razor_read_artifact")),
        "read_op_share": (sum(tool_mix.get(t, 0) for t in
                              ("razor_search", "razor_outline",
                               "razor_read", "razor_read_artifact"))
                          / steps) if steps else 0.0,
        # Phase 1 verification-first gates (opt-in; neutral
        # defaults when phase_gates is None, so Step 3 result
        # shape is unchanged).
        "test_ledger": (list(phase_gates.ledger)
                        if phase_gates is not None else []),
        "gate_waivers": (list(phase_gates.waivers)
                         if phase_gates is not None else []),
        "repro_recorded": (repro_satisfied(phase_gates.ledger,
                                            phase_gates.waivers)
                           if phase_gates is not None else False),
        "repro_outcome": (repro_outcome(phase_gates.ledger,
                                        phase_gates.waivers)
                          if phase_gates is not None else None),
        "edit_gate_refusals": edit_gate_refusals,
        "finish_gate_refusals": finish_gate_refusals,
        "harness_finish_check_runs": (
            phase_gates.harness_finish_check_runs
            if phase_gates is not None else 0),
        "lint_rejects": lint_rejects,
        "lint_skipped": lint_skipped,
        "flake_notes": (list(phase_gates.flake_notes)
                        if phase_gates is not None else []),
        "finish_note": final_finish_note,
        "gate_state_final": gate_state_final,
    }
