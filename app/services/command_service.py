import logging
import re
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MemoryCommand:
    action: str          # "remember" | "action" | "done" | "forget"
    memory_type: str     # "fact" | "action" | "preference"
    text: Optional[str]  # content extracted from the command, if any
    confirm: str         # human/voice confirmation string


# Normalize common filler so pattern matching is forgiving.
def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", t.strip().strip(".")).strip()


# ---- pattern table (order matters: most specific first) ----

# "remember [that] X", "note [that] X", "don't forget X", "keep in mind X"
RE_MEMORY_PREFIX = re.compile(
    r"^(?:please\s+)?"
    r"(?:"
    r"remember\s+that\b|remember\b|"
    r"note\s+that\b|note\s+down\b|note\b|"
    r"don['’]t\s+forget\b|keep\s+in\s+mind\b|"
    r"make\s+a\s+note\b"
    r")"
    r"\s*(.*)$",
    re.IGNORECASE,
)

# "action item: X", "to-do: X", "todo: X", "add task: X"
RE_ACTION = re.compile(
    r"^(?:action\s+item\s*[:]?|to[\s-]*do\s*[:]?|todo\s*[:]?|add\s+(?:a\s+)?task\s*[:]?|"
    r"open\s+action\s*[:]?|follow[\s-]*up\s*[:]?)\s+(.+)$",
    re.IGNORECASE,
)

# "done: X" / "mark [that] done: X" / "completed: X"
RE_DONE = re.compile(
    r"^(?:done\s*[:]?|mark\s+(?:that\s+)?done\s*[:]?|completed\s*[:]?|"
    r"finish\s+(?:that\s+)?\s*[:]?)\s+(.+)$",
    re.IGNORECASE,
)

# "forget X", "remove X", "that's wrong", "correct that"
RE_FORGET = re.compile(
    r"^(?:forget\b|remove\b|delete\b|scratch\b|cross\s+out\b|"
    r"that['’]?s\s+(?:wrong|incorrect|not\s+right)\b|"
    r"that\s+was\s+wrong\b|correct\s+that\b|never\s+mind\b)"
    r"\s*(.*)$",
    re.IGNORECASE,
)

# Preference cues, handled like remember but tagged memory_type="preference".
RE_PREFERENCE = re.compile(
    r"^(?:i\s+prefer\b|my\s+preference\s+is\b|i\s+like\b|i\s+usually\b|"
    r"always\s+prefer\b|prefer\s+to\b)\s+(.+)$",
    re.IGNORECASE,
)


def parse_command(text: str) -> Optional[MemoryCommand]:
    """Classify a transcribed utterance as a memory command, if any.

    Returns None when the utterance is ordinary conversation (should be stored
    as a normal transcript segment instead).
    """
    t = _norm(text)
    if not t:
        return None

    # "that's wrong" with empty content -> forget the last stored memory
    m = RE_FORGET.match(t)
    if m:
        content = m.group(1).strip().strip(":").strip()
        return MemoryCommand(
            action="forget",
            memory_type="fact",
            text=content or None,
            confirm=("Okay, I'll forget that." if not content
                     else f"Okay, I'll forget: {content}"),
        )

    m = RE_PREFERENCE.match(t)
    if m:
        content = m.group(1).strip()
        return MemoryCommand(
            action="remember",
            memory_type="preference",
            text=content,
            confirm=f"Got it — I'll remember that you prefer {content}.",
        )

    m = RE_ACTION.match(t)
    if m:
        content = m.group(1).strip()
        return MemoryCommand(
            action="action",
            memory_type="action",
            text=content,
            confirm=f"Noted as an open action item: {content}",
        )

    m = RE_DONE.match(t)
    if m:
        content = m.group(1).strip()
        return MemoryCommand(
            action="done",
            memory_type="action",
            text=content or None,
            confirm=("Marked done." if not content else f"Marked done: {content}"),
        )

    m = RE_MEMORY_PREFIX.match(t)
    if m:
        content = m.group(1).strip()
        if content:
            return MemoryCommand(
                action="remember",
                memory_type="fact",
                text=content,
                confirm=f"Remembered: {content}",
            )

    return None


parse = parse_command