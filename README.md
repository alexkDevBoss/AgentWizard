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

## Phase 1 resources

On top of Phase 0:

**`AWS::ApiGatewayV2::Api`** — one route, `POST /telegram/webhook`. The default
stage is throttled to 20 req/s (burst 10). Ten players cannot generate more
than a trickle; anything above that is a bug or someone hammering the endpoint,
and neither should be able to run up a bill.

**`AWS::Lambda::Function`** — `adventure-agent-{env}-telegram-webhook`, Python
3.12, 512 MB, 30s timeout, **reserved concurrency 10**. Its log group is a real
resource with one-week retention in dev.

The Lambda bundle is built by `infra/bundling.py` without Docker: pip installs
`requirements.txt` with `--platform manylinux2014_x86_64 --only-binary=:all:
--python-version 3.12`, so a Windows machine still produces Linux wheels.
Verify with `ls .build/backend-deps/yaml/` — you should see
`_yaml.cpython-312-x86_64-linux-gnu.so` and no `.pyd`.

---

## Wiring up the bot

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Put it in the secret (the file is deleted immediately after):

   ```bash
   aws secretsmanager get-secret-value --secret-id adventure-agent/dev --query SecretString --output text > s.json
   # edit s.json, fill in telegram_bot_token
   aws secretsmanager put-secret-value --secret-id adventure-agent/dev --secret-string file://s.json
   rm s.json
   ```

3. Set `ADVENTURE_OPERATOR_NAME` in `.env.local` and redeploy. **This is not
   cosmetic** — it is what `/real` tells a player when they ask who is running
   this, and a placeholder there is a safety failure.
4. Register the webhook:

   ```bash
   .venv/Scripts/python.exe scripts/telegram_setup.py whoami
   .venv/Scripts/python.exe scripts/telegram_setup.py set     # URL from stack outputs
   .venv/Scripts/python.exe scripts/telegram_setup.py info
   ```

5. Enrol yourself and send the code to the bot:

   ```bash
   .venv/Scripts/python.exe scripts/player.py add --name "Alex" --tz Europe/London
   # -> /start AB3KD9XY
   ```

---

## Operator CLI

```bash
python scripts/player.py add --name "Alex" --tz Europe/London   # mint an enrolment code
python scripts/player.py list                                   # every player, one line each
python scripts/player.py show plr_xxx                           # profile + full timeline
python scripts/player.py stop plr_xxx --reason "..."            # halt immediately
python scripts/player.py pause plr_xxx / resume plr_xxx
python scripts/player.py delete plr_xxx --yes                   # player and all their data
```

All of it runs through the same `backend/core` code the Lambda uses, so an
operator STOP is the same STOP a player gets. Add `--env prod` to target prod.

---

## How the safety layer is built

`backend/core/dispatch.py` is the **single outbound chokepoint**. Nothing else
may call a channel's send method. Four gates, in this order:

| # | Gate | Why it is where it is |
|---|---|---|
| 1 | player status | a stopped or paused player never receives story content |
| 2 | quiet hours | checked before anything is spent, so a deferred beat keeps its allowance |
| 3 | content validation | a failing message is replaced, not sent |
| 4 | daily rate limit | claimed last, so only a message that will actually go out burns one of the six |

**System replies skip gates 2–4, deliberately.** The spec requires STOP to work
"at any point" *and* forbids outbound messages between 22:00 and 08:00. Those
two rules conflict unless out-of-character replies are exempt — and a player
who texts at 23:30 is demonstrably awake. STOP confirmations, `/real` and PAUSE
acknowledgements are therefore always delivered. In-character story content
never is.

Command parsing is strict about scope and loose about form: `STOP`, `stop.`,
`/stop`, `please stop`, `STOP!` and `cancel` all halt the story, while "I had
to stop at the lights" does not — it raises a `needs_review` flag on the
timeline for a human instead. Same for "is this real?" and "I'm scared".

Content validation (`backend/core/validation.py`) is regex over *assertions*,
not bare nouns: a regex cannot tell a fictional death from a claimed real one,
but it can catch emergency instructions, authority claims, credential requests
and unsafe directives. It is tested from both sides — every forbidden shape is
refused, and eight samples of ordinary story prose must pass. Phase 3 adds a
model-based second pass on top; this layer stays, because it cannot be talked
out of its rules.

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
