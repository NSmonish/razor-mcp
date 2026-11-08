#!/usr/bin/env python3
"""Telemetry: token anatomy (chars/4 estimator, same method as the
baseline), spend math, and ledger helpers. Anatomy numbers are ESTIMATED
via chars/4 except the provider prompt-token total; replay = total minus
new content, floored at 0.
"""
import json
import os

PRICE_IN = 0.25   # $/M tokens (GPT-5 mini)
PRICE_OUT = 2.00  # $/M tokens (GPT-5 mini)


def anatomy(result):
    ac = result["anatomy_chars"]
    fixed = ac["system_plus_issue"] / 4
    reads = ac["reads"] / 4
    logs = ac["test_logs"] / 4
    other = (ac["edits_and_other_tool_outputs"]
             + ac["assistant_text_and_tool_args"]) / 4
    # Step 4: state messages are harness-generated fresh content sent
    # every call; without subtracting them they would inflate replay.
    # Absent (Steps 1-3) the term is 0 and the formula is unchanged.
    state = ac.get("state_message_chars", 0) / 4
    # Step 3 + auto-test: auto-test summaries are harness-generated
    # fresh content (user messages, never masked); same treatment.
    # Absent (all pre-auto-test records) the term is 0.
    auto_test = ac.get("auto_test_chars", 0) / 4
    # Step 3 + PAUSE-style plan: the plan message is harness-added
    # fresh content (user message, never masked); same treatment.
    # Absent (all pre-plan records) the term is 0.
    plan = ac.get("plan_chars", 0) / 4
    total = result["prompt_tokens"]
    replay = max(0.0, total - (fixed + reads + logs + other + state
                               + auto_test + plan))
    return {"fixed_system_plus_issue_est": round(fixed),
            "file_reads_est": round(reads),
            "test_logs_est": round(logs),
            "other_new_content_est": round(other),
            "state_message_est": round(state),
            "auto_test_est": round(auto_test),
            "plan_est": round(plan),
            "history_replay_est": round(replay),
            "provider_prompt_tokens_total": total,
            "note": "anatomy estimated via chars/4 except provider total; "
                    "replay = total minus new content, floored at 0"}


def cost_of(prompt_tokens, completion_tokens):
    return round((prompt_tokens * PRICE_IN
                  + completion_tokens * PRICE_OUT) / 1e6, 8)


# ---------------------------------------------------------------------------
# Phase 0 instrumentation (redesign plan, Phase 0: instrument before you
# build). Telemetry only: nothing here touches prompts, tools, masking,
# guard, or grading. Per-call usage breakdowns are captured in loop.py
# from the provider usage dict and aggregated here.
# ---------------------------------------------------------------------------

# GPT-5 mini cached-input price, $/M tokens: 0.1x the input price.
# Source: OpenAI published GPT-5-mini pricing (input $0.25, cached
# input $0.025, output $2.00 per 1M tokens). OpenAI automatic prompt
# caching charges NO separate cache-write premium (unlike Anthropic's
# 1.25x cache-write price cited in the redesign report) — so for this
# lane there is no observable cache-write token category: the usage
# object exposes cached_tokens (reads) but no written-to-cache count,
# and cache_write_tokens is therefore recorded as None, never invented.
PRICE_CACHED_IN = 0.025  # $/M tokens (GPT-5 mini cached input)

# Per-call classification: the measurable-now proxy for the report's
# phase calls (localize / repro / edit / test / finish). The current
# loop has NO phase gates (those arrive in Phase 1), so each model
# call is classified by the tools executed from that call's message:
#   localize: only read tools (search/outline/read/artifact)
#   edit:     only edits (finish alongside is terminal, not a phase)
#   test:     only tests (finish alongside is terminal, not a phase)
#   finish:   a finish call and nothing else
#   text:     no tool calls at all
#   mixed:    one turn spanning two work phases (read+edit,
#             read+test, edit+test)
#   other:    status-only or bypass-only turns (no code-affecting work)
# A finish call alongside work does not change the bucket: finish
# is terminal, not a work phase. razor_status and bypass-error names
# are overhead, not work, and are ignored for the bucket (a turn
# with nothing but overhead is "other").
# "repro" is NOT a Phase 0 class: a failing repro before first edit is
# a Phase 1 gate concept, and Phase 0 must not invent phase gates to
# segment by. True per-phase breakdown is Phase 1's work; this log
# says so explicitly.
READ_CALL_TOOLS = {"razor_search", "razor_outline", "razor_read",
                   "razor_read_artifact"}
