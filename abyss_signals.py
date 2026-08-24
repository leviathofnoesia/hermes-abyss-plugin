"""Abyss signal classification — Raindrop-style anomaly classifiers.

Extracted from the plugin god-file (Clean Architecture, use-case layer).
``_detect_signals`` runs the signal classifiers on a tool/LLM call result and
``_SIGNAL_PATTERNS`` is the pattern taxonomy. The only core dependency
(``_get_activity_conn`` for loop detection) is imported lazily inside the
function, exactly like abyss_wave.py, so there is no import cycle with
``__init__`` and the module works under any loader name.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

# Failure patterns that Raindrop detects — these are the "silent agent failures"
# that traditional logging misses (per Raindrop docs: silent tool errors,
# "forgetting", vague replies, persona drift, hallucinations, loops).
_SIGNAL_PATTERNS = [
    ("tool_error",      "error",  "error",   "Tool call failed with an error"),
    ("timeout",         "timeout", "warning", "Tool call or operation timed out"),
    ("rate_limit",      "rate_limit", "warning", "API rate limit hit"),
    ("loop_detected",   "loop",   "error",   "Agent appears to be in a loop"),
    ("vague_reply",     "vague",  "warning", "LLM response was vague or unhelpful"),
    ("drift_detected",  "drift",  "warning", "Potential persona drift detected"),
    ("context_loss",    "context", "error",  "Context may have been lost between turns"),
]


def _detect_signals(
    tool_name: str,
    result: Any,
    session_id: str,
    status: str,
    activity_id: int,
    error_type: str = "",
    error_message: str = "",
    duration_ms: int = 0,
) -> list:
    """Run Raindrop-style signal classifiers on a tool call result.

    Returns a list of detected signals. Each signal is dict with:
    signal_type, severity, label, description, details.

    Detection logic:
    1. Error-based: structured ``status == error`` / ``error_type`` set, or
       result text contains error-like keywords
    2. Timeout-based: structured ``error_type`` mentions timeout, or result text
    3. Rate limit: structured ``error_type`` mentions rate/429, or result text
    4. Context loss: structured error fields mention context-window exhaustion
    5. Slow call: structured ``duration_ms`` above threshold (60s)
    6. Loop detection: same tool called with identical args in same session
    7. Vague reply / refusal / persona drift on LLM responses
    """
    signals = []
    result_str = str(result).lower() if result else ""
    err_type_l = (error_type or "").lower()
    err_msg_l = (error_message or "").lower()
    already: set = set()

    # 1b. Benign read_file error suppression (computed before signal detection)
    # "File not found" is an agent's normal exploratory path probing (e.g.
    # probing for config/README/etc.) and not a backend fault. "Access denied:
    # ...credential store" is an intentional defense-in-depth gate, not a real
    # failure. Both flood the signal firehose with tool_error signals; suppress
    # them. Genuine permission errors on real files still classify normally.
    _SUPPRESS_TOOL_ERROR = False
    if tool_name == "read_file" and status == "error":
        _rf_low = err_msg_l + result_str
        if "file not found" in _rf_low or ("access denied" in _rf_low and "credential store" in _rf_low):
            _SUPPRESS_TOOL_ERROR = True

    def _add(signal_type, severity, label, description, details=None):
        if signal_type in already:
            return
        already.add(signal_type)
        signals.append({
            "signal_type": signal_type,
            "severity": severity,
            "label": label,
            "description": description,
            "details": details or {},
        })

    # 0. Exit-code classification. A bare "exit N" error_message carries no
    # diagnostic value yet currently triple-fires (tool_error + timeout +
    # slow_call for exit 124). Map the known codes to a single cause so each
    # event yields exactly one correctly-typed signal.
    _exit_match = re.match(r"^exit (-?\d+)$", (error_message or "").strip())
    _exit_cause = ({"124": "timeout", "127": "command-not-found",
                    "137": "killed", "-1": "killed", "-9": "killed"}.get(_exit_match.group(1))
                   if _exit_match else None)

    # 1. Error detection (structured only). The old text fallback scanned
    # result text on COMPLETED calls for error-like keywords ("error:",
    # "traceback", "*Error" class names) and fired ~127 false tool_error
    # signals on successful calls whose output merely mentioned an error
    # (grep results, build logs, read-file contents, memory entries). Real
    # tool failures always arrive with status=="error" or a structured
    # error_type, so the fallback was pure false-positive noise and is
    # removed. The read_file suppression above now fully silences tool_error.
    if _exit_cause == "timeout":
        pass  # exit 124 is a timeout kill; the timeout branch below owns it
    elif (not _SUPPRESS_TOOL_ERROR) and (status == "error" or err_type_l or "error" in err_type_l):
        if error_message:
            desc = f"Tool '{tool_name}' failed: {error_message[:200]}"
        else:
            desc = f"Tool '{tool_name}' failed with an error state"
        details = {"error_type": error_type, "error_message": error_message}
        if _exit_match:
            details["exit_code"] = _exit_match.group(1)
        # Bare exit codes are downgraded to warning: no diagnostic value.
        _add("tool_error", "warning" if _exit_match else "error", "Tool Error", desc, details)

    # 2. Timeout detection
    # Structured fields first; the result-text fallback requires a strong
    # tool-generated signature. A bare "timed out" substring in free text
    # (a page the agent merely read, log prose) is not evidence the tool call
    # itself timed out and flooded the feed with false positives.
    if _exit_cause == "timeout" or "timeout" in err_type_l or "timeout" in err_msg_l or "timed out" in err_msg_l \
            or "timeout error" in result_str or "operation timed out" in result_str:
        _add("timeout", "warning", "Timeout",
             f"Tool '{tool_name}' operation timed out",
             {"error_type": error_type, "error_message": error_message})

    # 3b. Context-loss / context-window exhaustion (structured evidence only).
    # The taxonomy has declared context_loss since extraction but no classifier
    # emitted it: a provider rejection like "maximum context length exceeded"
    # was lumped into generic tool_error — or worse, mislabeled rate_limit via
    # the "exhausted"/"quota" credit tokens. Context-window exhaustion is its
    # own failure class (the window is full, not the API quota). Only FAILED
    # calls with structured error fields qualify; a completed call whose RESULT
    # merely mentions "context window" (a doc read, log prose) can never fire.
    _CTX_LOSS_TOKENS = (
        "maximum context length", "context length exceeded", "context window",
        "too many tokens", "token limit", "max tokens", "token budget",
    )
    if (status == "error" or err_type_l or err_msg_l) and (
            any(tok in err_msg_l for tok in _CTX_LOSS_TOKENS) \
            or any(tok in err_type_l for tok in _CTX_LOSS_TOKENS)):
        _add("context_loss", "error", "Context Loss",
             f"Context window may have been exhausted during '{tool_name}'",
             {"error_type": error_type, "error_message": error_message})

    # 3. Rate limit detection (structured evidence only).
    # Result-text scanning fired on benign content: 29/37 unresolved
    # rate_limit signals sat on COMPLETED calls with empty structured fields
    # (a read_file of a file that merely mentions "rate"/​"quota", memory
    # entries containing "rate"). A rate limit is a backend error and always
    # arrives via error_type/​error_message. Keep only the unambiguous
    # "429"⁄"too many requests" result tokens, restricted to failed calls.
    #
    # FIX (abyss-fix-rate-limit-local-tools): never classify rate_limit for
    # tools that cannot make API calls. read_file, write_file, patch,
    # search_files, and skill-management tools are pure local filesystem
    # operations — they make zero network/API calls, so a rate_limit
    # emission is always a false positive (e.g. a read_file of a log file
    # whose contents happen to mention "429 too many requests"). Require the
    # evidence in error_type/​error_message (structured API context) rather
    # than in arbitrary stdout, and restrict to tools with a known network
    # backend. See abyss-fix-rate-limit-local-tools skill for verification.
    _LOCAL_ONLY_TOOLS = frozenset({
        "read_file", "write_file", "write_json", "patch", "search_files",
        "skills_list", "skill_view", "skill_manage",
    })
    _RATE_LIMIT_TOKENS = ("429", "too many requests")
    _CREDIT_TOKENS = ("credit", "balance", "exhausted", "payment required",
                      "402", "quota", "insufficient")
    _is_local_only = tool_name in _LOCAL_ONLY_TOOLS
    # Context-loss guard: when context_loss already fired for this call (e.g.
    # "context window exhausted"), the credit tokens above must not ALSO label
    # it rate_limit — the window is full, not the API quota.
    if not _is_local_only and not any(s["signal_type"] == "context_loss" for s in signals) and (
            any(tok in err_msg_l for tok in _RATE_LIMIT_TOKENS) \
            or ("429" in err_type_l or "too many requests" in err_type_l) \
            or (status == "error" and any(tok in result_str for tok in _RATE_LIMIT_TOKENS)) \
            or any(tok in err_msg_l for tok in _CREDIT_TOKENS) \
            or any(tok in err_type_l for tok in _CREDIT_TOKENS)):
        _add("rate_limit", "warning", "Rate Limit",
             f"API rate limit hit during '{tool_name}'",
             {"error_type": error_type, "error_message": error_message})

    # 4. Slow call detection (structured duration)
    if duration_ms and duration_ms > 60000 and _exit_cause != "timeout":
        # A timeout-killed call's duration IS the timeout, not a slow call.
        _add("slow_call", "info", "Slow Call",
             f"Tool '{tool_name}' took {duration_ms / 1000:.1f}s (>60s)",
             {"duration_ms": duration_ms})

    # 5. Loop detection: check if same tool called with same args recently
    if tool_name and session_id:
        from __init__ import _get_activity_conn

        conn = _get_activity_conn()
        try:
            recent = conn.execute(
                """SELECT tool_name, args, timestamp FROM activity
                  WHERE session_id = ? AND tool_name = ? AND id != ?
                  ORDER BY timestamp DESC LIMIT 3""",
                (session_id, tool_name, activity_id)
            ).fetchall()
            if len(recent) >= 2:
                # If same tool called 3+ times in same session with similar args = loop
                args_list = [r["args"] for r in recent]
                if len(set(args_list)) == 1:
                    _add("loop_detected", "error", "Agent Loop",
                         f"Tool '{tool_name}' called {len(recent)+1}x with identical args in same session")
        except sqlite3.Error:
            pass
        finally:
            conn.close()

    # 6. Vague/empty reply detection for LLM results
    if tool_name in ("llm_call_completed",) or "llm" in str(tool_name).lower():
        result_clean = result_str.strip().strip('"\'` \n\r\t').strip()
        if result_clean:
            # Persona drift (taxonomy-declared, previously unemitted): strong
            # first-person identity CLAIMS contradicting the assistant role.
            # Deliberately narrow — "i am an ai" is the CORRECT role and never
            # fires; only affirmative non-assistant claims do.
            if any(token in result_clean for token in (
                "i am not an ai", "i am not ai", "i'm not an ai",
                "i am not a bot", "i am actually a human", "i am a real person",
                "my real name is",
            )):
                _add("drift_detected", "warning", "Persona Drift",
                     "LLM response claims a non-assistant identity (possible persona drift)")
            elif len(result_clean) < 20 and not result_clean.endswith(('.', '!', '?')):
                _add("vague_reply", "warning", "Vague Reply",
                     f"LLM response appears too short or vague ({len(result_clean)} chars)")
            elif any(token in result_clean for token in (
                "i don't know", "i cannot", "i can't", "not sure how", "unable to",
                "i am not able", "sorry, i can't", "i'm sorry, but i can't",
            )):
                _add("refusal", "warning", "LLM Refusal",
                     f"LLM response contains a refusal/unable pattern")
        elif status == "error":
            _add("empty_result", "warning", "Empty Result",
                 f"Tool '{tool_name}' returned no result with error status")

    return signals
