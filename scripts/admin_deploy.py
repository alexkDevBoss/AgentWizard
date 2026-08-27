#!/usr/bin/env python
"""Build and publish the operator console.

    python scripts/admin_deploy.py config     # write admin/public/config.json only
    python scripts/admin_deploy.py deploy     # config + build + upload + invalidate

Configuration is written from the deployed stack outputs rather than committed,
so no Cognito or API identifiers live in the repo and the same source builds
against any environment.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from _common import REPO_ROOT, add_env_argument, bootstrap

ADMIN = REPO_ROOT / "admin"
CONFIG_PATH = ADMIN / "public" / "config.json"

# Stack output key -> config.json key.
OUTPUT_MAP = {
    "ApiEndpoint": "apiBaseUrl",
    "AdminUserPoolId": "userPoolId",
    "AdminUserPoolClientId": "clientId",
    "AdminLoginDomain": "loginDomain",
}


def stack_outputs(env_name: str) -> dict[str, str]:
    import boto3

    stack = f"AdventureAgent{'Prod' if env_name == 'prod' else 'Dev'}"
    cfn = boto3.client("cloudformation")
    try:
        described = cfn.describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:
        raise SystemExit(f"could not read stack {stack}: {exc}") from None
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


def write_config(env_name: str, outputs: dict[str, str]) -> dict:
    missing = [key for key in OUTPUT_MAP if key not in outputs]
    if missing:
        raise SystemExit(
            "stack outputs are missing: "
            + ", ".join(missing)
            + "\nDeploy the stack first (`make deploy`)."
        )

    config = {target: outputs[source] for source, target in OUTPUT_MAP.items()}
    config["region"] = "us-east-1"
    config["env"] = env_name

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {CONFIG_PATH.relative_to(REPO_ROOT)}")
    for key, value in config.items():
        print(f"  {key:<14} {value}")
    return config


def npm(*args: str) -> None:
    executable = shutil.which("npm") or shutil.which("npm.cmd")
    if executable is None:
        raise SystemExit("npm is not on PATH")
    subprocess.run([executable, *args], cwd=ADMIN, check=True)


def upload(outputs: dict[str, str]) -> None:
    import boto3

    bucket = outputs["AdminSiteBucket"]
    distribution = outputs["AdminDistributionId"]
    dist = ADMIN / "dist"
    if not dist.exists():
        raise SystemExit("admin/dist does not exist -- did the build run?")

    s3 = boto3.client("s3")
    uploaded = 0
    for path in sorted(dist.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(dist).as_posix()
        # The hashed asset bundles are immutable; index.html and config.json
        # must never be cached or a deploy would not take effect.
        immutable = key.startswith("assets/")
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": content_type(path),
                "CacheControl": (
                    "public, max-age=31536000, immutable"
                    if immutable
                    else "no-cache, must-revalidate"
                ),
            },
        )
        uploaded += 1
    print(f"uploaded {uploaded} files to s3://{bucket}")

    cloudfront = boto3.client("cloudfront")
    invalidation = cloudfront.create_invalidation(
        DistributionId=distribution,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"admin-deploy-{time.time_ns()}",
        },
    )
    print(f"invalidated {distribution} ({invalidation['Invalidation']['Id']})")


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".map": "application/json",
}


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_env_argument(parser)
    parser.add_argument("command", choices=["config", "deploy"], help="what to do")
    args = parser.parse_args()

    env_name = bootstrap(args.env)
    print(f"[{env_name}]")

    outputs = stack_outputs(env_name)
    write_config(env_name, outputs)

    if args.command == "config":
        print("\nRun `npm run dev` in admin/ to work against this environment.")
        return 0

    print()
    npm("run", "build")
    print()
    upload(outputs)
    print(f"\nconsole: {outputs.get('AdminConsoleUrl', '(no output)')}")
    print("CloudFront can take a minute to serve the new bundle.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
