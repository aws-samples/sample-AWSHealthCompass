"""CompassCore CDK Stack — foundational infrastructure for Compass + ITSM integration.

Creates: 3 Amazon DynamoDB tables, 2 Amazon SQS queues, 1 Amazon SNS topic,
2 Amazon S3 buckets, 1 AWS Secrets Manager secret, 1 AWS Lambda Processor
function (stub), 1 Lambda Layer (empty), 1 Amazon CloudFront distribution
(dashboard), 2 Amazon CloudWatch alarms, cross-region SQS resource policy.

All resources deploy to us-east-1. This stack blocks every other story.
"""
import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_dynamodb as dynamodb,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_s3 as s3,
    aws_kms as kms,
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_iam as iam,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_s3_deployment as s3_deployment,
    aws_ssm as ssm,
    aws_logs as logs,
    aws_wafv2 as wafv2,
)
from constructs import Construct

from stacks.waf_rules import build_waf_rules


class CoreStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass core: SQS/SNS/DynamoDB/Processor/S3/CloudFront WAF",
                         **kwargs)

        # ---------------------------------------------------------------
        # Ops Alert Email — required CDK context parameter
        # Fail-fast at synth time: a topic with no subscriber reproduces the
        # exact "alarm fires, nobody told" state this guard exists to close.
        # No placeholder/default is offered by design. Whole-stack synth
        # failure is the approved posture — fail-closed, not fail-open.
        # ---------------------------------------------------------------
        self.ops_alert_email = self.node.try_get_context("ops_alert_email")
        if not self.ops_alert_email:
            raise ValueError(
                "Missing required CDK context parameter 'ops_alert_email'. "
                "CloudWatch alarm notifications require a real subscriber email address. "
                "Deploy with: cdk deploy --all -c account=<ACCOUNT> -c ops_alert_email=<EMAIL>"
            )

        # ---------------------------------------------------------------
        # DynamoDB Tables
        # ---------------------------------------------------------------
        self.config_table = dynamodb.Table(
            self, "ConfigTable",
            table_name="compass-config",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="ttl",
        )

        self.campaigns_table = dynamodb.Table(
            self, "CampaignsTable",
            table_name="compass-campaigns",
            partition_key=dynamodb.Attribute(name="campaignId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.campaigns_table.add_global_secondary_index(
            index_name="service-startTime-index",
            partition_key=dynamodb.Attribute(name="service", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="startTime", type=dynamodb.AttributeType.STRING),
        )
        self.campaigns_table.add_global_secondary_index(
            index_name="status-updatedAt-index",
            partition_key=dynamodb.Attribute(name="status", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="updatedAt", type=dynamodb.AttributeType.STRING),
        )

        self.resources_table = dynamodb.Table(
            self, "ResourcesTable",
            table_name="compass-resources",
            partition_key=dynamodb.Attribute(name="campaignId", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="trackingKey", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery=True,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            removal_policy=RemovalPolicy.RETAIN,
            time_to_live_attribute="expiresAt",
        )
        self.resources_table.add_global_secondary_index(
            index_name="ticketStatus-index",
            partition_key=dynamodb.Attribute(name="ticketStatus", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # ---------------------------------------------------------------
        # SQS Ingestion Queue + DLQ
        # SQS_MANAGED encryption required.
        # ---------------------------------------------------------------
        self.ingestion_dlq = sqs.Queue(
            self, "IngestionDLQ",
            queue_name="compass-ingestion-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        self.ingestion_queue = sqs.Queue(
            self, "IngestionQueue",
            queue_name="compass-ingestion",
            visibility_timeout=Duration.seconds(900),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=3,
                queue=self.ingestion_dlq,
            ),
        )

        # Cross-region resource policy: allow EventBridge from us-west-2.
        # ArnEquals with exact rule name + SourceAccount.
        from stacks.event_capture_stack import HEALTH_EVENT_RULE_NAME
        self.ingestion_queue.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal("events.amazonaws.com")],
            actions=["sqs:SendMessage"],
            resources=[self.ingestion_queue.queue_arn],
            conditions={
                "ArnEquals": {
                    "aws:SourceArn": f"arn:aws:events:us-west-2:{self.account}:rule/{HEALTH_EVENT_RULE_NAME}",
                },
                "StringEquals": {
                    "aws:SourceAccount": self.account,
                },
            },
        ))

        # Allow test EventBridge rule (us-east-1) when deploy_test_tools is enabled.
        # The test rule uses its own IAM role with sqs:SendMessage, but SQS
        # resource policy must also allow the EventBridge service principal.
        if self.node.try_get_context("deploy_test_tools") == "true":
            from stacks.test_tools_stack import TEST_HEALTH_EVENT_RULE_NAME
            self.ingestion_queue.add_to_resource_policy(iam.PolicyStatement(
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["sqs:SendMessage"],
                resources=[self.ingestion_queue.queue_arn],
                conditions={
                    "ArnEquals": {
                        "aws:SourceArn": f"arn:aws:events:us-east-1:{self.account}:rule/{TEST_HEALTH_EVENT_RULE_NAME}",
                    },
                    "StringEquals": {
                        "aws:SourceAccount": self.account,
                    },
                },
            ))

        # ---------------------------------------------------------------
        # SNS Integration Topic
        # KMS encryption key required.
        # ---------------------------------------------------------------
        self.sns_key = kms.Key(
            self, "SnsEncryptionKey",
            description="KMS key for Compass SNS topic encryption",
            enable_key_rotation=True,
        )

        # SQS service principal must decrypt SNS messages
        # for SNS → SQS subscription delivery. Without this, messages silently vanish.
        self.sns_key.grant_decrypt(iam.ServicePrincipal("sqs.amazonaws.com"))

        self.integration_topic = sns.Topic(
            self, "IntegrationTopic",
            topic_name="compass-integration",
            enforce_ssl=True,
            # noqa: CDK API parameter name — not a forbidden term usage
            master_key=self.sns_key,
        )

        # ---------------------------------------------------------------
        # SNS Ops Alerts Topic
        # Dedicated notification target for CloudWatch alarms — separate from
        # self.integration_topic (the business-event fan-out topic, which this
        # change does not touch). Reuse this topic for all future us-east-1
        # alarm actions — do not create a second ops topic.
        #
        # Encryption: AWS-managed alias/aws/sns key, not a dedicated CMK.
        # This topic has no SQS subscriber and is low-volume (alarm state
        # transitions only), so the cross-service decrypt-grant need that
        # justifies self.sns_key for integration_topic does not apply here.
        # ---------------------------------------------------------------
        self.ops_alerts_topic = sns.Topic(
            self, "OpsAlertsTopic",
            topic_name="compass-ops-alerts",
            display_name="Compass Operational Alerts (us-east-1)",
            enforce_ssl=True,
            master_key=kms.Alias.from_alias_name(self, "OpsAlertsTopicKeyAlias", "alias/aws/sns"),
        )
        self.ops_alerts_topic.add_subscription(
            sns_subs.EmailSubscription(self.ops_alert_email)
        )

        # CDK's add_alarm_action(cw_actions.SnsAction(...)) does NOT automatically
        # grant CloudWatch permission to publish to this topic — verified by
        # CDK source inspection + isolated synth test. Without this
        # explicit statement, alarms detect correctly but notifications are
        # silently denied (reproduces the forbidden "detect but don't
        # notify" state). Do NOT replace this with
        # ops_alerts_topic.grant_publish(iam.ServicePrincipal("cloudwatch.amazonaws.com"))
        # — that helper omits the aws:SourceAccount condition and is a
        # cross-account confused-deputy vulnerability: any AWS
        # account that learns this topic's ARN (e.g. via the CfnOutput below)
        # could point their own CloudWatch alarms at it and publish spoofed
        # notifications. This statement must be added once, immediately after
        # topic creation, before any alarm is wired to it.
        self.ops_alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudWatchAlarmPublish",
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[self.ops_alerts_topic.topic_arn],
                conditions={"StringEquals": {"aws:SourceAccount": self.account}},
            )
        )

        # ---------------------------------------------------------------
        # S3 Payload Offload Bucket
        # KMS_MANAGED encryption.
        # ---------------------------------------------------------------
        self.payload_bucket = s3.Bucket(
            self, "PayloadBucket",
            bucket_name=f"compass-payload-offload-{cdk.Aws.ACCOUNT_ID}",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.KMS_MANAGED,
            enforce_ssl=True,
            lifecycle_rules=[s3.LifecycleRule(expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        # ---------------------------------------------------------------
        # Secrets Manager — JIRA Credentials (empty, populated by onboarding)
        # ---------------------------------------------------------------
        self.jira_secret = secretsmanager.Secret(
            self, "JiraCredentials",
            secret_name="compass/jira-credentials",  # nosec B106 — Secrets Manager resource name, not a credential
            description="JIRA API credentials — populated by onboarding wizard. Do not use placeholder value.",
        )

        # ---------------------------------------------------------------
        # Lambda Layer (empty shell — populated later)
        # ---------------------------------------------------------------
        self.shared_layer = lambda_.LayerVersion(
            self, "CompassCoreLayer",
            code=lambda_.Code.from_asset("lambdas/shared"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Shared resolve_core module",
        )

        # SSM parameter for layer ARN — avoids cross-stack CloudFormation Export
        # which blocks layer updates when consuming stacks import the export.
        ssm.StringParameter(
            self, "SharedLayerArnParam",
            parameter_name="/compass/shared-layer-arn",
            string_value=self.shared_layer.layer_version_arn,
            description="Compass shared Lambda Layer version ARN",
        )

        # ---------------------------------------------------------------
        # Processor Lambda (stub — business logic added later)
        # NO Secrets Manager access, NO secret values in env vars
        # ---------------------------------------------------------------
        self.processor_fn = lambda_.Function(
            self, "Processor",
            function_name="compass-processor",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/processor"),
            layers=[self.shared_layer],
            timeout=Duration.minutes(15),
            memory_size=512,
            environment={
                "CAMPAIGNS_TABLE": self.campaigns_table.table_name,
                "RESOURCES_TABLE": self.resources_table.table_name,
                "CONFIG_TABLE": self.config_table.table_name,
                "INTEGRATION_TOPIC_ARN": self.integration_topic.topic_arn,
                "PAYLOAD_BUCKET": self.payload_bucket.bucket_name,
                "LOG_LEVEL": "INFO",
            },
        )

        # SQS Event Source Mapping
        self.processor_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.ingestion_queue,
                batch_size=1,
                report_batch_item_failures=True,
            )
        )

        # ---------------------------------------------------------------
        # IAM — least privilege via CDK grant_* methods
        # ---------------------------------------------------------------
        self.campaigns_table.grant_read_write_data(self.processor_fn)
        self.resources_table.grant_read_write_data(self.processor_fn)
        self.config_table.grant_read_data(self.processor_fn)
        self.integration_topic.grant_publish(self.processor_fn)
        self.sns_key.grant_encrypt_decrypt(self.processor_fn)
        self.payload_bucket.grant_put(self.processor_fn)
        # SQS consume permissions auto-granted by SqsEventSource

        # ---------------------------------------------------------------
        # CloudWatch Alarms
        # ---------------------------------------------------------------
        ingestion_dlq_alarm = cloudwatch.Alarm(
            self, "IngestionDLQAlarm",
            metric=self.ingestion_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Messages in Compass ingestion DLQ — failed event processing",
        )
        # notification action appended, existing alarm
        # block above is unmodified.
        ingestion_dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_alerts_topic))

        processor_error_alarm = cloudwatch.Alarm(
            self, "ProcessorErrorAlarm",
            metric=self.processor_fn.metric_errors(),
            threshold=1,
            evaluation_periods=3,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Compass Processor Lambda sustained errors",
        )
        # notification action appended, existing alarm
        # block above is unmodified.
        processor_error_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_alerts_topic))

        # ---------------------------------------------------------------
        # Dashboard Hosting — S3 + CloudFront
        # ---------------------------------------------------------------
        self.dashboard_bucket = s3.Bucket(
            self, "DashboardBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        oai = cloudfront.OriginAccessIdentity(
            self, "DashboardOAI",
            comment="OAI for Compass dashboard S3 bucket",
        )
        self.dashboard_bucket.grant_read(oai)

        # grant_read alone grants object-level s3:GetObject on bucket/* but NOT
        # bucket-level s3:ListBucket. Without ListBucket, S3 returns 403
        # (Access Denied) for a MISSING key, which the (now-removed) 403->200
        # error response masked as the SPA shell. Granting s3:ListBucket on the
        # BUCKET ARN (not bucket/*) makes S3 return 404 for missing keys, so the
        # retained 404->200 rule serves the SPA deep-link fallback and the
        # WAF-edge 403 reaches the client as a true 403. Stays on OAI (no OAC
        # migration). Principal is the OAI S3 canonical-user (oai.grant_principal).
        self.dashboard_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudFrontOAIListBucket",
                effect=iam.Effect.ALLOW,
                principals=[oai.grant_principal],
                actions=["s3:ListBucket"],
                resources=[self.dashboard_bucket.bucket_arn],
            )
        )

        # ---------------------------------------------------------------
        # WAFv2 — CLOUDFRONT WebACL for the dashboard distribution
        # Created HERE in CoreStack (NOT ApiStack) to avoid a circular
        # stack dependency — the Distribution is a CoreStack resource and
        # ApiStack already depends on CoreStack. Attached at Distribution
        # construction via web_acl_id below. CLOUDFRONT scope requires the
        # WebACL live in us-east-1 (CoreStack is us-east-1).
        # Shared rule set; Option A (AWS-default 403, no custom response).
        # Deploy-time knobs:
        #   -c waf_rate_limit=<n>   default 2000 per-IP / 300s window
        #   -c waf_mode=block|count default 'block' (enforcing).
        # ---------------------------------------------------------------
        waf_mode = self.node.try_get_context("waf_mode") or "block"
        waf_rate_limit = int(self.node.try_get_context("waf_rate_limit") or 2000)

        self.cloudfront_acl = wafv2.CfnWebACL(
            self, "DashboardCloudFrontWebAcl",
            name="compass-dashboard-cloudfront",
            scope="CLOUDFRONT",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                metric_name="CompassDashboardCloudFrontAcl",
                sampled_requests_enabled=True,
            ),
            rules=build_waf_rules(
                waf_mode=waf_mode,
                rate_limit=waf_rate_limit,
            ),
        )

        self.dashboard_distribution = cloudfront.Distribution(
            self, "DashboardDistribution",
            web_acl_id=self.cloudfront_acl.attr_arn,  # full CLOUDFRONT WebACL ARN
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(
                    self.dashboard_bucket,
                    origin_access_identity=oai,
                ),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
            ),
            default_root_object="index.html",
            error_responses=[
                # The 403->200 /index.html remap was REMOVED. CloudFront applies
                # CustomErrorResponses keyed on the response STATUS CODE and DID
                # catch the WAF-generated edge 403, masking blocked attacks as
                # 200+SPA (empirically observed). With the 403 map gone, a
                # WAF-edge block reaches the client as a true 403. The SPA
                # deep-link fallback now runs via 404->200 (S3 returns 404 for
                # missing keys because the OAI holds s3:ListBucket, granted above).
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
        )

        # ---------------------------------------------------------------
        # WAF logging for the CloudFront WebACL.
        # Name MUST start with 'aws-waf-logs-'. Credential headers redacted.
        # ---------------------------------------------------------------
        cf_waf_log_group = logs.LogGroup(
            self, "CloudFrontWafLogGroup",
            log_group_name="aws-waf-logs-compass-cloudfront",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # CfnLoggingConfiguration does NOT create
        # the CloudWatch Logs resource policy authorizing WAF's vended-log
        # delivery principal. Without it, delivery is silently denied.
        # Provision it explicitly, scoped to this account + region.
        cf_waf_log_policy = logs.ResourcePolicy(
            self, "CloudFrontWafLogResourcePolicy",
            resource_policy_name="compass-waf-cloudfront-log-delivery",
            policy_statements=[
                iam.PolicyStatement(
                    sid="AWSWAFLogDeliveryCloudFront",
                    effect=iam.Effect.ALLOW,
                    principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
                    actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                    resources=[cf_waf_log_group.log_group_arn],
                    conditions={
                        "StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:{self.partition}:logs:{self.region}:{self.account}:*",
                        },
                    },
                ),
            ],
        )

        # LoggingConfiguration destination requires the log-group ARN WITHOUT
        # the trailing ':*' (CDK log_group_arn appends it; WAF rejects it).
        cf_waf_log_group_arn = (
            f"arn:{self.partition}:logs:{self.region}:{self.account}"
            ":log-group:aws-waf-logs-compass-cloudfront"
        )
        cf_waf_logging = wafv2.CfnLoggingConfiguration(
            self, "CloudFrontWafLoggingConfig",
            resource_arn=self.cloudfront_acl.attr_arn,
            log_destination_configs=[cf_waf_log_group_arn],
            redacted_fields=[
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "authorization"}),
                wafv2.CfnLoggingConfiguration.FieldToMatchProperty(
                    single_header={"Name": "x-api-key"}),
            ],
        )
        cf_waf_logging.node.add_dependency(cf_waf_log_policy)
        cf_waf_logging.node.add_dependency(cf_waf_log_group)

        # BUG-S23-001: Force re-upload every deploy. CDK asset hashing can
        # cache the fingerprint and skip upload even when dist/ content changed.
        # A deploy-time timestamp as custom hash guarantees CloudFormation always
        # sees a new asset and triggers the BucketDeployment custom resource.
        import time as _time

        s3_deployment.BucketDeployment(
            self, "DashboardDeployment",
            sources=[s3_deployment.Source.asset(
                "./dashboard/dist",
                asset_hash=str(int(_time.time())),
                asset_hash_type=cdk.AssetHashType.CUSTOM,
            )],
            destination_bucket=self.dashboard_bucket,
            distribution=self.dashboard_distribution,
            distribution_paths=["/*"],
            prune=False,  # False: ApiStack also writes config.json to this bucket
        )

        # ---------------------------------------------------------------
        # CDK Outputs for cross-stack / post-deploy reference
        # ---------------------------------------------------------------
        CfnOutput(self, "DashboardUrl",
            value=f"https://{self.dashboard_distribution.distribution_domain_name}",
            description="Dashboard URL (CloudFront)",
        )
        CfnOutput(self, "DashboardBucketName",
            value=self.dashboard_bucket.bucket_name,
            description="Dashboard S3 bucket for manual asset uploads",
        )
        CfnOutput(self, "IngestionQueueArn", value=self.ingestion_queue.queue_arn)
        CfnOutput(self, "IngestionQueueUrl", value=self.ingestion_queue.queue_url)
        CfnOutput(self, "IntegrationTopicArn", value=self.integration_topic.topic_arn)
        CfnOutput(self, "OpsAlertsTopicArn", value=self.ops_alerts_topic.topic_arn)
        CfnOutput(self, "PayloadBucketName", value=self.payload_bucket.bucket_name)
        CfnOutput(self, "JiraSecretArn", value=self.jira_secret.secret_arn)
