"""CompassServiceNow CDK Stack — ServiceNow integration infrastructure.

Creates: Amazon SQS ServiceNow queue + DLQ, ServiceNow Integration AWS Lambda (stub),
Amazon SNS subscription, AWS Secrets Manager secret, Amazon CloudWatch DLQ alarm.

All resources deploy to us-east-1. Depends on CompassCore stack.
Deployment gated by context flag: deploy_servicenow=true
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
    aws_secretsmanager as secretsmanager,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_logs as logs,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_ssm as ssm,
)
from constructs import Construct


class ServiceNowIntegrationStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *,
                 integration_topic: sns.Topic,
                 ops_alerts_topic: sns.Topic,
                 campaigns_table: dynamodb.Table,
                 resources_table: dynamodb.Table,
                 config_table: dynamodb.Table,
                 payload_bucket: s3.Bucket,
                 **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass ServiceNow integration",
                         **kwargs)

        # Compass shared layer from SSM — avoids cross-stack CloudFormation Export
        shared_layer_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/compass/shared-layer-arn",
        )
        shared_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "SharedLayerRef", shared_layer_arn,
        )

        # ---------------------------------------------------------------
        # Secrets Manager — ServiceNow OAuth credentials (empty, populated by onboarding)
        # Stores: client_id, client_secret, username, password, access_token,
        # refresh_token, token_expires_at
        # ---------------------------------------------------------------
        self.servicenow_secret = secretsmanager.Secret(
            self, "ServiceNowCredentials",
            secret_name="compass/servicenow-credentials",  # nosec B106 — Secrets Manager resource name, not a credential
            description="ServiceNow OAuth credentials — populated by onboarding wizard. Do not use placeholder value.",
        )

        # ---------------------------------------------------------------
        # SQS ServiceNow DLQ — 14-day retention for investigation/replay
        # ---------------------------------------------------------------
        self.servicenow_dlq = sqs.Queue(
            self, "ServiceNowDLQ",
            queue_name="compass-servicenow-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # ---------------------------------------------------------------
        # SQS ServiceNow Queue — subscribed to SNS Integration Topic
        # maxReceiveCount=10: absorbs ServiceNow 429 retries
        # ---------------------------------------------------------------
        self.servicenow_queue = sqs.Queue(
            self, "ServiceNowQueue",
            queue_name="compass-servicenow-integration",
            visibility_timeout=Duration.seconds(300),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=10,
                queue=self.servicenow_dlq,
            ),
        )

        # SNS subscription
        integration_topic.add_subscription(
            sns_subs.SqsSubscription(self.servicenow_queue)
        )

        # ---------------------------------------------------------------
        # CloudWatch Log Group — explicit 30-day retention
        # ---------------------------------------------------------------
        log_group = logs.LogGroup(
            self, "ServiceNowLambdaLogGroup",
            log_group_name="/aws/lambda/compass-servicenow-integration",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------
        # ServiceNow Integration Lambda (stub — business logic in STORY-059+)
        # Reserved concurrency = 2 for ServiceNow API rate limiting
        # ---------------------------------------------------------------
        self.servicenow_lambda = lambda_.Function(
            self, "ServiceNowIntegration",
            function_name="compass-servicenow-integration",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/servicenow_integration"),
            layers=[shared_layer],
            timeout=Duration.minutes(5),
            memory_size=256,
            reserved_concurrent_executions=2,
            environment={
                "CAMPAIGNS_TABLE": campaigns_table.table_name,
                "RESOURCES_TABLE": resources_table.table_name,
                "CONFIG_TABLE": config_table.table_name,
                "SERVICENOW_SECRET_ARN": self.servicenow_secret.secret_arn,
                "PAYLOAD_BUCKET": payload_bucket.bucket_name,
                "LOG_LEVEL": "INFO",
            },
            log_group=log_group,
        )

        # SQS Event Source Mapping — batch_size=1, max_concurrency=2
        self.servicenow_lambda.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.servicenow_queue,
                batch_size=1,
                max_concurrency=2,
                report_batch_item_failures=True,
            )
        )

        # ---------------------------------------------------------------
        # IAM — least privilege via CDK grant_* methods
        # ---------------------------------------------------------------
        campaigns_table.grant_read_write_data(self.servicenow_lambda)
        resources_table.grant_read_write_data(self.servicenow_lambda)
        config_table.grant_read_data(self.servicenow_lambda)
        self.servicenow_secret.grant_read(self.servicenow_lambda)
        self.servicenow_secret.grant_write(self.servicenow_lambda)  # Token refresh writes
        payload_bucket.grant_read(self.servicenow_lambda)

        # ---------------------------------------------------------------
        # CloudWatch Alarm — DLQ depth
        # ---------------------------------------------------------------
        servicenow_dlq_alarm = cloudwatch.Alarm(
            self, "ServiceNowDlqAlarm",
            metric=self.servicenow_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Messages in ServiceNow integration DLQ — investigate failed ticket operations",
        )
        # TR-5 (STORY-121): notification action appended, existing alarm
        # block above is unmodified. ops_alerts_topic is received from
        # CoreStack (same-region pattern already used for integration_topic).
        servicenow_dlq_alarm.add_alarm_action(cw_actions.SnsAction(ops_alerts_topic))

        # ---------------------------------------------------------------
        # CDK Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "ServiceNowQueueUrl", value=self.servicenow_queue.queue_url)
        CfnOutput(self, "ServiceNowQueueArn", value=self.servicenow_queue.queue_arn)
        CfnOutput(self, "ServiceNowDlqUrl", value=self.servicenow_dlq.queue_url)
        CfnOutput(self, "ServiceNowLambdaArn", value=self.servicenow_lambda.function_arn)
        CfnOutput(self, "ServiceNowSecretArn", value=self.servicenow_secret.secret_arn)