CALL_CLASSES = ("localize", "edit", "test", "finish", "text", "mixed",
                "other")


def classify_call(tool_names):
    """Classify one model call by the work phases its executed tools
    span. Single-phase turns take that phase's bucket; multi-phase
    turns are "mixed". Overhead names (razor_status, bypass errors)
    are filtered first, so status+finish is "finish" and a turn with
    nothing but overhead is "other"."""
    if not tool_names:
        return "text"
    work = [n for n in tool_names
            if n in READ_CALL_TOOLS
            or n in ("edit_file", "razor_run_tests", "finish")]
    if not work:
        return "other"
    if set(work) == {"finish"}:
        return "finish"
    phases = set()
    for name in work:
        if name in READ_CALL_TOOLS:
            phases.add("localize")
        elif name == "edit_file":
            phases.add("edit")
        elif name == "razor_run_tests":
            phases.add("test")
    if not phases:
        return "other"
    if len(phases) == 1:
        return phases.pop()
    return "mixed"


def _to_int(value):
    """Coerce a usage value to int, or None when absent/unparseable.
    Booleans are never valid token counts."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_usage(usage):
    """Parse one provider usage dict into a per-call breakdown.

    Cached input tokens come from usage["prompt_tokens_details"]
    ["cached_tokens"]; reasoning tokens from usage
    ["completion_tokens_details"]["reasoning_tokens"]. Either field
    may be absent (older lanes, stub backends, non-OpenAI providers):
    absent is recorded as None, never as 0, so aggregates can
    distinguish "zero cached" from "cache data unknown". Reasoning
    tokens are a SUBSET of completion tokens (OpenAI bills them at
    the output rate as part of completion), never an additive
    category — billed_cost_of_call does not add them again.
    """
    if not isinstance(usage, dict):
        usage = {}
    prompt = _to_int(usage.get("prompt_tokens"))
    completion = _to_int(usage.get("completion_tokens"))
    details_in = usage.get("prompt_tokens_details")
    details_out = usage.get("completion_tokens_details")
    cached = _to_int(details_in.get("cached_tokens")) \
        if isinstance(details_in, dict) else None
    reasoning = _to_int(details_out.get("reasoning_tokens")) \
        if isinstance(details_out, dict) else None
    return {"prompt_tokens": prompt if prompt is not None else 0,
            "completion_tokens": completion if completion is not None else 0,
            "cached_input_tokens": cached,
            "reasoning_tokens": reasoning}


def _cache_known(prompt, cached):
    """True when cached is a usable split of prompt (0 <= cached <=
    prompt). Out-of-range values are data anomalies, not splits."""
    return cached is not None and 0 <= cached <= prompt


def billed_cost_of_call(prompt_tokens, cached_input_tokens,
                        completion_tokens):
    """Billed cost of one call: cached tokens at PRICE_CACHED_IN,
    the remaining prompt at PRICE_IN, completion at PRICE_OUT.
    Reasoning tokens are NOT added (subset of completion).
    Cached unknown (None) falls back to the conservative full-rate
    cost_of — the same figure the paid guard uses."""
    if _cache_known(prompt_tokens, cached_input_tokens):
        fresh = prompt_tokens - cached_input_tokens
        return round((cached_input_tokens * PRICE_CACHED_IN
                      + fresh * PRICE_IN
                      + completion_tokens * PRICE_OUT) / 1e6, 8)
    return cost_of(prompt_tokens, completion_tokens)


def aggregate_calls(per_call_usage, per_call_classes=None):
    """Aggregate per-call breakdowns into per-arm totals.

    Calls without usable cache data contribute their prompt tokens
    to unknown_prompt_tokens (never to cached or fresh), and calls
    with anomalous cached values are counted separately; the cache
    hit rate denominator is cached+fresh over KNOWN calls only.
    Returns JSON-safe dicts (ints, floats, None, str keys only).
    """
    usage = per_call_usage or []
    classes = per_call_classes or ["unclassified"] * len(usage)
    totals = {"calls": len(usage),
              "prompt_tokens": 0,
              "completion_tokens": 0,
              "cached_input_tokens": 0,
              "fresh_input_tokens": 0,
              "unknown_prompt_tokens": 0,
              "reasoning_tokens": 0,
              "reasoning_unknown_calls": 0,
              "cache_known_calls": 0,
              "cache_unknown_calls": 0,
              "cache_anomalous_calls": 0,
              "cache_hit_rate": None,
              "billed_cost_usd": 0.0,
              "cache_read_cost_usd": 0.0,
              "fresh_input_cost_usd": 0.0,
              "output_cost_usd": 0.0,
              "cache_read_share_of_billed": None,
              "fresh_input_share_of_billed": None,
              "output_share_of_billed": None,
              "cache_write_tokens": None,
              "by_class": {}}
    for i, call in enumerate(usage):
        prompt = call.get("prompt_tokens", 0) or 0
        completion = call.get("completion_tokens", 0) or 0
        cached = call.get("cached_input_tokens")
        reasoning = call.get("reasoning_tokens")
        cls = classes[i] if i < len(classes) else "unclassified"
        totals["prompt_tokens"] += prompt
        totals["completion_tokens"] += completion
        if reasoning is None:
            totals["reasoning_unknown_calls"] += 1
        else:
            totals["reasoning_tokens"] += reasoning
        if _cache_known(prompt, cached):
            fresh = prompt - cached
            totals["cached_input_tokens"] += cached
            totals["fresh_input_tokens"] += fresh
            totals["cache_known_calls"] += 1
            totals["cache_read_cost_usd"] += round(
                cached * PRICE_CACHED_IN / 1e6, 8)
            totals["fresh_input_cost_usd"] += round(
                fresh * PRICE_IN / 1e6, 8)
        elif cached is None:
            totals["unknown_prompt_tokens"] += prompt
            totals["cache_unknown_calls"] += 1
            totals["fresh_input_cost_usd"] += round(
                prompt * PRICE_IN / 1e6, 8)
        else:
            totals["unknown_prompt_tokens"] += prompt
            totals["cache_anomalous_calls"] += 1
            totals["fresh_input_cost_usd"] += round(
                prompt * PRICE_IN / 1e6, 8)
        totals["output_cost_usd"] += round(
            completion * PRICE_OUT / 1e6, 8)
        bucket = totals["by_class"].setdefault(
            cls, {"calls": 0, "prompt_tokens": 0,
                  "cached_input_tokens": 0, "fresh_input_tokens": 0,
                  "completion_tokens": 0, "reasoning_tokens": 0,
                  "billed_cost_usd": 0.0})
        bucket["calls"] += 1
        bucket["prompt_tokens"] += prompt
        bucket["completion_tokens"] += completion
        if reasoning is not None:
            bucket["reasoning_tokens"] += reasoning
        if _cache_known(prompt, cached):
            bucket["cached_input_tokens"] += cached
            bucket["fresh_input_tokens"] += prompt - cached
        totals["billed_cost_usd"] += billed_cost_of_call(
            prompt, cached, completion)
    totals["billed_cost_usd"] = round(totals["billed_cost_usd"], 8)
    totals["cache_read_cost_usd"] = round(totals["cache_read_cost_usd"], 8)
    totals["fresh_input_cost_usd"] = round(
        totals["fresh_input_cost_usd"], 8)
    totals["output_cost_usd"] = round(totals["output_cost_usd"], 8)
    known_total = totals["cached_input_tokens"] \
        + totals["fresh_input_tokens"]
    if known_total > 0:
        totals["cache_hit_rate"] = round(
            totals["cached_input_tokens"] / known_total, 4)
    if totals["billed_cost_usd"] > 0:
        totals["cache_read_share_of_billed"] = round(
            totals["cache_read_cost_usd"] / totals["billed_cost_usd"], 4)
        totals["fresh_input_share_of_billed"] = round(
            totals["fresh_input_cost_usd"] / totals["billed_cost_usd"], 4)
        totals["output_share_of_billed"] = round(
            totals["output_cost_usd"] / totals["billed_cost_usd"], 4)
    for bucket in totals["by_class"].values():
        bucket["billed_cost_usd"] = round(bucket["billed_cost_usd"], 8)
    # Per-class billed cost is computed in a second pass so the
    # rounding matches the totals above (kept simple: recompute).
    for i, call in enumerate(usage):
        cls = classes[i] if i < len(classes) else "unclassified"
        totals["by_class"][cls]["billed_cost_usd"] = round(
            totals["by_class"][cls]["billed_cost_usd"]
            + billed_cost_of_call(call.get("prompt_tokens", 0) or 0,
                                  call.get("cached_input_tokens"),
                                  call.get("completion_tokens", 0) or 0),
            8)
    return totals


def charged_spend(records, cost_field=None):
    """Spend accounting with failures charged (report's proof rule).

    Returns total spend plus a split by record status, the spend on
    records that are NOT valid solves (failed, unresolved, excluded,
    capped, crashed, invalid, setup-failed), and cost per verified
    solve with ALL spend charged (total / valid solves). A "valid
    solve" is grade SOLVED with status VALID (or no status, for
    synthetic records). Historical records lack billed breakdowns:
    the spend figure used per record is cost_field when given and
    present, else billed_cost_usd when present, else cost_usd —
    documented here so a mixed ledger never silently mixes bases
    without the reader knowing the rule.
    """
    def _spend(rec):
        if cost_field:
            value = rec.get(cost_field)
            if value is not None:
                return float(value)
        value = rec.get("billed_cost_usd")
        if value is None:
            value = rec.get("cost_usd", 0.0)
        return float(value)

    by_status = {}
    total = 0.0
    solved_spend = 0.0
    solved = 0
    for rec in records:
        spend = _spend(rec)
        status = rec.get("status") or "UNKNOWN"
        by_status[status] = round(by_status.get(status, 0.0) + spend, 8)
        total += spend
        if rec.get("grade") == "SOLVED" \
                and rec.get("status") in ("VALID", None):
            solved += 1
            solved_spend += spend
    total = round(total, 8)
    failed_spend = round(total - solved_spend, 8)
    return {"arms": len(records),
            "total_spend_usd": total,
            "spend_by_status": by_status,
            "failed_and_excluded_spend_usd": failed_spend,
            "valid_solves": solved,
            "cost_per_verified_solve_charged":
                round(total / solved, 8) if solved else None}


def usage_record_fields(agent):
    """Additive ledger fields for a loop result dict (drivers copy
    these verbatim). Existing fields are untouched; this only adds.
    """
    totals = agent.get("usage_totals") or {}
    return {"per_call_usage": agent.get("per_call_usage", []),
            "per_call_classes": agent.get("per_call_classes", []),
            "per_call_tools": agent.get("per_call_tools", []),
            "usage_totals": totals,
            "billed_cost_usd": totals.get("billed_cost_usd", 0.0)}


def records(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def append(path, rec):
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")


def spend_total(path):
    return sum(float(r.get("cost_usd", 0.0)) for r in records(path))


def make_paid_guard(spend_fn):
    """Returns cost_guard(prompt_so_far, completion_so_far) for the paid
    driver. Aborts the arm before the next model call when:
    - G2 single-arm max: arm cost already >= $0.20 (EXCLUDED_COST), or
    - G1 hard ceiling: spend_total + arm_cost + $0.15 (max plausible
      single-call cost) >= $0.99 (COST_CAPPED).
    G1 is what guarantees the $1.00 ceiling: a call is only started when
    spend + arm_cost + 0.15 < 0.99, and no single call can cost more than
    ~$0.117 (400K input x $0.25 + 8K completion x $2.00), so the post-call
    total stays under $0.99 < $1.00. Returns None to proceed, else a
    stop_reason string."""
    def guard(prompt_so_far, completion_so_far):
        arm_cost = cost_of(prompt_so_far, completion_so_far)
        if arm_cost >= 0.20:
            return "cost_cap_excluded"
        if spend_fn() + arm_cost + 0.15 >= 0.99:
            return "cost_cap_ceiling"
        return None
    return guard
