"""Build the Lambda deployment asset without Docker.

CDK's usual Python bundling shells out to Docker. Nothing else in this project
needs Docker, and requiring a running daemon to run ``cdk diff`` is a bad trade
for a two-package dependency list.

Instead this installs ``requirements.txt`` into a staging directory with pip's
cross-platform flags -- ``--platform manylinux2014_x86_64 --only-binary=:all:
--python-version 3.12`` -- so a Windows or macOS machine produces the same
Linux-compatible wheels Lambda needs. The ``backend`` package is then copied in
alongside them.

The install is skipped when nothing has changed, keyed on a hash of
``requirements.txt`` plus the target platform.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BUILD_ROOT = REPO_ROOT / ".build"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
BACKEND = REPO_ROOT / "backend"

# Must match the Lambda runtime in the stack and .python-version.
PYTHON_VERSION = "3.12"
PLATFORM = "manylinux2014_x86_64"

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".pytest_cache")


def _requirements_fingerprint() -> str:
    payload = REQUIREMENTS.read_bytes() + f"{PLATFORM}:{PYTHON_VERSION}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _install_dependencies(target: Path) -> None:
    stamp = target / ".requirements-sha"
    fingerprint = _requirements_fingerprint()
    if stamp.exists() and stamp.read_text(encoding="utf-8").strip() == fingerprint:
        return

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--requirement",
            str(REQUIREMENTS),
            "--target",
            str(target),
            # Cross-build for the Lambda runtime regardless of this machine.
            "--platform",
            PLATFORM,
            "--python-version",
            PYTHON_VERSION,
            "--implementation",
            "cp",
            "--only-binary=:all:",
            "--upgrade",
        ],
        check=True,
    )
    stamp.write_text(fingerprint, encoding="utf-8")


def build_lambda_asset(name: str = "backend") -> str:
    """Stage dependencies plus the backend package; return the directory path.

    Called at synth time, so ``cdk diff`` reflects code changes without a
    separate build step.
    """
    deps = BUILD_ROOT / f"{name}-deps"
    _install_dependencies(deps)

    staged_backend = deps / "backend"
    if staged_backend.exists():
        shutil.rmtree(staged_backend)
    shutil.copytree(BACKEND, staged_backend, ignore=_IGNORE)

    return str(deps)
