#!/usr/bin/env python3
"""Callable two-arm comparison entry point for the razor_compare MCP tool.

One bug in, one side-by-side result out. The same bug runs twice, each
time on a fresh scratch copy: once with the plain Step 1 naive loop
(run_loop with no overrides: full tools, full history, no masking, no
guard), once with the frozen Step 3 system (budgeted Razor tools as the
only read path, masking keep-10, allocation guard refusing reads at
steps >= 23). Same prompt, same test gate, same model, same key. The
working tree is never touched; a patch is returned per arm only when
that arm's tests pass after its loop.

Contract (harness/RAZOR-COMPARE-CONTRACT.md, Nov 10, 2026):
  - Total cost cap (default $1.00), split half per arm ($0.50 each).
    An arm that hits its half-cap stops as COST_CAPPED; the other arm
    still runs. The cap is never exceeded (each arm's guard stops it
    with a $0.15 margin below its half-cap).
  - The comparison measures the harness naive loop vs Step 3, NOT the
    user's real agent. One bug is one sample.
  - The user's own OpenAI key, from the OPENAI_API_KEY environment
    variable (or the api_key argument). The key is never logged, stored,
    or sent anywhere except api.openai.com.
  - Python-only v1, git repos only (each patch is a git diff).

CLI (what the Node MCP bridge calls in the packaging step):
  python3 run_compare.py --repo /path/to/repo --issue "bug text"
      [--failing-tests tests/test_x.py::test_y,...]
      [--test-command "pytest tests/test_x.py -q"] [--cost-cap 1.00]
  Prints the result JSON to stdout. Exit 0 always carries a result;
  exit 2 is a usage error.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loop import MAX_STEPS, run_loop  # noqa: E402
from masking import MASK_KEEP_RECENT  # noqa: E402
from razor_tools import (RazorState, ablation_tool_defs,  # noqa: E402
                         make_razor_runner, step3_system)
from run_fix import (DirectOpenAIBackend, _build_prompt,  # noqa: E402
                     _detect_test_command, _make_guard, _parse_failed,
                     _resolve_names, GUARD_START, GUARD_TOOLS, MODEL)
from sandbox import run_tests, sh  # noqa: E402
from telemetry import cost_of  # noqa: E402

DEFAULT_CAP_USD = 1.00  # total for the pair; $0.50 per arm
SCRATCH_PREFIX = "razor-compare-"


def _setup_fail(reason):
    return {"status": "SETUP_FAIL", "naive": None, "razor": None,
            "comparison": None,
            "tokens": {"total": 0, "prompt": 0, "completion": 0},
            "cost_usd": 0.0, "billed_cost_usd": 0.0,
            "wall_seconds": 0.0, "summary": reason}


def _run_arm(repo, prompt, backend, model, arm_cap, step3, cmd,
             artifacts_dir):
    """Run one arm on its scratch copy and grade it. Same arm record
    shape as run_fix's result (status, patch, tests, tokens, costs,
    steps, n_edits, n_test_runs, stop_reason, wall_seconds, summary).
    Never raises: an unexpected error is an UNRESOLVED arm."""
    t0 = time.time()
    rec = {"tests": {"command": cmd, "exit_code": None, "failed": []},
           "tokens": {"total": 0, "prompt": 0, "completion": 0},
           "steps": 0, "n_edits": 0, "n_test_runs": 0,
           "stop_reason": None, "wall_seconds": 0.0}
    try:
        if step3:
            state = RazorState(repo, artifacts_dir,
                               test_command=cmd)
            agent = run_loop(repo, prompt, backend, "openai", model,
                             cost_guard=_make_guard(arm_cap),
                             tool_defs=ablation_tool_defs(),
                             tool_runner=make_razor_runner(state),
                             system_text=step3_system(),
                             mask_recent_n=MASK_KEEP_RECENT,
                             mask_sink=lambda t, c: state.new_history(c),
                             guard_tools=GUARD_TOOLS,
                             guard_start_step=GUARD_START)
        else:
            agent = run_loop(repo, prompt, backend, "openai", model,
                             cost_guard=_make_guard(arm_cap))
        rec["tokens"] = {
            "total": agent.get("prompt_tokens", 0)
            + agent.get("completion_tokens", 0),
            "prompt": agent.get("prompt_tokens", 0),
            "completion": agent.get("completion_tokens", 0)}
        rec["steps"] = agent.get("steps", 0)
        rec["n_edits"] = agent.get("n_edits", 0)
        rec["n_test_runs"] = agent.get("n_test_runs", 0)
        rec["stop_reason"] = agent.get("stop_reason")
        rec["wall_seconds"] = round(time.time() - t0, 1)
        rec["cost_usd"] = cost_of(rec["tokens"]["prompt"],
                                  rec["tokens"]["completion"])
        usage_totals = agent.get("usage_totals") or {}
        rec["billed_cost_usd"] = usage_totals.get(
            "billed_cost_usd", rec["cost_usd"])
        if agent.get("crashed"):
            rec.update({"status": "UNRESOLVED", "patch": None,
                        "summary": "The fix loop crashed: "
                        f"{str(agent['crashed'])[:300]}"})
            return rec
        if agent.get("stop_reason") == "cost_cap_excluded":
            rec.update({"status": "COST_CAPPED", "patch": None,
                        "summary": "Stopped at the arm cost cap "
                        f"(${arm_cap:.2f}) before finishing."})
            return rec
        if not agent.get("valid"):
            rec.update({"status": "UNRESOLVED", "patch": None,
                        "summary": "The agent stopped without editing "
                        "or testing anything."})
            return rec
        rc, tail = run_tests(repo, cmd)
        rec["tests"]["exit_code"] = rc
        rec["tests"]["failed"] = _parse_failed(tail)
        if rc != 0:
            rec.update({"status": "UNRESOLVED", "patch": None,
                        "summary": "The fix did not make the tests "
                        "pass; no patch returned."})
            return rec
        r = sh("git diff HEAD", cwd=repo, timeout=300)
        patch = (r.stdout or "") if r.returncode == 0 else ""
        if not patch.strip():
            rec.update({"status": "UNRESOLVED", "patch": None,
                        "summary": "Tests pass but no diff was found "
                        "(flaky test run?); no patch returned."})
            return rec
        rec.update({"status": "SOLVED", "patch": patch,
                    "summary": "Fixed; the test command passes "
                    f"(exit 0, {rec['n_edits']} edit(s), "
                    f"{rec['n_test_runs']} test run(s))."})
        return rec
    except Exception as e:  # never raise out of an arm
        rec["wall_seconds"] = round(time.time() - t0, 1)
        rec.update({"status": "UNRESOLVED", "patch": None,
                    "summary": f"Arm error: {e}"[:300]})
        return rec


def _ratio(num, den):
    if not den:
        return None
    return round(num / den, 4)


def run_compare(repo_path, issue_text, failing_tests=None,
                test_command=None, cost_cap_usd=DEFAULT_CAP_USD,
                model=MODEL, api_key=None, backend=None,
                scratch_root=None, keep_scratch=False):
    """Run the naive loop and the Step 3 loop on one bug. Returns the
    contract result dict (see the module docstring). Never raises for a
    bad input or a failed run; those are statuses in the result."""
    t0 = time.time()
    if not repo_path or not os.path.isdir(repo_path):
        return _setup_fail(f"repo not found: {repo_path}")
    if not (issue_text or "").strip():
        return _setup_fail("issue_text is empty")
    if model != MODEL:
        return _setup_fail(f"v1 supports only {MODEL}, got {model}")
    if not cost_cap_usd or cost_cap_usd <= 0:
        return _setup_fail("cost_cap_usd must be > 0")
    if backend is None:
        if os.environ.get("RAZOR_FIX_BACKEND") == "vault":
            # Same validation-only hatch as run_fix (see there).
            try:
                from model import SubprocessBackend
            except Exception:
                return _setup_fail("vault backend unavailable on "
                                   "this machine")
            backend = SubprocessBackend("openai", model)
        else:
            key = api_key or os.environ.get("OPENAI_API_KEY")
            if not key:
                return _setup_fail("no OpenAI key: set OPENAI_API_KEY "
                                   "in the MCP server environment")
            backend = DirectOpenAIBackend(key, model)

    arm_cap = cost_cap_usd / 2
    scratch_root = scratch_root or tempfile.gettempdir()
    attempt_dir = tempfile.mkdtemp(prefix=SCRATCH_PREFIX,
                                   dir=scratch_root)
    repo_naive = os.path.join(attempt_dir, "repo-naive")
    repo_razor = os.path.join(attempt_dir, "repo-razor")
    artifacts_dir = os.path.join(attempt_dir, "artifacts")
    try:
        for dest in (repo_naive, repo_razor):
            try:
                shutil.copytree(repo_path, dest, symlinks=True,
                                ignore=shutil.ignore_patterns(
                                    "__pycache__", "*.pyc", "node_modules",
                                    ".venv", "venv"))
            except Exception as e:
                return _setup_fail(f"repo copy failed: {e}"[:300])
            if not os.path.isdir(os.path.join(dest, ".git")):
                return _setup_fail("not a git repository (v1 requires "
                                   "git for the patch)")
            r = sh("git reset --hard -q HEAD", cwd=dest, timeout=300)
            if r.returncode != 0:
                return _setup_fail("scratch reset failed: "
                                   f"{(r.stderr or '')[-300:]}")
        cmd = test_command or _detect_test_command(repo_naive)
        if not cmd:
            return _setup_fail("no test command: pass --test-command, or "
                               "add pytest config / a tests/ directory")
        cmd = "PYTHONDONTWRITEBYTECODE=1 " + cmd
        names = _resolve_names(repo_naive, failing_tests)
        prompt = _build_prompt(os.path.basename(
            os.path.abspath(repo_path)), issue_text, names, cmd)
        # One shared precheck on the naive copy.
        rc, _tail = run_tests(repo_naive, cmd)
        if rc == 0:
            return _setup_fail("tests already pass on this repo: "
                               "nothing to reproduce")
        if rc == -1:
            return _setup_fail("precheck test run timed out")
        naive = _run_arm(repo_naive, prompt, backend, model, arm_cap,
                         False, cmd, artifacts_dir)
        razor = _run_arm(repo_razor, prompt, backend, model, arm_cap,
                         True, cmd, artifacts_dir)
        naive_tok = naive["tokens"]["total"]
        razor_tok = razor["tokens"]["total"]
        naive_billed = naive["billed_cost_usd"]
        razor_billed = razor["billed_cost_usd"]
        if razor_billed < naive_billed:
            cheaper = "razor"
        elif razor_billed > naive_billed:
            cheaper = "naive"
        else:
            cheaper = "tie"
        if razor["wall_seconds"] < naive["wall_seconds"]:
            faster = "razor"
        elif razor["wall_seconds"] > naive["wall_seconds"]:
            faster = "naive"
        else:
            faster = "tie"
        comparison = {
            "token_ratio": _ratio(razor_tok, naive_tok),
            "cost_ratio": _ratio(razor["cost_usd"], naive["cost_usd"]),
            "billed_ratio": _ratio(razor_billed, naive_billed),
            "solved": {"naive": naive["status"] == "SOLVED",
                       "razor": razor["status"] == "SOLVED"},
            "cheaper": cheaper,
            "faster": faster}
        verdicts = {naive["status"], razor["status"]}
        if verdicts <= {"SOLVED", "UNRESOLVED"}:
            status = "COMPLETE"
        elif verdicts == {"COST_CAPPED"}:
            status = "COST_CAPPED"
        else:
            status = "PARTIAL"
        total_tokens = {
            "total": naive_tok + razor_tok,
            "prompt": naive["tokens"]["prompt"]
            + razor["tokens"]["prompt"],
            "completion": naive["tokens"]["completion"]
            + razor["tokens"]["completion"]}
        if comparison["token_ratio"] is not None:
            summary = (
                f"Naive {naive['status'].lower()}, Razor "
                f"{razor['status'].lower()}; token ratio "
                f"{comparison['token_ratio']}, billed ratio "
                f"{comparison['billed_ratio']}.")
        else:
            summary = (
                f"Naive {naive['status'].lower()}, Razor "
                f"{razor['status'].lower()}; no naive tokens to "
                "compare against.")
        return {"status": status, "naive": naive, "razor": razor,
                "comparison": comparison, "tokens": total_tokens,
                "cost_usd": round(naive["cost_usd"]
                                  + razor["cost_usd"], 8),
                "billed_cost_usd": round(naive_billed + razor_billed, 8),
                "wall_seconds": round(time.time() - t0, 1),
                "summary": summary}
    finally:
        if not keep_scratch:
            shutil.rmtree(attempt_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Run the naive loop and "
                                 "the Step 3 loop on one bug "
                                 "(razor_compare entry point).")
    ap.add_argument("--repo", required=True,
                    help="absolute path to the repo root")
    ap.add_argument("--issue", required=True,
                    help="bug report in the user's words")
    ap.add_argument("--failing-tests", default="",
                    help="comma-separated failing test node IDs")
    ap.add_argument("--test-command", default="",
                    help="test command run from the repo dir")
    ap.add_argument("--cost-cap", type=float, default=DEFAULT_CAP_USD,
                    help="hard total cap in USD for the pair "
                    "(default 1.00)")
    ap.add_argument("--keep-scratch", action="store_true",
                    help="keep the scratch copies for debugging")
    args = ap.parse_args()
    result = run_compare(args.repo, args.issue,
                         failing_tests=[t.strip() for t in
                                        args.failing_tests.split(",")
                                        if t.strip()],
                         test_command=args.test_command or None,
                         cost_cap_usd=args.cost_cap,
                         keep_scratch=args.keep_scratch)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
