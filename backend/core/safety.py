"""Player-safety controls: command parsing and the out-of-character copy.

Everything in this module is a product safety requirement. It is implemented
as code and covered by tests, never expressed as an instruction to a model.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum

# Filled in from the environment so no real person or organisation is named in
# the repo. The stack sets these; they must be real before a player is
# enrolled -- `/real` is worthless if it names a placeholder.
OPERATOR_NAME = os.environ.get("ADVENTURE_OPERATOR_NAME", "the operator")
OPERATOR_CONTACT = os.environ.get("ADVENTURE_OPERATOR_CONTACT", "")


class Command(StrEnum):
    STOP = "STOP"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    REAL = "REAL"
    START = "START"
    NONE = "NONE"


#: Words that carry no meaning around a control keyword. Stripping them lets
#: "please stop", "stop now" and "stop this" all halt the story, while a
#: sentence like "I had to stop at the lights" still does not.
_FILLER = frozenset(
    {"please", "now", "it", "this", "all", "the", "just", "ok", "okay", "hey"}
)

_KEYWORDS: dict[str, Command] = {
    "stop": Command.STOP,
    "cancel": Command.STOP,
    "quit": Command.STOP,
    "end": Command.STOP,
    "unsubscribe": Command.STOP,
    "pause": Command.PAUSE,
    "hold": Command.PAUSE,
    "resume": Command.RESUME,
    "continue": Command.RESUME,
    "unpause": Command.RESUME,
    "real": Command.REAL,
    "start": Command.START,
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def parse_command(text: str | None) -> tuple[Command, str | None]:
    """Classify an inbound message. Returns the command and any argument.

    Matching is deliberately strict about scope and loose about form: the whole
    message (minus punctuation and filler words) must reduce to a single
    keyword. A keyword buried in a sentence does not trigger -- see
    :func:`mentions_stopping` for that case, which flags for the operator
    instead of acting automatically.
    """
    if not text:
        return Command.NONE, None

    raw = text.strip()

    # Slash commands are unambiguous and may carry an argument.
    if raw.startswith("/"):
        head, _, rest = raw[1:].partition(" ")
        head = head.split("@", 1)[0].lower()  # /start@MyBot in group chats
        command = _KEYWORDS.get(head, Command.NONE)
        return command, (rest.strip() or None)

    words = [w for w in _PUNCT.sub(" ", raw.lower()).split() if w]
    meaningful = [w for w in words if w not in _FILLER]
    if len(meaningful) == 1 and meaningful[0] in _KEYWORDS:
        return _KEYWORDS[meaningful[0]], None
    return Command.NONE, None


#: Phrases that suggest the player wants out but did not send a bare keyword,
#: or is sincerely asking whether the fiction is real. These do not act on
#: their own -- they raise a flag in the admin panel for a human to read.
_CONCERN_PATTERNS = (
    r"\bstop\b",
    r"\bleave me alone\b",
    r"\bi (don'?t|do not) want\b",
    r"\bis this real\b",
    r"\bare you real\b",
    r"\bare you a (real )?(person|human|bot|ai)\b",
    r"\bam i in (danger|trouble)\b",
    r"\bi'?m scared\b",
    r"\bthis is (scaring|freaking) me\b",
    r"\bcall the police\b",
    r"\bnot a game\b",
)
_CONCERN = re.compile("|".join(_CONCERN_PATTERNS), re.IGNORECASE)


def mentions_stopping(text: str | None) -> bool:
    """True when a message deserves an operator's eyes even if it is not a command."""
    return bool(text and _CONCERN.search(text))


# ------------------------------------------------------------ player-facing copy
#
# All of the following is out-of-character. It is plain, unstyled, and never
# passes through a model.

_CONTACT_LINE = f"\nReach a human: {OPERATOR_CONTACT}" if OPERATOR_CONTACT else ""

REAL_TEXT = (
    "This is a work of fiction.\n\n"
    "You are taking part in an interactive story. Every character who messages "
    "or calls you is written and voiced by AI. Nothing in it is a real "
    f"emergency, and nobody is really in trouble.\n\n"
    f"It is run by {OPERATOR_NAME}, who can see these messages.\n\n"
    "Send STOP at any time and it ends immediately. Send PAUSE to put it on "
    "hold and RESUME to pick it back up."
    f"{_CONTACT_LINE}"
)

FOOTER = "— fiction, AI-generated. Send /real for details, STOP to end."

STOP_CONFIRMATION = (
    "Stopped.\n\n"
    "The story is over and you will not be contacted again. This message is "
    "from the system, not from a character.\n\n"
    "Thank you for playing."
    f"{_CONTACT_LINE}"
)

PAUSE_CONFIRMATION = (
    "Paused.\n\n"
    "Nothing more will reach you until you send RESUME. Send STOP if you would "
    "rather end it altogether."
)

RESUME_CONFIRMATION = "Resumed. The story picks up where it left off."

RESUME_WHEN_NOT_PAUSED = "Nothing to resume — the story is already running."

RESUME_AFTER_STOP = (
    "This story has ended and cannot be restarted from here. "
    f"{OPERATOR_NAME} can start a new one if you would like."
)

ALREADY_STOPPED = "Already stopped. You will not be contacted again."

UNKNOWN_CHAT = (
    "This is an interactive fiction bot, and this chat is not enrolled in a "
    "story.\n\n"
    "If you were given a code, send it as: /start YOURCODE"
)

BAD_CODE = "That code was not recognised. Check it and try /start YOURCODE again."

ENROLLED = (
    "You're enrolled.\n\n"
    "Before anything else, the plain facts: this is fiction, the characters "
    "are AI, and STOP ends it instantly at any point. Send /real any time to "
    "see this again.\n\n"
    "It begins shortly."
)

#: Used when generation or validation fails. Never mentions the failure.
SAFE_FALLBACK = "…give me a moment."
