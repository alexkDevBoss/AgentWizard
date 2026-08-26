#!/usr/bin/env python
"""CDK entry point.

Both stacks are always defined so ``cdk ls`` shows the whole picture; you pick
one on the command line:

    make diff              # AdventureAgentDev
    make diff ENV=prod     # AdventureAgentProd

Never run a bare ``cdk deploy`` with no stack name -- that would deploy prod
too.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# cdk.json runs this as `python infra/app.py`, so sys.path[0] is infra/.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def load_env_local() -> None:
    """Read .env.local into os.environ. Deliberately dependency-free.

    Values already present in the real environment win, so CI and one-off
    overrides behave the way you would expect.
    """
    path = REPO_ROOT / ".env.local"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_env_local()

import aws_cdk as cdk  # noqa: E402

from infra.config import ENVIRONMENTS, REGION  # noqa: E402
from infra.stacks import AdventureAgentStack  # noqa: E402

app = cdk.App()

aws_env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    # Pinned, never inherited: Nova Sonic is us-east-1 only.
    region=REGION,
)

for cfg in ENVIRONMENTS.values():
    AdventureAgentStack(app, cfg.stack_name, cfg=cfg, env=aws_env)

app.synth()
