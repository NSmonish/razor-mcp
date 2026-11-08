#!/usr/bin/env python3
"""Per-issue sandbox: setup (clone + venv + install + fix test files),
prompt building, precheck, and grading by test runs. Mirrors the proven
baseline driver (run_issue.py) exactly, including its two httpx rules:
failing_tests_parent is NEVER trusted for httpx (FAILING_OVERRIDE governs
the prompt), and the pytest-asyncio deprecation flag is added to the
httpx test command only. Dev JSONs are read, never written.
"""
import json
import os
import subprocess
import time

DEV_DIR = "/home/hatch/workspace/razor-pipeline/results-eval/dev"

# Failing-test names the issue JSON gets wrong (Step 1 data defect: both
# httpx JSONs copy fastapi-32240026's failing list + run counts). Derived
# from each issue's own test_diff (added test functions), NOT from the
# gold source patch. Grading does not use this list: it runs the full
# scoped test files and requires exit 0.
FAILING_OVERRIDE = {
    "httpx-99cba6ac": ["tests/test_auth.py::test_digest_auth_rfc_2069"],
    "httpx-a682f6f1": ["tests/test_asgi.py::test_asgi_exc_no_raise"],
}


def sh(cmd, cwd=None, timeout=900):
    return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                          text=True, timeout=timeout)


def load_issue(iid):
    with open(os.path.join(DEV_DIR, iid + ".json")) as f:
        return json.load(f)


def test_cmd(issue, venv):
    cmd = issue["test_command"].replace("<venv>", venv)
    files = " ".join(issue["test_files"])
    cmd = cmd.replace("<test_files>", files)
    extra = issue.get("pytest_extra_args") or []
    if extra:
        cmd = cmd.replace(files, " ".join(extra) + " " + files)
    return cmd


def issue_cmd(issue, venv):
    """Test command for an arm, with the httpx env fix applied (httpx only:
    pytest-asyncio deprecation warning turned into an INTERNALERROR by
    httpx's filterwarnings=error)."""
    cmd = test_cmd(issue, venv)
    if issue["issue_id"].startswith("httpx-"):
        cmd = cmd + " -W ignore::pytest.PytestDeprecationWarning"
    return cmd


def failing_names(issue):
    if issue["issue_id"] in FAILING_OVERRIDE:
        return FAILING_OVERRIDE[issue["issue_id"]]
    return issue["failing_tests_parent"]


def resolve_failing_names(repo, issue):
    """Resolve failing-test node IDs against a repo checkout.

    Returns (names, fallback): names are pytest CLI args whose file
    part exists under repo; fallback is True when NO name resolved,
    meaning the caller must run the scoped test files instead.

    Why this exists (Phase 0 bug fix): failing_names() returns the
    parent list verbatim, and the auto-test path used to paste those
    strings raw into a pytest command with no validation. A node ID
    whose file part does not exist under the repo root makes pytest
    exit 4 (usage error: file or directory not found) with zero
    FAILED lines, which the auto-test parser can only report as
    "still failing" with no test names — degraded, unactionable
    feedback. That is exactly the Part A evidence (15 of 19
    auto-test runs exit 4, zero parsed names; only the httpx arms,
    whose override list holds valid relative node IDs, gave clean
    signals). The exact defect shape inside the real parent lists
    was NOT inspected (no issue data is opened in this phase), so
    this resolver covers the whole unresolvable-arg class rather
    than one guessed shape:
      - a name is kept when its file part exists under repo;
      - otherwise leading path components are stripped one at a
        time and retried (parent lists sometimes carry a repo-name
        prefix, e.g. pkg/tests/... instead of tests/...);
      - names that still do not resolve are dropped, never passed
        to pytest.
    Safety: absolute paths and ".." components are rejected outright
    (a CLI arg must never escape the checkout). Repair risk, stated
    honestly: stripping could in theory resolve to a different file
    than intended if a repo held two same-named test trees; these
    checkouts hold one test tree, the repaired name's file path
    appears verbatim in any FAILED line so a wrong file is visible,
    and final grading (full scoped files) is unaffected.
    """
    resolved = []
    for n in failing_names(issue):
        if not n:
            continue
        file_part, sep, rest = n.partition("::")
        # Reject args that could escape the checkout.
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
    if resolved:
        return resolved, False
    return [], True


def build_prompt(issue, venv, cmd):
    names = failing_names(issue)
    shown = "\n".join(f"- {n}" for n in names[:20])
    return (
        f"Repository: {issue['repo']} (checked out at the buggy commit).\n"
        f"Issue: {issue['subject']}\n\n"
        f"Failing test(s) to fix:\n{shown}\n\n"
        f"Test files involved: {', '.join(issue['test_files'])}\n"
        f"Run the tests with this command (from the repo directory):\n{cmd}\n\n"
        "Your task: fix the source code so the failing test(s) pass and no "
        "other test in these files breaks. Read the failing test code and "
        "the relevant source files first, reproduce the failure with the "
        "test command, make a minimal edit, then re-run the tests before "
        "calling finish. Do not edit test files."
    )


def setup(issue, attempt_dir):
    """Fresh clone at parent, fix commit's test files, fresh venv, install.
    Returns (repo, venv) or raises. Install runs with cwd=repo and retries
    in place up to 3 times (PyPI index flaps on this VM)."""
    repo = os.path.join(attempt_dir, "repo")
    venv = os.path.join(attempt_dir, "venv")
    r = sh(f"git clone -q {issue['repo_url']} {repo}", timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"clone failed: {(r.stderr or '')[-300:]}")
    r = sh(f"git checkout -q {issue['base_commit']}", cwd=repo, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"base checkout failed: {(r.stderr or '')[-300:]}")
    files = " ".join(issue["test_files"])
    r = sh(f"git checkout -q {issue['fix_commit']} -- {files}", cwd=repo,
           timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"test-file checkout failed: {(r.stderr or '')[-300:]}")
    inst = issue["install_command"].replace("<venv>", venv)
    last = None
    for tries in range(3):
        r = sh(inst, cwd=repo, timeout=900)
        if r.returncode == 0:
            return repo, venv
        last = r
        if tries < 2:
            time.sleep(60)
    raise RuntimeError(f"install failed after 3 tries: "
                       f"{(last.stdout or '')[-500:]}"
                       f"{(last.stderr or '')[-500:]}")


def run_tests(repo, cmd, timeout=600):
    try:
        r = sh(cmd, cwd=repo, timeout=timeout)
        tail = ((r.stdout or "") + (r.stderr or ""))[-1500:]
        return r.returncode, tail
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
