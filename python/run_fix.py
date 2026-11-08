#!/usr/bin/env python3
"""Callable Step 3 fix entry point for the razor_fix MCP tool (Path A).

One bug in, one verified result out. No eval wiring: no issue JSONs, no
ledgers, no pre-registered gates. The loop configuration is the frozen
Step 3 system exactly (budgeted Razor tools as the only read path,
masking keep-10, allocation guard refusing reads at steps >= 23,
30-step budget, GPT-5 mini, temp 0.0).

Contract (harness/MCP-FIX-CONTRACT.md, Nov 7, 2026):
  - Runs locally in a scratch copy of the repo; the user's working tree
    is never touched. The patch is returned as text; applying it is the
    caller's job.
  - The patch is returned ONLY when the scoped tests pass on it after
    the loop (finish verification). Otherwise no patch, only a summary.
  - The user's own OpenAI key, from the OPENAI_API_KEY environment
    variable (or the api_key argument). The key is never logged, stored,
    or sent anywhere except api.openai.com.
  - Hard per-fix cost cap (default $0.50); no run without one.
  - Python-only v1, git repos only (the patch is a git diff).

CLI (what the Node MCP bridge calls in the packaging step):
  python3 run_fix.py --repo /path/to/repo --issue "bug text"
      [--failing-tests tests/test_x.py::test_y,...]
      [--test-command "pytest tests/test_x.py -q"] [--cost-cap 0.50]
  Prints the result JSON to stdout. Exit 0 always carries a result;
  exit 2 is a usage error.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from loop import MAX_STEPS, run_loop  # noqa: E402
from masking import MASK_KEEP_RECENT  # noqa: E402
from razor_tools import (RazorState, ablation_tool_defs,  # noqa: E402
                         make_razor_runner, step3_system)
from sandbox import run_tests, sh  # noqa: E402
from telemetry import cost_of  # noqa: E402

MODEL = "gpt-5-mini"
GUARD_TOOLS = {"razor_search", "razor_outline", "razor_read",
               "razor_read_artifact"}
GUARD_START = MAX_STEPS - 8 + 1  # 23: final 8 steps reserved
GUARD_MARGIN = 0.15  # one model call costs at most ~$0.117; same margin
# as the eval driver's ceiling guard, so the cap is never exceeded.
SCRATCH_PREFIX = "razor-fix-"
DEFAULT_CAP_USD = 0.50


class DirectOpenAIBackend:
    """One model call straight to api.openai.com with the user's own key.

    Same backend interface as model.StubBackend / SubprocessBackend:
    complete(messages, tools, temperature) -> (resp, meta), resp is
    OpenAI-shaped (choices[0].message + usage) or None on failure.
    Retries 429/5xx only (capped waits); 401/403/400 fail immediately.
    The key never appears in an error message, a log, or a result.
    """

    BASE_URL = "https://api.openai.com/v1"

    def __init__(self, api_key, model=MODEL, call_timeout=1200):
        self.api_key = api_key
        self.model = model
        self.call_timeout = call_timeout

    def complete(self, messages, tools, temperature=0.0):
        body = json.dumps({"model": self.model, "messages": messages,
                           "tools": tools, "temperature": temperature})
        t0 = time.time()
        last_err = "unknown error"
        for attempt in range(4):
            req = urllib.request.Request(
                self.BASE_URL + "/chat/completions", method="POST",
                data=body.encode(),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + self.api_key})
            try:
                with urllib.request.urlopen(req,
                                            timeout=self.call_timeout) as r:
                    return (json.loads(r.read().decode()),
                            {"seconds": time.time() - t0})
            except urllib.error.HTTPError as e:
                last_err = f"openai HTTP {e.code}"
                if e.code in (400, 401, 403):
                    return None, {"error": last_err,
                                  "seconds": time.time() - t0}
                if attempt < 3:
                    time.sleep([5, 15, 30][attempt])
            except (urllib.error.URLError, TimeoutError) as e:
                last_err = f"openai request failed: {e}"
                if attempt < 3:
                    time.sleep([5, 15, 30][attempt])
        return None, {"error": last_err, "seconds": time.time() - t0}


def _resolve_names(repo, names):
    """Keep failing-test names whose file part exists under repo.

    Mirrors sandbox.resolve_failing_names: absolute paths and ".."
    components are rejected; otherwise leading path components are
    stripped one at a time and retried. Unresolvable names are dropped,
    never passed to a test command.
    """
    resolved = []
    for n in names or []:
        if not n:
            continue
        file_part, sep, rest = n.partition("::")
        if not file_part or os.path.isabs(file_part):
            continue
        if ".." in file_part.split("/"):
            continue
        cand = file_part
        while True:
            if os.path.isfile(os.path.join(repo, cand)):
                resolved.append(cand + (sep + rest if sep else ""))
                break
            if "/" not in cand:
                break
            cand = cand.split("/", 1)[1]
    return resolved


def _detect_test_command(repo):
    """Default test command, or None when nothing is detectable.

    pytest -q when the repo has pytest config or a tests/ directory.
    The command must work from the repo dir in the caller's environment;
    v1 does no dependency setup.
    """
    markers = ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini")
    if any(os.path.isfile(os.path.join(repo, m)) for m in markers):
        return "pytest -q"
    if os.path.isdir(os.path.join(repo, "tests")):
        return "pytest -q"
    if os.path.isdir(os.path.join(repo, "test")):
        return "pytest -q"
    return None


def _build_prompt(repo_name, issue_text, failing_names, cmd):
    shown = "\n".join(f"- {n}" for n in failing_names[:20])
    if not shown:
        shown = "- (none listed; reproduce with the test command below)"
    return (
        f"Repository: {repo_name}.\n"
        f"Issue: {issue_text}\n\n"
        f"Failing test(s) to fix:\n{shown}\n\n"
        f"Run the tests with this command (from the repo directory):\n"
        f"{cmd}\n\n"
        "Your task: fix the source code so the failing test(s) pass and no "
        "other test run by this command breaks. Read the failing test code "
        "and the relevant source files first, reproduce the failure with "
        "the test command, make a minimal edit, then re-run the tests "
        "before calling finish. Do not edit test files."
    )


def _parse_failed(tail):
    return [m.group(1) for m in
            re.finditer(r"^FAILED\s+(\S+)", tail or "", re.M)][:20]


def _setup_fail(reason):
    return {"status": "SETUP_FAIL", "patch": None,
            "tests": {"command": None, "exit_code": None, "failed": []},
            "tokens": {"total": 0, "prompt": 0, "completion": 0},
            "cost_usd": 0.0, "billed_cost_usd": 0.0, "steps": 0,
            "n_edits": 0, "n_test_runs": 0, "stop_reason": "setup_fail",
            "wall_seconds": 0.0, "summary": reason}


def _make_guard(cost_cap_usd):
    def guard(prompt_so_far, completion_so_far):
        arm_cost = cost_of(prompt_so_far, completion_so_far)
        if arm_cost + GUARD_MARGIN >= cost_cap_usd - 0.01:
            return "cost_cap_excluded"
        return None
    return guard


def run_fix(repo_path, issue_text, failing_tests=None, test_command=None,
            cost_cap_usd=DEFAULT_CAP_USD, model=MODEL, api_key=None,
            backend=None, scratch_root=None, keep_scratch=False):
    """Run the frozen Step 3 loop on one bug. Returns the contract result
    dict (see the module docstring). Never raises for a bad input or a
    failed run; those are statuses in the result."""
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
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            return _setup_fail("no OpenAI key: set OPENAI_API_KEY "
                               "in the MCP server environment")
        backend = DirectOpenAIBackend(key, model)

    scratch_root = scratch_root or tempfile.gettempdir()
    attempt_dir = tempfile.mkdtemp(prefix=SCRATCH_PREFIX,
                                   dir=scratch_root)
    repo = os.path.join(attempt_dir, "repo")
    try:
        try:
            shutil.copytree(repo_path, repo, symlinks=True,
                            ignore=shutil.ignore_patterns(
                                "__pycache__", "*.pyc", "node_modules",
                                ".venv", "venv"))
        except Exception as e:
            return _setup_fail(f"repo copy failed: {e}"[:300])
        if not os.path.isdir(os.path.join(repo, ".git")):
            return _setup_fail("not a git repository (v1 requires git "
                               "for the patch)")
        r = sh("git reset --hard -q HEAD", cwd=repo, timeout=300)
        if r.returncode != 0:
            return _setup_fail("scratch reset failed: "
                               f"{(r.stderr or '')[-300:]}")
        cmd = test_command or _detect_test_command(repo)
        if not cmd:
            return _setup_fail("no test command: pass --test-command, or "
                               "add pytest config / a tests/ directory")
        # Bytecode staleness: CPython validates a .pyc by source mtime
        # in seconds + file size, so an edit made in the same second
        # with the same size leaves the pre-edit bytecode valid, and a
        # later test run imports the old code (caught by the unit test:
        # a fixed calc.py still returned -1). The eval harness carries
        # this as open issue O1; the product path avoids it by never
        # writing bytecode for test runs. The prefixed command is what
        # runs everywhere: precheck, the loop's razor_run_tests, grading.
        cmd = "PYTHONDONTWRITEBYTECODE=1 " + cmd
        names = _resolve_names(repo, failing_tests)
        prompt = _build_prompt(os.path.basename(
            os.path.abspath(repo_path)), issue_text, names, cmd)
        rc, tail = run_tests(repo, cmd)
        if rc == 0:
            return _setup_fail("tests already pass on this repo: "
                               "nothing to reproduce")
        if rc == -1:
            return _setup_fail("precheck test run timed out")
        state = RazorState(repo, os.path.join(attempt_dir, "artifacts"),
                           test_command=cmd)
        agent = run_loop(repo, prompt, backend, "openai", model,
                         cost_guard=_make_guard(cost_cap_usd),
                         tool_defs=ablation_tool_defs(),
                         tool_runner=make_razor_runner(state),
                         system_text=step3_system(),
                         mask_recent_n=MASK_KEEP_RECENT,
                         mask_sink=lambda t, c: state.new_history(c),
                         guard_tools=GUARD_TOOLS,
                         guard_start_step=GUARD_START)
        rec = {"tests": {"command": cmd, "exit_code": None, "failed": []},
               "tokens": {"total": agent.get("prompt_tokens", 0)
                          + agent.get("completion_tokens", 0),
                          "prompt": agent.get("prompt_tokens", 0),
                          "completion": agent.get("completion_tokens", 0)},
               "steps": agent.get("steps", 0),
               "n_edits": agent.get("n_edits", 0),
               "n_test_runs": agent.get("n_test_runs", 0),
               "stop_reason": agent.get("stop_reason"),
               "wall_seconds": round(time.time() - t0, 1)}
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
                        "summary": "Stopped at the cost cap "
                        f"(${cost_cap_usd:.2f}) before finishing."})
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
    finally:
        if not keep_scratch:
            shutil.rmtree(attempt_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="Run the Step 3 fix loop "
                                 "on one bug (razor_fix entry point).")
    ap.add_argument("--repo", required=True,
                    help="absolute path to the repo root")
    ap.add_argument("--issue", required=True,
                    help="bug report in the user's words")
    ap.add_argument("--failing-tests", default="",
                    help="comma-separated failing test node IDs")
    ap.add_argument("--test-command", default="",
                    help="test command run from the repo dir")
    ap.add_argument("--cost-cap", type=float, default=DEFAULT_CAP_USD,
                    help="hard per-fix cap in USD (default 0.50)")
    ap.add_argument("--keep-scratch", action="store_true",
                    help="keep the scratch copy for debugging")
    args = ap.parse_args()
    result = run_fix(args.repo, args.issue,
                     failing_tests=[t.strip() for t in
                                    args.failing_tests.split(",") if t.strip()],
                     test_command=args.test_command or None,
                     cost_cap_usd=args.cost_cap,
                     keep_scratch=args.keep_scratch)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
