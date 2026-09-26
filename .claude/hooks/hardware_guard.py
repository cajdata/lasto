"""PreToolUse guard for lasto: the second layer behind the --live deny rule.

Blocks shell commands that could reach real vehicle hardware: the --live flag
(and abbreviations argparse might accept), PCAN channel names, COM ports,
serial devices, and inline code that opens PCAN or serial directly.

Asks before any shell command that names a safety core path, so a shell edit
can't route around the ask rule on Edit(src/lasto/safety/**).

Asks before launching a subagent or workflow whose instructions mention
hardware or the safety core. Subagents don't run this project's hooks or read
CLAUDE.md, and CLAUDE.md says that work isn't delegated.

Fails closed: if the hook input can't be parsed, the tool call is blocked.
Standard library only, so it runs without the project environment.
"""

from __future__ import annotations

import json
import re
import sys

# Tools that run commands or processes. Every string in their input is scanned.
COMMAND_TOOLS = re.compile(r"^(Bash|PowerShell|Monitor|mcp__terminal__.*)$")

# Tools that hand work to subagents. Every string in their input is scanned.
DELEGATION_TOOLS = re.compile(r"^(Agent|Task|Workflow)$")

BLOCK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"--live"), "the --live flag (real hardware)"),
    (re.compile(r"--l(?:i(?:v)?)?(?![\w-])"), "an abbreviation of --live"),
    (re.compile(r"\bPCAN_[A-Z]+BUS\d+", re.IGNORECASE), "a PCAN channel name"),
    (re.compile(r"\bCOM\d{1,3}\b", re.IGNORECASE), "a COM port"),
    (re.compile(r"/dev/(?:tty|cu\.|rfcomm|serial)", re.IGNORECASE), "a serial device"),
    (re.compile(r"\bPCANBasic\b", re.IGNORECASE), "the PCAN-Basic API"),
    (re.compile(r"\b(?:interface|bustype)\W{0,8}pcan\b", re.IGNORECASE), "a python-can PCAN bus"),
    # python-can can pick the interface from a config file or CAN_INTERFACE, so any inline bus open counts.
    (re.compile(r"\bcan\.(?:interface\.)?Bus\s*\("), "opening a python-can bus"),
    (re.compile(r"\bCAN_INTERFACE\b"), "the python-can interface variable"),
    (re.compile(r"\bserial\.(?:Serial|serial_for_url)\b"), "opening a serial port"),
]

SAFETY_CORE_PATH = re.compile(r"src[\\/]+lasto[\\/]+safety", re.IGNORECASE)
SAFETY_CORE_ANY = re.compile(r"src[\\/]+lasto[\\/]+safety|\blasto\.safety\b|\bsafety core\b", re.IGNORECASE)


def collect_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in collect_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in collect_strings(v)]
    return []


def decide(event: dict) -> tuple[str, str] | None:
    """Return (decision, reason) for a PreToolUse event, or None to stay out of the way."""
    tool = event.get("tool_name")
    if not isinstance(tool, str):
        raise ValueError("hook input has no tool_name")
    delegating = bool(DELEGATION_TOOLS.match(tool))
    if not delegating and not COMMAND_TOOLS.match(tool):
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        raise ValueError(f"{tool} input is not an object")
    text = "\n".join(collect_strings(tool_input))
    if delegating:
        hits = [what for pattern, what in BLOCK_PATTERNS if pattern.search(text)]
        if SAFETY_CORE_ANY.search(text):
            hits.append("the safety core")
        if hits:
            return (
                "ask",
                f"lasto hardware guard: this {tool} call hands a subagent instructions that mention "
                f"{', '.join(hits)}. Subagents don't run this project's hooks or read CLAUDE.md, "
                "and safety core or hardware work isn't delegated.",
            )
        return None
    for pattern, what in BLOCK_PATTERNS:
        match = pattern.search(text)
        if match:
            return (
                "deny",
                f"lasto hardware guard: blocked {tool} command containing {what} "
                f"({match.group(0)!r}). Live hardware commands are run by the user, "
                "never by Claude. Give the user the exact command instead.",
            )
    if SAFETY_CORE_PATH.search(text):
        return (
            "ask",
            "lasto hardware guard: this shell command names a safety core path "
            "(src/lasto/safety). Safety core changes need the user's approval.",
        )
    return None


def main() -> int:
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        result = decide(event)
    except Exception as exc:  # fail closed on anything unexpected
        print(f"lasto hardware guard: could not check this tool call ({exc}); blocking it.", file=sys.stderr)
        return 2
    if result is not None:
        decision, reason = result
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": decision,
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
