"""CompassApi CDK Stack — Amazon API Gateway, API Lambda, scheduled Lambdas, Cognito.

Creates: REST API with API key auth, usage plan, API AWS Lambda (stub),
2 scheduled Lambda functions (stubs), 2 Amazon EventBridge schedule rules,
Amazon CloudWatch log groups, Cognito User Pool with RBAC groups.

All resources deploy to us-east-1. Depends on CompassCore stack.
"""
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_sns as sns,
    aws_sqs as sqs,
    aws_s3 as s3,
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_apigateway as apigateway,
    aws_iam as iam,
    aws_logs as logs,
    aws_events as events,
    aws_events_targets as targets,
    aws_ssm as ssm,
    aws_cognito as cognito,
    aws_cloudfront as cloudfront,
    aws_s3_deployment as s3_deployment,
    aws_wafv2 as wafv2,
    custom_resources as cr,
)
from constructs import Construct

from stacks.waf_rules import build_waf_rules


class ApiStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *,
                 campaigns_table: dynamodb.Table,
                 resources_table: dynamodb.Table,
                 config_table: dynamodb.Table,
                 jira_secret: secretsmanager.Secret,
                 integration_topic: sns.Topic,
                 payload_bucket: s3.Bucket,
                 sns_key: kms.IKey,
                 ingestion_queue: sqs.Queue,
                 jira_fn: lambda_.Function,
                 dashboard_bucket: s3.Bucket,
                 dashboard_distribution: cloudfront.Distribution,
                 event_generator_fn: lambda_.Function = None,
                 servicenow_secret: secretsmanager.Secret = None,
                 **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass API Gateway + dashboard hosting + regional WAF",
                         **kwargs)

        # Compass shared layer from SSM — avoids cross-stack CloudFormation Export
        # that blocks layer updates when the layer content changes.
        shared_layer_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/compass/shared-layer-arn",
        )
        shared_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "SharedLayerRef", shared_layer_arn,
        )

        log_level = self.node.try_get_context("log_level") or "INFO"

        # ---------------------------------------------------------------
        # Sync Lambda
        # ---------------------------------------------------------------
        sync_log_group = logs.LogGroup(
            self, "SyncLogGroup",
            log_group_name="/aws/lambda/compass-sync",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.sync_fn = lambda_.Function(
            self, "SyncLambda",
            function_name="compass-sync",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/sync"),
            layers=[shared_layer],
            timeout=Duration.minutes(5),
            memory_size=256,
            log_group=sync_log_group,
            environment={
                "CAMPAIGNS_TABLE": campaigns_table.table_name,
                "RESOURCES_TABLE": resources_table.table_name,
                "CONFIG_TABLE": config_table.table_name,
                "JIRA_SECRET_ARN": jira_secret.secret_arn,
                "LOG_LEVEL": log_level,
            },
        )

        # IAM — least privilege per Snape IMPL-SEC-005-02
        campaigns_table.grant(self.sync_fn, "dynamodb:GetItem", "dynamodb:UpdateItem")
        resources_table.grant(
            self.sync_fn,
            "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:Scan",
        )
        config_table.grant(self.sync_fn, "dynamodb:GetItem", "dynamodb:PutItem")
        jira_secret.grant_read(self.sync_fn)

        # ServiceNow secret — conditional on Beta ServiceNow deployment
        if servicenow_secret:
            servicenow_secret.grant_read(self.sync_fn)
            servicenow_secret.grant_write(self.sync_fn)  # STORY-138: OAuth token-refresh persists refreshed token (PutSecretValue) — mirrors ServiceNow integration Lambda
            self.sync_fn.add_environment("SERVICENOW_SECRET_ARN", servicenow_secret.secret_arn)

        # Schedule — hourly, disableable via CDK context
        sync_enabled = not self.node.try_get_context("disable_sync_schedule")
        events.Rule(
            self, "SyncSchedule",
            rule_name="compass-sync-schedule",
            schedule=events.Schedule.rate(Duration.hours(1)),
            targets=[targets.LambdaFunction(self.sync_fn)],
            enabled=sync_enabled,
        )

        # ---------------------------------------------------------------
        # Reconciliation Lambda
        # ---------------------------------------------------------------
        reconciliation_log_group = logs.LogGroup(
            self, "ReconciliationLogGroup",
            log_group_name="/aws/lambda/compass-reconciliation",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.reconciliation_fn = lambda_.Function(
            self, "ReconciliationLambda",
            function_name="compass-reconciliation",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/reconciliation"),
            layers=[shared_layer],
            timeout=Duration.minutes(15),
            memory_size=512,
            log_group=reconciliation_log_group,
            environment={
                "CAMPAIGNS_TABLE": campaigns_table.table_name,
                "RESOURCES_TABLE": resources_table.table_name,
                "CONFIG_TABLE": config_table.table_name,
                "INTEGRATION_TOPIC_ARN": integration_topic.topic_arn,
                "PAYLOAD_BUCKET": payload_bucket.bucket_name,
                "LOG_LEVEL": log_level,
            },
        )

        # IAM — DynamoDB: full CRUD on campaigns/resources, restricted on config
        # per Snape IMPL-SEC-005-03
        campaigns_table.grant_read_write_data(self.reconciliation_fn)
        resources_table.grant_read_write_data(self.reconciliation_fn)
        config_table.grant(
            self.reconciliation_fn,
            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan",
        )

        # IAM — SNS publish + KMS encrypt for cross-stack encrypted topic
        # per Snape IMPL-SEC-005-08
        integration_topic.grant_publish(self.reconciliation_fn)
        sns_key.grant_encrypt_decrypt(self.reconciliation_fn)

        # IAM — S3 PutObject restricted to payloads/* prefix
        # per Snape IMPL-SEC-005-05
        self.reconciliation_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[payload_bucket.arn_for_objects("payloads/*")],
        ))

        # IAM — Health API (Resource: "*" required — no resource-level permissions)
        # per Snape IMPL-SEC-005-01: region condition for defense-in-depth
        # AWS Health Organizational View APIs do not support resource-level ARNs.
        self.reconciliation_fn.add_to_role_policy(iam.PolicyStatement(
            actions=[
                "health:DescribeEventsForOrganization",
                "health:DescribeAffectedEntitiesForOrganization",
                "health:DescribeEventDetailsForOrganization",
            ],
            resources=["*"],
            conditions={
                "StringEquals": {
                    "aws:RequestedRegion": "us-east-1",
                },
            },
        ))

        # IAM — Organizations: no resource-level permissions available (SEC-004-06)
        # organizations:ListAccounts does not support resource-level ARN constraints.
        # Transitive requirement of the AWS Health Organizational View API
        # (health:DescribeEventsForOrganization enumerates org accounts to build
        # the org view). Mirrors the api_lambda grant (STORY-134, IMPL-SEC-134-01/04).
        # NOTE: no aws:RequestedRegion condition — Organizations is a global service
        # (IMPL-SEC-134-02/03).
        self.reconciliation_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["organizations:ListAccounts"],
            resources=["*"],
        ))

        # Schedule — daily at 02:00 UTC, disableable via CDK context
        reconciliation_enabled = not self.node.try_get_context("disable_reconciliation_schedule")
        events.Rule(
            self, "ReconciliationSchedule",
            rule_name="compass-reconciliation-schedule",
            schedule=events.Schedule.cron(hour="2", minute="0"),
            targets=[targets.LambdaFunction(self.reconciliation_fn)],
            enabled=reconciliation_enabled,
        )

        # ---------------------------------------------------------------
        # Telemetry Lambda (STORY-080, T-IMP-1)
        # Daily aggregation of anonymized metrics. Consent-gated.
        # ---------------------------------------------------------------
        telemetry_log_group = logs.LogGroup(
            self, "TelemetryLogGroup",
            log_group_name="/aws/lambda/compass-telemetry",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.telemetry_fn = lambda_.Function(
            self, "TelemetryLambda",
            function_name="compass-telemetry",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/telemetry"),
            timeout=Duration.minutes(5),
            memory_size=256,
            log_group=telemetry_log_group,
            environment={
                "CONFIG_TABLE": config_table.table_name,
                "RESOURCES_TABLE": resources_table.table_name,
                "LOG_LEVEL": log_level,
            },
        )

        # IAM — read ResourcesTable, read+write ConfigTable (for TELEMETRY_LATEST)
        resources_table.grant(self.telemetry_fn, "dynamodb:Scan")
        config_table.grant(
            self.telemetry_fn,
            "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan",
        )

        # Schedule — daily at 00:00 UTC
        telemetry_enabled = not self.node.try_get_context("disable_telemetry_schedule")
        events.Rule(
            self, "TelemetrySchedule",
            rule_name="compass-telemetry-schedule",
            schedule=events.Schedule.rate(Duration.days(1)),
            targets=[targets.LambdaFunction(self.telemetry_fn)],
            enabled=telemetry_enabled,
        )

        # ---------------------------------------------------------------
        # API Lambda (STORY-004)
        # ---------------------------------------------------------------
        cors_allow_origin = self.node.try_get_context("cors_allow_origin") or "*"

        api_log_group = logs.LogGroup(
            self, "ApiLambdaLogGroup",
            log_group_name="/aws/lambda/compass-api",
            retention=logs.RetentionDays.ONE_MONTH,
            # TODO: Beta — change RemovalPolicy to RETAIN for audit log preservation (SEC-004-23)
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.api_lambda = lambda_.Function(
            self, "ApiLambda",
            function_name="compass-api",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/api"),
            layers=[shared_layer],
            timeout=Duration.seconds(30),
            memory_size=256,
            log_group=api_log_group,
            environment={
                "CAMPAIGNS_TABLE": campaigns_table.table_name,
                "RESOURCES_TABLE": resources_table.table_name,
                "CONFIG_TABLE": config_table.table_name,
                "JIRA_SECRET_ARN": jira_secret.secret_arn,
                "CORS_ALLOW_ORIGIN": cors_allow_origin,
                "RECONCILIATION_FUNCTION_NAME": self.reconciliation_fn.function_name,
                "SYNC_FUNCTION_NAME": self.sync_fn.function_name,
                "INTEGRATION_TOPIC_ARN": integration_topic.topic_arn,
                "PAYLOAD_BUCKET": payload_bucket.bucket_name,
                "LOG_LEVEL": log_level,
            },
        )

        # IAM — least privilege (Design §5.3, SEC-004-04, SEC-004-IMPL-01)
        config_table.grant_read_write_data(self.api_lambda)
        campaigns_table.grant_read_write_data(self.api_lambda)
        resources_table.grant_read_write_data(self.api_lambda)
        jira_secret.grant_read(self.api_lambda)
        jira_secret.grant_write(self.api_lambda)
        if servicenow_secret:
            servicenow_secret.grant_read(self.api_lambda)
            servicenow_secret.grant_write(self.api_lambda)
            self.api_lambda.add_environment("SERVICENOW_SECRET_ARN", servicenow_secret.secret_arn)
        payload_bucket.grant_read(self.api_lambda)
        # SEC-004-IMPL-01: ONLY GetQueueAttributes — no consume/send
        ingestion_queue.grant(self.api_lambda, "sqs:GetQueueAttributes")
        # Organizations — no resource-level permissions available (SEC-004-06)
        # organizations:ListAccounts does not support resource-level ARN constraints.
        self.api_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["organizations:ListAccounts"],
            resources=["*"],
        ))
        # API → Reconciliation invoke
        self.reconciliation_fn.grant_invoke(self.api_lambda)
        # API → Sync invoke (STORY-038: on-demand sync trigger)
        self.sync_fn.grant_invoke(self.api_lambda)
        # API → JIRA Integration Topic publish (BUG-S23-017: async ticket creation via SNS)
        integration_topic.grant_publish(self.api_lambda)
        sns_key.grant_encrypt_decrypt(self.api_lambda)
        # S3 PutObject for large payload offload (resolve_core.payload.publish_or_offload)
        self.api_lambda.add_to_role_policy(iam.PolicyStatement(
            actions=["s3:PutObject"],
            resources=[payload_bucket.arn_for_objects("payloads/*")],
        ))

        # API → Event Generator invoke (STORY-045: test event generation)
        if event_generator_fn:
            self.api_lambda.add_environment(
                "EVENT_GENERATOR_FUNCTION_NAME", event_generator_fn.function_name,
            )
            event_generator_fn.grant_invoke(self.api_lambda)

        # ---------------------------------------------------------------
        # API Gateway REST API (STORY-004)
        # WAFv2 REGIONAL WebACL is attached to the prod stage below
        # (STORY-131 — closes SEC-004-24: SQLi/XSS/known-bad-inputs/IP-reputation
        # managed groups + per-IP rate limiting).
        # ---------------------------------------------------------------
        api_access_log_group = logs.LogGroup(
            self, "ApiAccessLogGroup",
            log_group_name="/aws/apigateway/compass-api-access",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.api = apigateway.RestApi(
            self, "CompassApi",
            rest_api_name="compass-api",
            description="Compass + ITSM Integration API (Beta)",
            deploy_options=apigateway.StageOptions(
                stage_name="prod",
                throttling_rate_limit=50,
                throttling_burst_limit=100,
                # SEC-004-11: ERROR, not INFO — prevents header leaks in execution logs
                logging_level=apigateway.MethodLoggingLevel.ERROR,
                data_trace_enabled=False,
                metrics_enabled=True,
                access_log_destination=apigateway.LogGroupLogDestination(api_access_log_group),
                access_log_format=apigateway.AccessLogFormat.custom(
                    " ".join([
                        apigateway.AccessLogField.context_request_id(),
                        apigateway.AccessLogField.context_identity_source_ip(),
                        apigateway.AccessLogField.context_http_method(),
                        apigateway.AccessLogField.context_resource_path(),
                        apigateway.AccessLogField.context_status(),
                        apigateway.AccessLogField.context_identity_api_key_id(),
                        apigateway.AccessLogField.context_integration_latency(),
                    ])
                ),
            ),
            default_cors_preflight_options=apigateway.CorsOptions(
                allow_origins=[cors_allow_origin],
                allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
                allow_headers=["Content-Type", "x-api-key", "Authorization"],
                max_age=Duration.seconds(300),
            ),
            endpoint_types=[apigateway.EndpointType.REGIONAL],
        )

        # SEC-004-22: Gateway responses with CORS headers on 4XX/5XX
        for response_type in [
            apigateway.ResponseType.DEFAULT_4_XX,
            apigateway.ResponseType.DEFAULT_5_XX,
        ]:
            self.api.add_gateway_response(
                f"GatewayResponse{response_type.response_type}",
                type=response_type,
                response_headers={
                    "Access-Control-Allow-Origin": f"'{cors_allow_origin}'",
                    "Access-Control-Allow-Headers": "'Content-Type, x-api-key, Authorization'",
                },
            )

        # API Key + Usage Plan
        self.api_key = apigateway.ApiKey(
            self, "DashboardApiKey",
            api_key_name="compass-dashboard-key",
            description="API key for Compass dashboard",
        )

        usage_plan = self.api.add_usage_plan(
            "CompassUsagePlan",
            name="compass-usage-plan",
            throttle=apigateway.ThrottleSettings(
                rate_limit=50,
                burst_limit=100,
            ),
        )
        usage_plan.add_api_key(self.api_key)
        usage_plan.add_api_stage(stage=self.api.deployment_stage)

        # ---------------------------------------------------------------
        # WAFv2 — REGIONAL WebACL on the API Gateway prod stage (STORY-131)
        # Closes SEC-004-24. L1/Cfn constructs only (no stable L2 for wafv2).
        # DD-1/DD-3/DD-4/DD-6/DD-7/DD-9 · TR-1/TR-2/TR-5/TR-6/TR-7/TR-9/TR-10.
        # Deploy-time knobs (read like cors_allow_origin, DD-4/DD-7/§3.4):
        #   -c waf_rate_limit=<n>   default 2000 per-IP / 300s window
        #   -c waf_mode=block|count default 'block' (enforcing); 'count' forces
        #                           ALL rule actions/override_actions to count.
        # ---------------------------------------------------------------
        waf_mode = self.node.try_get_context("waf_mode") or "block"
        waf_rate_limit = int(self.node.try_get_context("waf_rate_limit") or 2000)

        regional_acl = wafv2.CfnWebACL(
            self, "ApiRegionalWebAcl",
            name="compass-api-regional",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="CompassApiRegionalAcl",
                sampled_requests_enabled=True,
            ),
            # DD-6/TR-9: custom 403 JSON body for the API-edge rate-rule block.
            custom_response_bodies={
                "waf-blocked-json": wafv2.CfnWebACL.CustomResponseBodyProperty(
                    content_type="APPLICATION_JSON",
                    content=(
                        '{"error":{"code":"WAF_BLOCKED",'
                        '"message":"Request blocked by security policy."}}'
                    ),
                ),
            },
            rules=build_waf_rules(
                waf_mode=waf_mode,
                rate_limit=waf_rate_limit,
                cors_allow_origin=cors_allow_origin,
                rate_rule_custom_response_key="waf-blocked-json",
            ),
        )

        # TR-2: associate to the prod stage ARN. add_dependency ensures the
        # deployment stage exists before the association is created.
        regional_assoc = wafv2.CfnWebACLAssociation(
            self, "ApiWebAclAssociation",
            resource_arn=(
                f"arn:{self.partition}:apigateway:{self.region}::/restapis/"
                f"{self.api.rest_api_id}/stages/{self.api.deployment_stage.stage_name}"
            ),
            web_acl_arn=regional_acl.attr_arn,
        )
        regional_assoc.node.add_dependency(self.api)

        # DD-9/TR-7: WAF logging -> dedicated CloudWatch log group. Name MUST
        # start with 'aws-waf-logs-'. Credential headers redacted so Cognito
        # JWTs / API keys never land in WAF logs.
        api_waf_log_group = logs.LogGroup(
            self, "ApiWafLogGroup",
            log_group_name="aws-waf-logs-compass-api",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Snape F-2 (MEDIUM, MANDATORY): CfnLoggingConfiguration does NOT create
        # the CloudWatch Logs resource policy that authorizes WAF's vended-log
        # delivery principal (delivery.logs.amazonaws.com). Without it, log
        # delivery is silently denied and AC-6 fails. Provision it explicitly,
        # scoped to this account + region (aws:SourceAccount + aws:SourceArn),
        # per AWS's documented WAF->CWL delivery policy.
        api_waf_log_policy = logs.ResourcePolicy(
            self, "ApiWafLogResourcePolicy",
            resource_policy_name="compass-waf-api-log-delivery",
            policy_statements=[
                iam.PolicyStatement(
                    sid="AWSWAFLogDeliveryApi",
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    # log_group_arn already carries the trailing ':*' (log-stream scope)
                    resources=[api_waf_log_group.log_group_arn],
                    conditions={
                        "StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:{self.partition}:logs:{self.region}:{self.account}:*",
                        },
                    },
                ),
            ],
        )

        # WAF LoggingConfiguration destination requires the log-group ARN
        # WITHOUT the trailing ':*' (CDK log_group_arn appends it, which WAF
        # rejects) — build the clean ARN explicitly.
        api_waf_log_group_arn = (
            f"arn:{self.partition}:logs:{self.region}:{self.account}"
            ":log-group:aws-waf-logs-compass-api"
        )
        api_waf_logging = wafv2.CfnLoggingConfiguration(
            self, "ApiWafLoggingConfig",
            resource_arn=regional_acl.attr_arn,
            log_destination_configs=[api_waf_log_group_arn],
            redacted_fields=[
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "authorization"}),
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "x-api-key"}),
            ],
        )
        # F-2: log delivery must be authorized before WAF starts delivering.
        api_waf_logging.node.add_dependency(api_waf_log_policy)
        api_waf_logging.node.add_dependency(api_waf_log_group)

        # Lambda proxy integration
        # FIX: Use a single broad Lambda permission to avoid exceeding the
        # 20KB resource policy limit (32 routes × 2 stages = 64 permissions).
        # Grant API Gateway invoke once with a wildcard source ARN.
        self.api_lambda.add_permission(
            "ApiGatewayInvoke",
            principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
            source_arn=self.api.arn_for_execute_api(),
        )
        # Use AwsIntegration instead of LambdaIntegration to prevent CDK from
        # auto-creating per-method Lambda:Permission resources.
        integration = apigateway.AwsIntegration(
            proxy=True,
            service="lambda",
            path=f"2015-03-31/functions/{self.api_lambda.function_arn}/invocations",
            options=apigateway.IntegrationOptions(
                credentials_role=None,  # Use resource-based policy (the permission above)
            ),
        )

        # ---------------------------------------------------------------
        # Cognito User Pool (STORY-067, Beta Phase 3)
        # SEC-PH3-01: AccountRecovery.NONE — no self-service password reset
        # ---------------------------------------------------------------
        self.user_pool = cognito.UserPool(
            self, "CompassUserPool",
            user_pool_name="compass-itsm-users",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_uppercase=True,
                require_lowercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            account_recovery=cognito.AccountRecovery.NONE,
            mfa=cognito.Mfa.OPTIONAL,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # App Client — SPA flow, no secret
        self.app_client = self.user_pool.add_client(
            "CompassDashboardClient",
            user_pool_client_name="compass-dashboard",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(
                user_password=True,
                user_srp=True,
                custom=False,
            ),
            id_token_validity=Duration.hours(1),
            access_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            prevent_user_existence_errors=True,
        )

        # RBAC Groups
        cognito.CfnUserPoolGroup(self, "AdminsGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="Admins",
            description="Full read/write access",
            precedence=1,
        )
        cognito.CfnUserPoolGroup(self, "ViewersGroup",
            user_pool_id=self.user_pool.user_pool_id,
            group_name="Viewers",
            description="Read-only access",
            precedence=10,
        )

        # ---------------------------------------------------------------
        # Lambda Authorizer (STORY-067, SEC-068)
        # Dual-auth: Cognito JWT OR API key validated by Lambda.
        # Rollback: deploy with -c auth_mode=api_key_only to revert.
        # ---------------------------------------------------------------
        auth_mode = self.node.try_get_context("auth_mode") or "cognito"

        authorizer = None
        if auth_mode != "api_key_only":
            # Custom resource to retrieve API key value for authorizer validation
            get_api_key_value = cr.AwsCustomResource(
                self, "GetApiKeyValue",
                on_create=cr.AwsSdkCall(
                    service="APIGateway",
                    action="getApiKey",
                    parameters={
                        "apiKey": self.api_key.key_id,
                        "includeValue": True,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of("compass-api-key-value"),
                    output_paths=["value"],
                ),
                on_update=cr.AwsSdkCall(
                    service="APIGateway",
                    action="getApiKey",
                    parameters={
                        "apiKey": self.api_key.key_id,
                        "includeValue": True,
                    },
                    physical_resource_id=cr.PhysicalResourceId.of("compass-api-key-value"),
                    output_paths=["value"],
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([
                    iam.PolicyStatement(
                        actions=["apigateway:GET"],
                        resources=[f"arn:aws:apigateway:{self.region}::/apikeys/{self.api_key.key_id}"],
                    ),
                ]),
            )

            authorizer_log_group = logs.LogGroup(
                self, "AuthorizerLogGroup",
                log_group_name="/aws/lambda/compass-cognito-authorizer",
                retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )

            # C-1: Timeout ≥ 10s for cold start + 5s JWKS fetch headroom
            self.authorizer_fn = lambda_.Function(
                self, "CognitoAuthorizer",
                function_name="compass-cognito-authorizer",
                runtime=lambda_.Runtime.PYTHON_3_12,
                handler="handler.lambda_handler",
                code=lambda_.Code.from_asset("lambdas/authorizer"),
                timeout=Duration.seconds(10),
                memory_size=128,
                log_group=authorizer_log_group,
                environment={
                    "USER_POOL_ID": self.user_pool.user_pool_id,
                    "APP_CLIENT_ID": self.app_client.user_pool_client_id,
                    "API_KEY_VALUE": get_api_key_value.get_response_field("value"),
                    "LOG_LEVEL": log_level,
                },
            )

            # C-4: Cache TTL = 300s; group changes propagate within 5 minutes
            # FIX: Use CfnAuthorizer (L1) with empty identity_source so API Gateway
            # always invokes the Lambda regardless of which headers are present.
            # The L2 RequestAuthorizer requires identity_sources which makes APIGW
            # reject requests missing ANY listed header with 401 before invoking Lambda.
            self.authorizer_fn.add_permission(
                "ApiGwAuthorizerInvoke",
                principal=iam.ServicePrincipal("apigateway.amazonaws.com"),
                source_arn=f"arn:aws:execute-api:{self.region}:{self.account}:{self.api.rest_api_id}/authorizers/*",
            )

            cfn_authorizer = apigateway.CfnAuthorizer(
                self, "CompassLambdaAuth",
                rest_api_id=self.api.rest_api_id,
                name="compass-lambda-authorizer",
                type="REQUEST",
                authorizer_uri=f"arn:aws:apigateway:{self.region}:lambda:path/2015-03-31/functions/{self.authorizer_fn.function_arn}/invocations",
                authorizer_result_ttl_in_seconds=0,  # TTL=0 disables caching; no identity_source needed
            )

            authorizer = cfn_authorizer  # Truthy sentinel; actual wiring via CFN override below

        # ---------------------------------------------------------------
        # Route tree (Design §4.4 — 32 methods across 26 resources)
        # Auth: Lambda authorizer (cognito mode) or native API key (api_key_only mode)
        # C-3: Rollback — deploy with `cdk deploy -c auth_mode=api_key_only`
        # C-5: GET /api/status excluded from authorizer (health check)
        # ---------------------------------------------------------------
        # Build method options based on auth mode
        if authorizer:
            auth_opts = {
                "authorization_type": apigateway.AuthorizationType.CUSTOM,
            }
        else:
            auth_opts = {"api_key_required": True}

        api_root = self.api.root.add_resource("api")

        # /api/campaigns
        campaigns = api_root.add_resource("campaigns")
        campaigns.add_method("GET", integration, **auth_opts)
        campaign_id = campaigns.add_resource("{id}")
        campaign_id.add_method("GET", integration, **auth_opts)
        campaign_id.add_resource("resources").add_method("GET", integration, **auth_opts)
        campaign_id.add_resource("breakdown").add_method("GET", integration, **auth_opts)
        campaign_id.add_resource("create-tickets").add_method("POST", integration, **auth_opts)
        campaign_id.add_resource("group-preview").add_method("GET", integration, **auth_opts)

        # /api/config/jira
        config = api_root.add_resource("config")
        config.add_resource("summary").add_method("GET", integration, **auth_opts)
        jira = config.add_resource("jira")
        jira.add_method("GET", integration, **auth_opts)
        jira.add_method("POST", integration, **auth_opts)
        jira.add_method("DELETE", integration, **auth_opts)
        jira.add_resource("test").add_method("POST", integration, **auth_opts)

        # /api/config/servicenow (STORY-064)
        servicenow = config.add_resource("servicenow")
        servicenow.add_method("GET", integration, **auth_opts)
        servicenow.add_method("POST", integration, **auth_opts)
        servicenow.add_method("DELETE", integration, **auth_opts)
        servicenow.add_resource("test").add_method("POST", integration, **auth_opts)

        # /api/config/routing
        routing = config.add_resource("routing")
        routing.add_method("GET", integration, **auth_opts)
        routing.add_resource("default").add_method("POST", integration, **auth_opts)
        accounts = routing.add_resource("accounts")
        accounts.add_method("POST", integration, **auth_opts)
        accounts.add_resource("{accountId}").add_method("DELETE", integration, **auth_opts)
        imp = routing.add_resource("import")
        imp.add_method("POST", integration, **auth_opts)
        imp.add_resource("confirm").add_method("POST", integration, **auth_opts)
        routing.add_resource("discover").add_method("POST", integration, **auth_opts)
        routing.add_resource("strategy").add_method("POST", integration, **auth_opts)
        routing.add_resource("validate").add_method("POST", integration, **auth_opts)
        tags = routing.add_resource("tags")
        tags.add_method("GET", integration, **auth_opts)
        tags.add_method("POST", integration, **auth_opts)
        tags.add_resource("{tagValue}").add_method("DELETE", integration, **auth_opts)
        routing.add_resource("tag-preview").add_method("GET", integration, **auth_opts)
        routing.add_resource("orphan-status").add_method("GET", integration, **auth_opts)
        routing.add_resource("suggestions").add_method("GET", integration, **auth_opts)

        # /api/config/status + /api/config/activate (BUG-030-01)
        config.add_resource("status").add_method("GET", integration, **auth_opts)
        config.add_resource("activate").add_method("POST", integration, **auth_opts)

        # /api/config/setup-timer (STORY-079: Setup Time Measurement)
        setup_timer = config.add_resource("setup-timer")
        setup_timer.add_method("GET", integration, **auth_opts)
        setup_timer.add_resource("start").add_method("POST", integration, **auth_opts)
        setup_timer.add_resource("complete").add_method("POST", integration, **auth_opts)

        # /api/config/telemetry (STORY-080: Beta Telemetry)
        config.add_resource("telemetry").add_method("GET", integration, **auth_opts)

        # /api/config/cmdb-routing (STORY-087: CMDB-based routing)
        cmdb_routing = config.add_resource("cmdb-routing")
        cmdb_routing.add_method("GET", integration, **auth_opts)
        cmdb_routing.add_method("POST", integration, **auth_opts)

        # /api/config/routing/services (STORY-088: Service-based routing)
        services = routing.add_resource("services")
        services.add_method("GET", integration, **auth_opts)
        services.add_method("POST", integration, **auth_opts)
        services.add_resource("{service}").add_method("DELETE", integration, **auth_opts)

        # /api/config/integrations (STORY-093: Multi-Platform ITSM Routing)
        integrations = config.add_resource("integrations")
        integrations.add_method("GET", integration, **auth_opts)
        integrations.add_method("PUT", integration, **auth_opts)

        # /api/config/platform (STORY-055, STORY-065)
        platform = config.add_resource("platform")
        platform.add_method("GET", integration, **auth_opts)
        platform.add_method("PUT", integration, **auth_opts)
        platform.add_method("POST", integration, **auth_opts)

        # /api/config/dispatch
        dispatch = config.add_resource("dispatch")
        dispatch.add_method("GET", integration, **auth_opts)
        dispatch.add_method("POST", integration, **auth_opts)
        rules = dispatch.add_resource("rules")
        rules.add_method("POST", integration, **auth_opts)
        rule_id = rules.add_resource("{ruleId}")
        rule_id.add_method("PUT", integration, **auth_opts)
        rule_id.add_method("DELETE", integration, **auth_opts)

        # /api/status — C-5: Health check exempt from Lambda authorizer and API key
        api_root.add_resource("status").add_method("GET", integration, api_key_required=False)

        # /api/reconcile
        reconcile = api_root.add_resource("reconcile")
        reconcile.add_method("POST", integration, **auth_opts)
        reconcile.add_resource("status").add_method("GET", integration, **auth_opts)

        # /api/metrics
        api_root.add_resource("metrics").add_resource("routing-coverage").add_method(
            "GET", integration, **auth_opts,
        )

        # /api/routing/coverage (STORY-071, B-ROUTE-3)
        routing_api = api_root.add_resource("routing")
        routing_coverage = routing_api.add_resource("coverage")
        routing_coverage.add_method("GET", integration, **auth_opts)
        routing_coverage.add_resource("unroutable").add_method("GET", integration, **auth_opts)

        # /api/routing/orphans (STORY-089: Orphan Queue Visibility)
        routing_api.add_resource("orphans").add_method("GET", integration, **auth_opts)

        # /api/sync (STORY-038: on-demand JIRA sync trigger)
        api_root.add_resource("sync").add_method("POST", integration, **auth_opts)

        # /api/generate-events (STORY-045: test event generation)
        api_root.add_resource("generate-events").add_method("POST", integration, **auth_opts)

        # /api/test/route (STORY-077: dry-run routing test)
        test_resource = api_root.add_resource("test")
        test_resource.add_resource("route").add_method("POST", integration, **auth_opts)

        # /api/telemetry (STORY-086: Beta P1 Metrics T-B-4, T-B-5)
        telemetry_api = api_root.add_resource("telemetry")
        telemetry_api.add_resource("session").add_method("POST", integration, **auth_opts)
        telemetry_api.add_resource("event").add_method("POST", integration, **auth_opts)

        # ---------------------------------------------------------------
        # Apply CfnAuthorizer ID to all CUSTOM-auth methods via CFN override.
        # This is necessary because we use L1 CfnAuthorizer (to get empty
        # identity_source) but L2 add_method() for route definitions.
        # ---------------------------------------------------------------
        if authorizer:
            for child in self.node.find_all():
                if isinstance(child, apigateway.CfnMethod):
                    if child.authorization_type == "CUSTOM":
                        child.add_property_override("AuthorizerId", cfn_authorizer.ref)

        # ---------------------------------------------------------------
        # Runtime config.json — deployed to dashboard S3 bucket so the SPA
        # can load Cognito/API values at runtime (no build-time baking).
        # ---------------------------------------------------------------
        s3_deployment.BucketDeployment(
            self, "DashboardConfigDeployment",
            sources=[s3_deployment.Source.json_data("config.json", {
                "userPoolId": self.user_pool.user_pool_id,
                "clientId": self.app_client.user_pool_client_id,
                "apiUrl": self.api.url,
                "region": "us-east-1",
            })],
            destination_bucket=dashboard_bucket,
            distribution=dashboard_distribution,
            distribution_paths=["/config.json"],
            prune=False,  # STORY-106: Shared bucket — CoreStack deploys dashboard assets here
        )

        # ---------------------------------------------------------------
        # CDK Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "SyncLambdaArn", value=self.sync_fn.function_arn)
        CfnOutput(self, "ReconciliationLambdaArn", value=self.reconciliation_fn.function_arn)
        # SEC-004-03: Expose key_id only — retrieve value via:
        #   aws apigateway get-api-key --api-key {id} --include-value (SEC-004-25)
        CfnOutput(self, "ApiUrl", value=self.api.url)
        CfnOutput(self, "ApiKeyId", value=self.api_key.key_id)
        CfnOutput(self, "UserPoolId", value=self.user_pool.user_pool_id)
        CfnOutput(self, "UserPoolClientId", value=self.app_client.user_pool_client_id)
