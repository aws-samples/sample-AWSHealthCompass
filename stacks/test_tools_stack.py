"""CompassTestTools CDK Stack — test-only infrastructure.

TEST ONLY — Do not deploy to production accounts.

Creates an Event Generator Lambda and a us-east-1 EventBridge rule that
routes synthetic aws.health events to the SQS Ingestion Queue. This stack
is gated behind ``deploy_test_tools=true`` CDK context and MUST NOT exist
in production deployments.

The us-east-1 EventBridge rule creates a duplicate ingestion path alongside
the production us-west-2 rule. Processor Lambda idempotency (DynamoDB
conditional writes) prevents duplicate campaigns if both rules fire.
"""
import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    Duration,
    aws_events as events,
    aws_lambda as lambda_,
    aws_iam as iam,
)
from constructs import Construct

# Rule name constant — used in CoreStack SQS policy if needed.
TEST_HEALTH_EVENT_RULE_NAME = "compass-test-health-event-rule"


class TestToolsStack(cdk.Stack):
    def __init__(self, scope: Construct, id: str, *,
                 ingestion_queue_arn: str,
                 ingestion_queue_url: str,
                 **kwargs) -> None:
        super().__init__(scope, id,
                         description="(uksb-1xprlbuzr3) Compass test event generator — TEST ONLY. Do not deploy to production accounts.",
                         **kwargs)

        # CTRL-02: Tag all resources as test environment.
        cdk.Tags.of(self).add("compass:environment", "test")

        # ---------------------------------------------------------------
        # Event Generator Lambda
        # CTRL-03: IAM least privilege — sqs:SendMessage on ingestion queue.
        # CTRL-05: Function name contains 'test'.
        # ---------------------------------------------------------------
        self.generator_fn = lambda_.Function(
            self, "EventGenerator",
            function_name="compass-test-event-generator",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/event_generator"),
            timeout=Duration.minutes(5),
            memory_size=256,
            environment={
                "INGESTION_QUEUE_URL": ingestion_queue_url,
            },
        )

        # CTRL-03: sqs:SendMessage on the ingestion queue.
        self.generator_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[ingestion_queue_arn],
        ))

        # ---------------------------------------------------------------
        # Test EventBridge Rule — routes synthetic events to ingestion queue.
        # CTRL-06: Rule name contains 'test'.
        # NOTE: This creates a duplicate ingestion path. Processor idempotency
        # (DynamoDB conditional writes) prevents duplicate campaigns.
        #
        # Uses CfnRule (L1) with a dedicated IAM role to avoid cyclic
        # cross-stack references. Same pattern as EventCaptureStack.
        # ---------------------------------------------------------------
        event_role = iam.Role(self, "TestEventBridgeRole",
            assumed_by=iam.ServicePrincipal("events.amazonaws.com"),
        )
        event_role.add_to_policy(iam.PolicyStatement(
            actions=["sqs:SendMessage"],
            resources=[ingestion_queue_arn],
        ))

        self.test_rule = events.CfnRule(self, "TestHealthEventRule",
            name=TEST_HEALTH_EVENT_RULE_NAME,
            description="TEST ONLY — Routes synthetic Health events to ingestion queue",
            event_pattern={
                "source": ["aws.health"],
                "detail-type": ["AWS Health Event"],
            },
            targets=[events.CfnRule.TargetProperty(
                id="IngestionQueue",
                arn=ingestion_queue_arn,
                role_arn=event_role.role_arn,
            )],
        )

        # ---------------------------------------------------------------
        # Outputs
        # ---------------------------------------------------------------
        CfnOutput(self, "EventGeneratorFunctionName",
                  value=self.generator_fn.function_name)
        CfnOutput(self, "EventGeneratorFunctionArn",
                  value=self.generator_fn.function_arn)
