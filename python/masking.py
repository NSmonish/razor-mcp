#!/usr/bin/env python3
"""History masking for Step 3 (the replay attack, one lever with the
guard — see loop.py and PROGRESS-step3.md).

Mechanism (following JetBrains' "Complexity Trap" design, arXiv:2508.21433,
where masking all but the last M=10 observations cut cost 50.9-56.1% vs
the raw agent): in the history replayed to the model, older tool results
are replaced by short stubs; the most recent N maskable tool results
stay verbatim. Masking is LOSSLESS BY REFERENCE, not summarization:
every stub carries an artifact id, and the agent re-reads the full
content with razor_read_artifact. Nothing is ever destroyed — it is
moved out of the replayed prefix into artifacts.

Never masked, by construction:
  - the system prompt and the initial task (user) prompt,
  - the agent's own edits (edit_file results) and finish,
  - bypass errors and guard refusals (harness directives),
  - razor_status (small, time-sensitive budget report).
Maskable tools (re-derivable outputs only): razor_search, razor_outline,
razor_read, razor_read_artifact, razor_run_tests.

A result is masked only when masking actually saves: results of 300
chars or fewer stay verbatim (stubs run ~200 chars, so masking a
result over 300 chars always saves >= ~100 chars).

Atomicity: masked messages keep their original role, tool name, and
tool_call_id, so provider invariants (every tool_call answered by a
tool message) still hold. The stored history is never mutated; each
model call gets a freshly built masked copy.
"""
import json

MASK_KEEP_RECENT = 10
MIN_MASK_CHARS = 300

MASKABLE_TOOLS = frozenset({
    "razor_search", "razor_outline", "razor_read",
    "razor_read_artifact", "razor_run_tests",
})


def _summary(tool, content):
    """One-line summary (<=60 chars) extracted from a JSON tool result,
    or '' when the result is not structured JSON."""
    try:
        obj = json.loads(content)
    except Exception:
        return ""
    if not isinstance(obj, dict):
        return ""
    if tool == "razor_read":
        p = obj.get("path", "")
        if p:
            return f"path={p} lines={obj.get('start_line')}-" \
                   f"{obj.get('end_line')}"
    if tool == "razor_search":
        q = obj.get("query", "")
        if q:
            return f"query={q} hits={obj.get('total_hits')}"
    if tool == "razor_outline":
        return f"files={obj.get('total_files')} " \
               f"symbols={obj.get('total_symbols')}"
    if tool == "razor_run_tests":
        return f"exit={obj.get('exit_code')} " \
               f"passed={obj.get('passed')}"
    if tool == "razor_read_artifact":
        return f"artifact={obj.get('artifact_id')} " \
               f"chars={obj.get('total_chars')}"
    return ""


def build_stub(tool, orig_chars, artifact_id, content=""):
    """Short stub for a masked tool result. Always carries the artifact
    id when one exists; without an id it says to re-run the tool (only
    possible when no sink was provided — the Step 3 driver always
    provides one, so id-less stubs should not occur there)."""
    summ = _summary(tool, content)
    if artifact_id:
        base = (f"[masked {tool} ({orig_chars} chars) -> artifact "
                f"{artifact_id}")
        if summ:
            base += f" | {summ[:60]}"
        base += (f" | re-read: razor_read_artifact "
                 f"{{\"artifact_id\": \"{artifact_id}\"}}]")
        return base
    base = f"[masked {tool} ({orig_chars} chars)"
    if summ:
        base += f" | {summ[:60]}"
    base += " | re-run the tool to recover]"
    return base


def masked_view(messages, metas, keep_recent=MASK_KEEP_RECENT, sink=None):
    """Build the history actually sent to the model.

    messages: the loop's stored messages (never mutated).
    metas: parallel list; each entry is None or a dict with keys:
        tool (str), maskable (bool), artifact_id (str|None),
        gemini_prefix (str|None, for gemini-lane tool-result messages).
    keep_recent: number of most recent maskable results kept verbatim.
    sink: optional callable(tool, content) -> artifact_id, called lazily
        the first time a result without an artifact id is masked; the id
        is memoized into the meta so later views reuse it.

    Returns (view, masked): view is a new message list; masked is a list
    of (msg_index, orig_chars, stub_chars) for results masked in this
    view (including results masked in earlier views too — the caller
    dedupes by index when counting distinct masked results).
    """
    # Indices of maskable tool-result messages, in order.
    maskable_idx = [i for i, m in enumerate(metas)
                    if m and m.get("maskable")]
    keep = set(maskable_idx[-keep_recent:] if keep_recent > 0 else [])
    view = []
    masked = []
    for i, (msg, meta) in enumerate(zip(messages, metas)):
        if meta and meta.get("maskable") and i not in keep:
            content = msg.get("content", "")
            if len(content) <= MIN_MASK_CHARS:
                view.append(msg)
                continue
            aid = meta.get("artifact_id")
            if aid is None and sink is not None:
                aid = sink(meta["tool"], content)
                meta["artifact_id"] = aid
            stub = build_stub(meta["tool"], len(content), aid, content)
            new = dict(msg)
            prefix = meta.get("gemini_prefix")
            new["content"] = (prefix + stub) if prefix else stub
            view.append(new)
            masked.append((i, len(content), len(new["content"])))
        else:
            view.append(msg)
    return view, masked
