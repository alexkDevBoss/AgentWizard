"""The Telegram webhook route and its Lambda.

The HTTP API itself is owned by the stack, not by this construct: the admin
console already hangs off the same API, and email (Phase 5) and the voice
bridge (Phase 6) will too. This construct adds one route to it.

That route is deliberately *not* behind the API's JWT authorizer. Telegram
cannot present a Cognito token; it authenticates with a secret header the
handler verifies in constant time against Secrets Manager.
"""

from __future__ import annotations

from aws_cdk import Duration, RemovalPolicy
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as cwlogs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from infra.bundling import build_lambda_asset
from infra.config import EnvConfig

WEBHOOK_PATH = "/telegram/webhook"

#: Per-function reserved concurrency, or None to leave it unset.
#:
#: This account's *total* Lambda concurrency is 10 -- the default for an
#: unverified account, where it is normally 1000 -- and AWS requires at least
#: 10 to remain unreserved. Reserving any amount is therefore rejected
#: outright, and the account cap serves as the guardrail instead. Set this to
#: an integer once the Service Quotas limit is raised, so one runaway
#: function cannot starve the others.
RESERVED_CONCURRENCY: int | None = None


class TelegramApi(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        api: apigw.HttpApi,
        table: dynamodb.TableV2,
        secret: secretsmanager.Secret,
        operator_name: str,
        operator_contact: str,
    ) -> None:
        super().__init__(scope, construct_id)
        self.cfg = cfg

        self.log_group = cwlogs.LogGroup(
            self,
            "WebhookLogs",
            log_group_name=f"/aws/lambda/{cfg.prefix}-telegram-webhook",
            retention=(
                cwlogs.RetentionDays.ONE_MONTH
                if cfg.retain_data
                else cwlogs.RetentionDays.ONE_WEEK
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.function = lambda_.Function(
            self,
            "Webhook",
            function_name=f"{cfg.prefix}-telegram-webhook",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="backend.handlers.telegram_webhook.handler",
            code=lambda_.Code.from_asset(build_lambda_asset()),
            # The handler deliberately sleeps for a second or two before an
            # in-character reply; 30s leaves room for that plus two Bot API
            # round trips.
            timeout=Duration.seconds(30),
            memory_size=512,
            reserved_concurrent_executions=RESERVED_CONCURRENCY,
            log_group=self.log_group,
            environment={
                "ADVENTURE_TABLE_NAME": table.table_name,
                "ADVENTURE_SECRET_NAME": cfg.secret_name,
                "ADVENTURE_ENV": cfg.name,
                "ADVENTURE_OPERATOR_NAME": operator_name,
                "ADVENTURE_OPERATOR_CONTACT": operator_contact,
                "ADVENTURE_LOG_LEVEL": "INFO",
            },
        )

        table.grant_read_write_data(self.function)
        secret.grant_read(self.function)

        api.add_routes(
            path=WEBHOOK_PATH,
            methods=[apigw.HttpMethod.POST],
            integration=HttpLambdaIntegration("WebhookIntegration", self.function),
        )
