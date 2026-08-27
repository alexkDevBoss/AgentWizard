"""Cognito plus the admin Lambda, mounted on the shared HTTP API.

Auth is Cognito's hosted UI over authorization-code-with-PKCE. The console
never sees a password: it redirects to Cognito, gets an ID token back, and
sends it as a bearer token. API Gateway's JWT authorizer verifies the signature
before our code runs.

Self sign-up is off. Operators are created by hand with
``aws cognito-idp admin-create-user`` -- there are one or two of them, and an
open sign-up page in front of a console that can message real players would be
indefensible.
"""

from __future__ import annotations

import hashlib

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as cwlogs
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.aws_apigatewayv2_authorizers import HttpUserPoolAuthorizer
from aws_cdk.aws_apigatewayv2_integrations import HttpLambdaIntegration
from constructs import Construct

from infra.bundling import build_lambda_asset
from infra.config import EnvConfig

#: Every route the console can call. Declared here so the surface is auditable
#: in one place rather than scattered across the handler.
ROUTES: tuple[tuple[apigw.HttpMethod, str], ...] = (
    (apigw.HttpMethod.GET, "/admin/me"),
    (apigw.HttpMethod.GET, "/admin/players"),
    (apigw.HttpMethod.POST, "/admin/players"),
    (apigw.HttpMethod.GET, "/admin/players/{player_id}"),
    (apigw.HttpMethod.DELETE, "/admin/players/{player_id}"),
    (apigw.HttpMethod.POST, "/admin/players/{player_id}/message"),
    (apigw.HttpMethod.POST, "/admin/players/{player_id}/note"),
    (apigw.HttpMethod.POST, "/admin/players/{player_id}/stop"),
    (apigw.HttpMethod.POST, "/admin/players/{player_id}/pause"),
    (apigw.HttpMethod.POST, "/admin/players/{player_id}/resume"),
)

#: Where the console may be loaded from during development.
LOCAL_DEV_ORIGIN = "http://localhost:5173"


class AdminApi(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        cfg: EnvConfig,
        api: apigw.HttpApi,
        table: dynamodb.TableV2,
        secret: secretsmanager.Secret,
        console_url: str,
    ) -> None:
        super().__init__(scope, construct_id)
        self.cfg = cfg

        self.user_pool = self._make_user_pool()
        self.domain = self._make_domain()
        self.client = self._make_client(console_url)
        self.function = self._make_function(table, secret)
        self._mount(api)
        self._outputs(scope, console_url)

    # ------------------------------------------------------------- cognito

    def _make_user_pool(self) -> cognito.UserPool:
        return cognito.UserPool(
            self,
            "Operators",
            user_pool_name=f"{self.cfg.prefix}-operators",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            standard_attributes=cognito.StandardAttributes(
                email=cognito.StandardAttribute(required=True, mutable=False)
            ),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=False,
            ),
            # Available but not forced: a locked-out operator during a live
            # story is worse than the marginal risk on a two-person pool.
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=(
                RemovalPolicy.RETAIN if self.cfg.retain_data else RemovalPolicy.DESTROY
            ),
        )

    def _make_domain(self) -> cognito.UserPoolDomain:
        """A Cognito-hosted login domain.

        The prefix is globally unique across all AWS accounts, so it is salted
        with a hash of the account id rather than the id itself -- unique
        without publishing the account number in a public hostname.
        """
        account = Stack.of(self).account
        if "$" in account or "{" in account:
            raise SystemExit(
                "the AWS account must be resolved at synth time for the Cognito "
                "domain prefix; set CDK_DEFAULT_ACCOUNT or pass an explicit env"
            )
        salt = hashlib.sha256(account.encode()).hexdigest()[:10]

        return cognito.UserPoolDomain(
            self,
            "LoginDomain",
            user_pool=self.user_pool,
            cognito_domain=cognito.CognitoDomainOptions(
                domain_prefix=f"{self.cfg.prefix}-{salt}"
            ),
        )

    def _make_client(self, console_url: str) -> cognito.UserPoolClient:
        callbacks = [f"{console_url}/", f"{LOCAL_DEV_ORIGIN}/"]
        return cognito.UserPoolClient(
            self,
            "ConsoleClient",
            user_pool=self.user_pool,
            user_pool_client_name=f"{self.cfg.prefix}-console",
            # A browser cannot keep a secret; PKCE is what protects the flow.
            generate_secret=False,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.OPENID,
                    cognito.OAuthScope.EMAIL,
                    cognito.OAuthScope.PROFILE,
                ],
                callback_urls=callbacks,
                logout_urls=callbacks,
            ),
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            # Long enough to run a story session without a surprise logout,
            # short enough that a stolen token is not a standing key.
            access_token_validity=Duration.hours(8),
            id_token_validity=Duration.hours(8),
            refresh_token_validity=Duration.days(7),
        )

    # -------------------------------------------------------------- lambda

    def _make_function(
        self, table: dynamodb.TableV2, secret: secretsmanager.Secret
    ) -> lambda_.Function:
        log_group = cwlogs.LogGroup(
            self,
            "AdminLogs",
            log_group_name=f"/aws/lambda/{self.cfg.prefix}-admin-api",
            retention=(
                cwlogs.RetentionDays.ONE_MONTH
                if self.cfg.retain_data
                else cwlogs.RetentionDays.ONE_WEEK
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        function = lambda_.Function(
            self,
            "AdminApi",
            function_name=f"{self.cfg.prefix}-admin-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            handler="backend.handlers.admin_api.handler",
            code=lambda_.Code.from_asset(build_lambda_asset()),
            # A manual send pauses for the typing delay like any other message.
            timeout=Duration.seconds(30),
            memory_size=512,
            log_group=log_group,
            environment={
                "ADVENTURE_TABLE_NAME": table.table_name,
                "ADVENTURE_SECRET_NAME": self.cfg.secret_name,
                "ADVENTURE_ENV": self.cfg.name,
                "ADVENTURE_LOG_LEVEL": "INFO",
            },
        )
        table.grant_read_write_data(function)
        secret.grant_read(function)
        return function

    # --------------------------------------------------------------- wiring

    def _mount(self, api: apigw.HttpApi) -> None:
        authorizer = HttpUserPoolAuthorizer(
            "ConsoleAuthorizer",
            self.user_pool,
            user_pool_clients=[self.client],
            identity_source=["$request.header.Authorization"],
        )
        integration = HttpLambdaIntegration("AdminIntegration", self.function)

        for method, path in ROUTES:
            api.add_routes(
                path=path,
                methods=[method],
                integration=integration,
                authorizer=authorizer,
            )

    def _outputs(self, scope: Construct, console_url: str) -> None:
        CfnOutput(scope, "AdminUserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(scope, "AdminUserPoolClientId", value=self.client.user_pool_client_id)
        CfnOutput(
            scope,
            "AdminLoginDomain",
            value=f"https://{self.domain.domain_name}.auth.{Stack.of(self).region}.amazoncognito.com",
            description="Cognito hosted UI",
        )
        CfnOutput(
            scope,
            "AdminCallbackUrl",
            value=f"{console_url}/",
            description="Registered OAuth callback",
        )
