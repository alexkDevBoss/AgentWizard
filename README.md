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
  story/          arc.py + arcs/*.yaml — the story as data, not code
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

## Phase 2 resources

On top of Phase 1, the operator console and the API behind it:

**`AWS::Cognito::UserPool`** — one operator pool, hosted UI, no self-signup.
Users are created by hand.

**`AWS::S3::Bucket` + `AWS::CloudFront::Distribution`** — the React console,
served from CloudFront. Its configuration (API endpoint, Cognito ids) is
fetched at runtime from `/config.json`, written from stack outputs at deploy
time, so no identifiers are committed.

**Admin routes on the HTTP API**, behind a JWT authorizer. The important one
is `POST /admin/players/{id}/message`, and the important property is that it
is **not privileged**: it goes through the same `dispatch.send_to_player`
chokepoint everything else does, so an operator typing at 23:30 is held for
quiet hours and the seventh message of the day is refused.

The console deploys separately from the stack:

```bash
make admin-config     # write admin/public/config.json from stack outputs
make admin-dev        # run it locally against dev
make admin-deploy     # build, upload to S3, invalidate CloudFront
```

---

## Phase 3 resources

No new AWS resources — one IAM statement and a much larger Lambda bundle.

**`bedrock:InvokeModel`** on the webhook Lambda's role, scoped to Anthropic
models. Two ARNs, not one: the model id is a *cross-region inference profile*,
and invoking one needs permission on the profile **and** on the foundation
models it may route to, which live in whichever region the profile picks.

**The Lambda bundle grows from ~2MB to ~36MB unpacked**, because
`requirements.txt` now carries the `anthropic` SDK (and with it pydantic and
httpx2). That is still far inside Lambda's 250MB unpacked limit. The SDK is
installed *without* its `[bedrock]` extra — that extra is only boto3 and
botocore, which the runtime already provides.

### Which model, and why not a newer one

`backend/core/config.py` pins `us.anthropic.claude-opus-4-6-v1`. Two
constraints produced that string, both verified against the live account
rather than read off a docs page:

- **This account cannot reach Claude Opus 5, Opus 4.8/4.7, or Sonnet 5.** They
  return `403 ... is not available for this account` on Bedrock. Opus 4.6 is
  the most capable model it is entitled to. Getting the newer ones appears to
  need an AWS Sales conversation, not a console toggle.
- **The `us.` prefix is required.** A bare `anthropic.claude-opus-4-6-v1` is
  rejected for on-demand throughput; only the cross-region inference profile
  works.

There is a second Bedrock endpoint — the Messages API, reached through the
SDK's `AnthropicBedrockMantle` client — and it is **not usable here**: it
serves only the newer model ids this account is locked out of, returning 404
for everything else. `AnthropicBedrock`, the InvokeModel path, is what works.

If the account is later granted Opus 5, changing `STORY_MODEL` and re-running
the tests is the whole migration.

**Cost.** Bedrock spend lands on the AWS bill, so the account-wide $50/month
budget from Phase 0 covers it — which is the main reason for using Bedrock at
all rather than the first-party API. At the full ten players by six messages a
day, Opus 4.6 runs roughly $30–40/month; in dev with one or two players it is
cents. Every call logs its token counts under the `model.usage` event.

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
refused, and eight samples of ordinary story prose must pass. A model-based
second pass now sits on top of it for generated content (see *How the story
engine works*); this layer stays regardless, because it cannot be talked out
of its rules.

---

## How the story engine works

`backend/core/engine.py` replaced the Phase 1 echo. One inbound message
produces one generated reply, in four steps:

1. **Work out the beat.** `backend/story/arcs/*.yaml` holds the arc — premise,
   cast, and one beat per day. Which beat is current is *arithmetic over
   player-local calendar days* (`clock.arc_day`), never a model judgement.
   Asking a model "is this beat finished?" makes the shape of someone's week
   depend on the thing least able to be held to it: a stuck beat repeats
   forever, a runaway one burns the arc in an afternoon.
2. **Replay the timeline.** The last 40 player-visible events become the
   conversation, oldest first. System replies, commands and blocked sends are
   excluded — replaying them teaches the model to write STOP confirmations —
   and the out-of-character footer is stripped, because dispatch adds it on
   the way out and leaving it in would double it.
3. **Generate**, with adaptive thinking and the thinking summary suppressed.
4. **Review**, then dispatch.

### The engine has no special powers

Generated content passes exactly the four gates an operator's hand-typed
message does, and carries a fifth burden the operator does not:

| | operator | engine |
|---|---|---|
| status / quiet hours / rate limit | yes | yes |
| regex content validation | yes | yes |
| model-based review | no | **yes** |

The review pass (`backend/core/review.py`) reads meaning, which the regex
cannot: it is what catches a message that trips no pattern while still leaving
a player convinced something real is happening. It applies only to *generated*
text — an operator is a human who is accountable for their own words, and
sending them to a model for approval would be both surprising and slower.

Two decisions in there worth not re-litigating:

- **A reviewer that cannot be reached does not block the message.** It has
  already passed every deterministic rule; failing closed would mean a
  throttled account silently replaces someone's week with "…give me a
  moment." The send is logged loudly and flagged `needs_review` instead.
- **A verdict of `safe: true` that also names a broken rule is treated as a
  refusal.** Trusting the boolean is the dangerous way to resolve that
  contradiction.

### When generation fails

Every failure path ends with the player getting something sane and the
operator getting a flag. A model timeout, a refused review, or a broken arc
file all fall back to `safety.SAFE_FALLBACK` — sent as **ordinary story
content**, so it is held during quiet hours and spends one of the six exactly
as the message it replaced would have. Exempting it would turn a failing model
into a way to message somebody at 23:30.

### Duplicate updates

The handler claims each Telegram `update_id` in DynamoDB before doing any
work. Generation turned a handler that answered in milliseconds into one that
takes several seconds, and Telegram retries anything it does not get a 2xx
for; without the claim, a retry arriving mid-generation would answer the same
message twice and spend two of the player's six on it. The trade is
deliberate: a claimed update that then fails is not retried. A dropped reply
is recoverable and visible in the log; a duplicate one is neither.

### Writing a new arc

Drop a YAML file in `backend/story/arcs/` and point `DEFAULT_ARC_ID` at it, or
set a player's `arc_id`. `tests/test_arc.py` validates every shipped arc on
every run, so a malformed one fails in CI rather than halfway through
somebody's week. Nothing in an arc file is a safety control — the rules apply
regardless of what its text asks for, and an arc that demanded something
forbidden would simply have its messages refused.

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

- **A Bedrock `403 ... is not available for this account`** is model
  entitlement, not IAM. Adding permissions will not fix it; the account has to
  be granted the model. Check what it can actually reach with a real call —
  `list-foundation-models` lists models the account cannot invoke.
- **`Thinking may not be enabled when tool_choice forces tool use`** — the two
  cannot be combined. That is why the writer thinks and returns prose while
  the reviewer returns a forced-tool verdict without thinking.
- **Destroying and immediately redeploying dev** will fail on the secret name:
  deleted secrets sit in a 7–30 day recovery window that blocks reuse of the
  name. Force it through with
  `aws secretsmanager delete-secret --secret-id adventure-agent/dev --force-delete-without-recovery`.
- **CDK deprecation warnings about `TableGrantsProps`** come from inside
  `aws-cdk-lib`'s own `TableV2` implementation, not from this repo. Ignore them.
- On Windows the CDK CLI spawns the app through `cmd.exe`, which rejects
  forward slashes in the interpreter path. The Makefile handles this; a
  hand-typed `cdk --app ".venv/Scripts/python.exe ..."` will not work.
