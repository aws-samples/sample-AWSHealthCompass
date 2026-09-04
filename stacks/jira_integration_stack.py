"""CompassJira CDK Stack — JIRA integration infrastructure.

Creates: Amazon SQS JIRA queue + DLQ, JIRA Integration AWS Lambda (stub),
Amazon SNS subscription, Amazon CloudWatch log group, DLQ alarm.

All resources deploy to us-east-1. Depends on CompassCore stack.
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


class JiraIntegrationStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *,
                 integration_topic: sns.Topic,
                 ops_alerts_topic: sns.Topic,
                 campaigns_table: dynamodb.Table,
                 resources_table: dynamodb.Table,
                 config_table: dynamodb.Table,
                 jira_secret: secretsmanager.Secret,
                 payload_bucket: s3.Bucket,
                 **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass JIRA integration",
                         **kwargs)

        # Compass shared layer from SSM — avoids cross-stack CloudFormation Export
        # that blocks layer updates when the layer content changes.
        shared_layer_arn = ssm.StringParameter.value_for_string_parameter(
            self, "/compass/shared-layer-arn",
        )
        shared_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "SharedLayerRef", shared_layer_arn,
        )

        # ---------------------------------------------------------------
        # SQS JIRA DLQ — 14-day retention for investigation/replay
        # ---------------------------------------------------------------
        self.jira_dlq = sqs.Queue(
            self, "JiraDLQ",
            queue_name="compass-jira-integration-dlq",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # ---------------------------------------------------------------
        # SQS JIRA Queue — subscribed to SNS Integration Topic
        # maxReceiveCount=10: higher than ingestion (3) to absorb JIRA 429 retries
        # ---------------------------------------------------------------
        self.jira_queue = sqs.Queue(
            self, "JiraQueue",
            queue_name="compass-jira-integration",
            visibility_timeout=Duration.seconds(300),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            dead_letter_queue=sqs.DeadLetterQueue(
                max_receive_count=10,
                queue=self.jira_dlq,
            ),
        )

        # SNS subscription — CDK auto-generates queue policy for sns.amazonaws.com
        integration_topic.add_subscription(
            sns_subs.SqsSubscription(self.jira_queue)
        )

        # ---------------------------------------------------------------
        # CloudWatch Log Group — explicit 30-day retention
        # ---------------------------------------------------------------
        log_group = logs.LogGroup(
            self, "JiraLambdaLogGroup",
            log_group_name="/aws/lambda/compass-jira-integration",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------
        # JIRA Integration Lambda (stub — business logic added later)
        # Reserved concurrency = 2 for JIRA API rate limiting
        # ---------------------------------------------------------------
        self.jira_lambda = lambda_.Function(
            self, "JiraIntegration",
            function_name="compass-jira-integration",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/jira_integration"),
            layers=[shared_layer],
            timeout=Duration.minutes(5),
            memory_size=256,
            reserved_concurrent_executions=2,
            environment={
                "CAMPAIGNS_TABLE": campaigns_table.table_name,
                "RESOURCES_TABLE": resources_table.table_name,
                "CONFIG_TABLE": config_table.table_name,
                "JIRA_SECRET_ARN": jira_secret.secret_arn,
                "PAYLOAD_BUCKET": payload_bucket.bucket_name,
                "LOG_LEVEL": "INFO",
            },
            log_group=log_group,
        )

        # SQS Event Source Mapping — batch_size=1, max_concurrency=2
        self.jira_lambda.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.jira_queue,
                batch_size=1,
                max_concurrency=2,
                report_batch_item_failures=True,
            )
        )

        # ---------------------------------------------------------------
        # IAM — least privilege via CDK grant_* methods
        # ---------------------------------------------------------------
        campaigns_table.grant_read_write_data(self.jira_lambda)
        resources_table.grant_read_write_data(self.jira_lambda)
        config_table.grant_read_data(self.jira_lambda)
        jira_secret.grant_read(self.jira_lambda)
        payload_bucket.grant_read(self.jira_lambda)
        # SQS consume permissions auto-granted by SqsEventSource

        # ---------------------------------------------------------------
        # CloudWatch Alarm — DLQ depth
        # ---------------------------------------------------------------
        jira_dlq_alarm = cloudwatch.Alarm(
            self, "JiraDlqAlarm",
            metric=self.jira_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="Messages in JIRA integration DLQ — investigate failed ticket operations",
        )
        # notification action appended, existing alarm
        # block above is unmodified. ops_alerts_topic is received from
        # CoreStack (same-region pattern already used for integration_topic).
        jira_dlq_alarm.add_alarm_action(cw_actions.SnsAction(ops_alerts_topic))

        # ---------------------------------------------------------------
        # CDK Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "JiraQueueUrl", value=self.jira_queue.queue_url)
        CfnOutput(self, "JiraQueueArn", value=self.jira_queue.queue_arn)
        CfnOutput(self, "JiraDlqUrl", value=self.jira_dlq.queue_url)
        CfnOutput(self, "JiraLambdaArn", value=self.jira_lambda.function_arn)
