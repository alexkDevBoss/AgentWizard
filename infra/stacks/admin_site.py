"""Where the operator console is served from: S3 behind CloudFront.

Deliberately depends on nothing else in the stack. The Cognito client needs
this distribution's domain for its OAuth callback, the API's CORS rules need it
too, and both of those sit downstream of the HTTP API -- so if this construct
referenced the API in turn, CloudFormation would have a dependency cycle.
Keeping the site at the root of the graph is what makes the rest linear.

The bucket is private; CloudFront reaches it through an Origin Access Control.
Nothing here is world-writable and the console itself is useless without a
Cognito token.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Duration, RemovalPolicy
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from constructs import Construct

from infra.config import EnvConfig


class AdminSite(Construct):
    def __init__(self, scope: Construct, construct_id: str, *, cfg: EnvConfig) -> None:
        super().__init__(scope, construct_id)

        self.bucket = s3.Bucket(
            self,
            "SiteBucket",
            bucket_name=f"{cfg.prefix}-admin-site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=(
                RemovalPolicy.RETAIN if cfg.retain_data else RemovalPolicy.DESTROY
            ),
            # The console is a build artefact, not player data -- the spec's
            # "leave nothing behind except player media" applies here.
            auto_delete_objects=not cfg.retain_data,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Distribution",
            comment=f"Adventure Agent admin console ({cfg.name})",
            default_root_object="index.html",
            # One operator, occasionally two. Paying for edge locations in
            # every region would be pure waste.
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(self.bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
                compress=True,
            ),
            # A single-page app owns its own routing: any path CloudFront
            # cannot find must still return the shell, not an error page.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=code,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                )
                for code in (403, 404)
            ],
        )

        CfnOutput(
            scope,
            "AdminConsoleUrl",
            value=self.url,
            description="The operator console",
        )
        CfnOutput(
            scope,
            "AdminSiteBucket",
            value=self.bucket.bucket_name,
            description="Upload target for `make admin-deploy`",
        )
        CfnOutput(
            scope,
            "AdminDistributionId",
            value=self.distribution.distribution_id,
            description="Invalidation target for `make admin-deploy`",
        )

    @property
    def url(self) -> str:
        return f"https://{self.distribution.distribution_domain_name}"
