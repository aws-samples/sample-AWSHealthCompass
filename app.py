#!/usr/bin/env python3
"""Compass + ITSM Integration — CDK App Entry Point."""
import aws_cdk as cdk

from stacks.core_stack import CoreStack
from stacks.event_capture_stack import EventCaptureStack
from stacks.jira_integration_stack import JiraIntegrationStack
from stacks.servicenow_integration_stack import ServiceNowIntegrationStack
from stacks.api_stack import ApiStack
from stacks.test_tools_stack import TestToolsStack

app = cdk.App()

env_east = cdk.Environment(
    account=app.node.try_get_context("account"),
    region="us-east-1",
)
env_west = cdk.Environment(
    account=app.node.try_get_context("account"),
    region="us-west-2",
)

core = CoreStack(app, "CompassCore", env=env_east)

EventCaptureStack(app, "CompassEventCapture",
    env=env_west,
    ingestion_queue_arn=core.ingestion_queue.queue_arn,
    ops_alert_email=core.ops_alert_email,
)

jira = JiraIntegrationStack(app, "CompassJira",
    env=env_east,
    integration_topic=core.integration_topic,
    ops_alerts_topic=core.ops_alerts_topic,
    campaigns_table=core.campaigns_table,
    resources_table=core.resources_table,
    config_table=core.config_table,
    jira_secret=core.jira_secret,
    payload_bucket=core.payload_bucket,
)

# CTRL-02: ServiceNow stack MUST NOT deploy without explicit opt-in.
servicenow = None
if app.node.try_get_context("deploy_servicenow") == "true":
    servicenow = ServiceNowIntegrationStack(app, "CompassServiceNow",
        env=env_east,
        integration_topic=core.integration_topic,
        ops_alerts_topic=core.ops_alerts_topic,
        campaigns_table=core.campaigns_table,
        resources_table=core.resources_table,
        config_table=core.config_table,
        payload_bucket=core.payload_bucket,
    )

# CTRL-01: Strict string comparison — "true" only, not truthy.
# CTRL-10: Test tools MUST NOT deploy without explicit opt-in.
test_tools = None
if app.node.try_get_context("deploy_test_tools") == "true":
    test_tools = TestToolsStack(app, "CompassTestTools",
        env=env_east,
        ingestion_queue_arn=core.ingestion_queue.queue_arn,
        ingestion_queue_url=core.ingestion_queue.queue_url,
    )

ApiStack(app, "CompassApi",
    env=env_east,
    campaigns_table=core.campaigns_table,
    resources_table=core.resources_table,
    config_table=core.config_table,
    jira_secret=core.jira_secret,
    integration_topic=core.integration_topic,
    payload_bucket=core.payload_bucket,
    sns_key=core.sns_key,
    ingestion_queue=core.ingestion_queue,
    jira_fn=jira.jira_lambda,
    dashboard_bucket=core.dashboard_bucket,
    dashboard_distribution=core.dashboard_distribution,
    event_generator_fn=test_tools.generator_fn if test_tools else None,
    servicenow_secret=servicenow.servicenow_secret if servicenow else None,
)

cdk.Tags.of(app).add("auto-delete", "never")

app.synth()
