"""CompassEventCapture CDK Stack — us-west-2.

Captures org-level AWS Health events via Amazon EventBridge and forwards
them cross-region to the Amazon SQS Ingestion Queue in us-east-1.

Resources:
  - EventBridge rule (aws.health, scheduledChange + accountNotification)
  - IAM role for EventBridge to send to cross-region SQS
  - DLQ for EventBridge delivery failures
  - Amazon CloudWatch alarm on DLQ depth

Uses CfnRule (L1) because CDK L2 targets.SqsQueue does not support
cross-region imported queues (known CDK limitation).
"""
import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    aws_events as events,
    aws_sqs as sqs,
    aws_sns as sns,
    aws_sns_subscriptions as sns_subs,
    aws_kms as kms,
    aws_iam as iam,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
)
from constructs import Construct

# Shared constant: rule name coupled with CoreStack SQS policy (IMPL-SEC-002-01).
HEALTH_EVENT_RULE_NAME = "compass-health-event-capture"


class EventCaptureStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *,
                 ingestion_queue_arn: str, ops_alert_email: str, **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass us-west-2 EventBridge Health event capture",
                         **kwargs)

        # ---------------------------------------------------------------
        # SNS Ops Alerts Topic — region-local (STORY-121, TR-2)
        # CloudWatch alarm actions can only reference an SNS topic ARN in
        # the alarm's own region. This topic is created independently here
        # rather than passed in from CoreStack, for the same reason
        # ingestion_queue_arn is passed as a plain string rather than a
        # construct reference: CDK cannot create cross-region CloudFormation
        # exports/imports. Same encryption/SSL posture as CoreStack's
        # OpsAlertsTopic, subscribed to the same operator email address.
        # ---------------------------------------------------------------
        self.ops_alerts_topic = sns.Topic(
            self, "OpsAlertsTopicWest",
            topic_name="compass-ops-alerts",
            display_name="Compass Operational Alerts (us-west-2)",
            enforce_ssl=True,
            master_key=kms.Alias.from_alias_name(self, "OpsAlertsTopicKeyAlias", "alias/aws/sns"),
        )
        self.ops_alerts_topic.add_subscription(
            sns_subs.EmailSubscription(ops_alert_email)
        )

        # TR-13 (mandatory, Snape Finding 1/1b — CRITICAL): explicit,
        # conditioned resource policy statement — CDK does NOT auto-grant
        # CloudWatch publish permission via add_alarm_action(). Do NOT
        # replace with grant_publish(ServicePrincipal(...)) — that omits the
        # aws:SourceAccount condition and is a cross-account confused-deputy
        # vulnerability. See core_stack.py for full rationale.
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

        # DLQ for EventBridge delivery failures.
        # IMPL-SEC-002-02: encrypted at rest + enforce SSL.
        self.dlq = sqs.Queue(self, "EventBridgeDLQ",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
        )

        # IAM role for EventBridge to deliver to cross-region SQS.
        # IMPL-SEC-002-04: scoped to exact queue ARNs, no wildcards.
        event_role = iam.Role(self, "EventBridgeRole",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
        )
        event_role.add_to_policy(iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[ingestion_queue_arn, self.dlq.queue_arn],
        ))

        # EventBridge rule via CfnRule (L1) — L2 targets.SqsQueue
        # does not support cross-region imported queues.
        self.rule = events.CfnRule(self, "HealthEventRule",
            name=HEALTH_EVENT_RULE_NAME,
            description="Captures org-level AWS Health events for Compass ITSM integration",
            event_pattern={
                "source": ["aws.health"],
                "detail-type": ["AWS Health Event"],
                "detail": {
                    "eventTypeCategory": ["scheduledChange", "accountNotification"],
                },
            },
            targets=[events.CfnRule.TargetProperty(
                id="IngestionQueue",
                arn=ingestion_queue_arn,
                role_arn=event_role.role_arn,
                dead_letter_config=events.CfnRule.DeadLetterConfigProperty(
                    arn=self.dlq.queue_arn,
                ),
            )],
        )

        # CloudWatch alarm — any delivery failure is critical.
        eb_dlq_alarm = cloudwatch.Alarm(self, "EventBridgeDLQAlarm",
            metric=self.dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            alarm_description="EventBridge failed to deliver Health events to SQS Ingestion Queue",
        )
        # TR-5 (STORY-121): notification action appended, existing alarm
        # block above is unmodified.
        eb_dlq_alarm.add_alarm_action(cw_actions.SnsAction(self.ops_alerts_topic))

        # Outputs for operational reference.
        CfnOutput(self, "EventBridgeRuleArn",
            value=f"arn:aws:events:{self.region}:{self.account}:rule/{HEALTH_EVENT_RULE_NAME}")
        CfnOutput(self, "EventBridgeDLQUrl", value=self.dlq.queue_url)
        CfnOutput(self, "OpsAlertsTopicArnWest", value=self.ops_alerts_topic.topic_arn)
