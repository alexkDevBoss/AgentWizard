"""Shared bootstrap for the operator scripts.

Resolves which environment you are pointing at and sets the same variables the
Lambda gets, so the scripts exercise exactly the same code paths as production.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROJECT = "adventure-agent"


def force_utf8_output() -> None:
    """Print story prose without mangling it.

    Windows consoles default to a legacy code page, which turns em-dashes and
    curly quotes into replacement characters. The operator reads real narrative
    text through these scripts all week, so the console has to handle it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def load_env_local() -> None:
    path = REPO_ROOT / ".env.local"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def bootstrap(env_name: str | None = None) -> str:
    """Point the backend modules at one environment's resources.

    Names are derived, not looked up: they follow the same convention as
    ``infra/config.py``, so this works before the stack outputs are to hand.
    """
    force_utf8_output()
    load_env_local()
    name = env_name or os.environ.get("ADVENTURE_ENV", "dev")

    os.environ["ADVENTURE_ENV"] = name
    os.environ.setdefault("ADVENTURE_TABLE_NAME", f"{PROJECT}-{name}")
    os.environ.setdefault("ADVENTURE_SECRET_NAME", f"{PROJECT}/{name}")
    os.environ.setdefault("AWS_REGION", "us-east-1")
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return name


def add_env_argument(parser) -> None:
    parser.add_argument(
        "--env",
        default=os.environ.get("ADVENTURE_ENV", "dev"),
        choices=["dev", "prod"],
        help="which environment to act on (default: dev)",
    )


def confirm(prompt: str, *, assume_yes: bool = False) -> bool:
    """Destructive actions ask, unless explicitly told not to."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print(
            "refusing to act without --yes in a non-interactive shell", file=sys.stderr
        )
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}
