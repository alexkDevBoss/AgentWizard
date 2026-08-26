"""Phase 0 has no behaviour to test yet.

Real tests arrive with the safety layer (Phase 1), the state machine
transitions and the fallback paths (Phase 3), per the spec. This file exists so
`make test` is green from the first commit rather than erroring on an empty
test suite.
"""


def test_repo_layout() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for expected in (
        "Makefile",
        "cdk.json",
        "requirements.txt",
        "infra/app.py",
        ".python-version",
    ):
        assert (root / expected).exists(), f"missing {expected}"


def test_python_version_pin_matches_lambda_runtime() -> None:
    import sys
    from pathlib import Path

    pinned = (
        (Path(__file__).resolve().parent.parent / ".python-version")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert pinned == "3.12"
    assert f"{sys.version_info.major}.{sys.version_info.minor}" == pinned
