# Personal AI Adventure Agent — MVP

A 7-day interactive fiction experience delivered over Telegram, one phone call
and one email. Built for **10 concurrent players**, optimised for observability
and manual intervention rather than scale.

The player knows it is fiction from the start.

---

## Getting started

Requires **Python 3.12**, **Node 20+** (for the CDK CLI), and **GNU Make**.

```bash
git clone <repo> && cd AgentWizard
make setup                      # creates .venv (3.12) and installs everything
cp .env.local.example .env.local
$EDITOR .env.local              # set ADVENTURE_BUDGET_EMAIL
make test lint
make diff                       # review before deploying anything
```

`make setup` is the only bootstrap step. Every other target routes through
`.venv` on its own, so you never need to think about activation state.

| Target | What it does |
|---|---|
| `make setup` | create `.venv` with Python 3.12, install all three requirement files |
| `make test` | pytest |
| `make lint` / `make fmt` | ruff check + format check / format and autofix |
| `make synth` | `cdk synth` |
| `make diff` | `cdk diff` — **always run this first** |
| `make deploy` | `cdk deploy` |
| `make destroy` | `cdk destroy` |
| `make bootstrap` | `cdk bootstrap` (once per account/region) |
| `make clean` | remove `cdk.out`, caches (keeps `.venv`) |

All CDK targets take `ENV=dev` (default) or `ENV=prod`:

```bash
make diff ENV=prod
```

There is no target that deploys both stacks at once, on purpose.

---

## Layout

```
infra/          AWS CDK v2 (Python) — all infrastructure
  app.py          entry point; defines both stacks
  config.py       per-environment settings
  stacks/         the stack definitions
backend/        Python 3.12
  handlers/       Lambda entry points (thin)
  core/           story engine, state, safety validation
  channels/       telegram.py, email.py, voice.py
  story/          arc definitions as YAML data
voice/          Fargate container for the Nova Sonic call session
admin/          React + Vite operator console
scripts/        operator CLI (start arc, stop player, delete player)
tests/          safety layer, state transitions, fallback paths
```

### Python environment

One venv at the repo root covers `infra/`, `backend/`, `scripts/` and
`tests/`. Never install into system Python, and never create a nested venv.
`voice/` is a container and manages its own dependencies in its Dockerfile.

Dependencies are split three ways and pinned exactly:

| File | Purpose |
|---|---|
| `requirements.txt` | backend runtime — **this is what CDK bundles into the Lambda zips**, so keep it lean |
| `requirements-infra.txt` | CDK |
| `requirements-dev.txt` | pytest, ruff, type stubs, moto |

`boto3` is deliberately absent from `requirements.txt`: the `python3.12` Lambda
runtime provides it, and bundling it would add ~20MB to every function. It is
pinned in `requirements-dev.txt` for local parity. If we ever need a newer
boto3 than the runtime ships, move the pin and accept the size.

---

## Environments

Two stacks, both defined in `infra/app.py`, deployed independently.

| | `AdventureAgentDev` | `AdventureAgentProd` |
|---|---|---|
| Data on `cdk destroy` | destroyed | retained |
| DynamoDB deletion protection | off | on |
| Point-in-time recovery | off | on |
| Stack termination protection | off | on |
| Creates the account budget | yes | no (it is account-wide) |

Region is pinned to **us-east-1** in `infra/config.py` and never inherited from
your shell profile — Bedrock Nova Sonic is only available there.

---

## Phase 0 resources

Three resources, nothing more:

**`AWS::Budgets::Budget`** — `adventure-agent-monthly`, $50/month,
account-wide, emailing `ADVENTURE_BUDGET_EMAIL` at 50% / 80% / 100% actual and
100% forecast. Not filtered by tag: tag-scoped budgets need the cost-allocation
tag activated in the Billing console and take ~24h to start collecting, which
would leave the first day of spend unguarded.

**`AWS::DynamoDB::GlobalTable`** — the single table, `adventure-agent-{env}`.
It renders as a `GlobalTable` because it uses the `TableV2` construct; with one
replica it behaves exactly like a regional table. On-demand billing. TTL on
`ttl`. One GSI.

Access patterns:

```
PLAYER#<player_id>   PROFILE                the player record
PLAYER#<player_id>   EVENT#<ts>#<id>        timeline: every in/outbound event
PLAYER#<player_id>   BEAT#<beat_id>         per-beat state and outcome
PLAYER#<player_id>   QUOTA#<yyyy-mm-dd>     daily send counters (ttl'd)
CHAT#<channel>#<id>  PLAYER                 inbound channel -> player lookup

gsi1:  gsi1pk = STATUS#<status>,  gsi1sk = <last_contact_iso>
       -> the admin panel player list, ordered by staleness
```

**`AWS::SecretsManager::Secret`** — `adventure-agent/{env}`, one JSON blob
holding every third-party credential. One secret rather than five because
Secrets Manager bills $0.40 per secret per month. Created with empty
placeholders; `telegram_webhook_secret` is generated by Secrets Manager so it
never has to be invented or typed. Generated values are produced once at create
time — a later deploy will not overwrite hand-entered values.

Fill it in after the first deploy:

```bash
aws secretsmanager get-secret-value --secret-id adventure-agent/dev \
  --query SecretString --output text
aws secretsmanager put-secret-value --secret-id adventure-agent/dev \
  --secret-string file://secret.json     # then delete secret.json
```

---

## Rules that are not negotiable

These are enforced in code, never left to the model:

- `STOP` halts the story instantly on any channel, in any casing.
  `PAUSE` / `RESUME` suspend and continue.
- No outbound message or call between 22:00 and 08:00 in the player's timezone.
- Max 6 outbound messages per player per day; max 1 call per arc.
- Every in-character message carries an out-of-character footer, and `/real`
  always returns a plain statement of what this is and how to stop.
- Every generated message passes a validation gate before it is sent. Failures
  are logged, flagged in the admin panel, and replaced with a safe pre-written
  line.
- EXIF is stripped from every uploaded image on ingest. GPS coordinates are
  never persisted — the geofence is evaluated in the Lambda and only a boolean
  plus a place label is stored.

No secrets, tokens or phone numbers in the repo or in git history.
`.env.local` is git-ignored; everything real lives in Secrets Manager.

---

## Gotchas

- **Destroying and immediately redeploying dev** will fail on the secret name:
  deleted secrets sit in a 7–30 day recovery window that blocks reuse of the
  name. Force it through with
  `aws secretsmanager delete-secret --secret-id adventure-agent/dev --force-delete-without-recovery`.
- **CDK deprecation warnings about `TableGrantsProps`** come from inside
  `aws-cdk-lib`'s own `TableV2` implementation, not from this repo. Ignore them.
- On Windows the CDK CLI spawns the app through `cmd.exe`, which rejects
  forward slashes in the interpreter path. The Makefile handles this; a
  hand-typed `cdk --app ".venv/Scripts/python.exe ..."` will not work.
