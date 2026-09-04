# Adding a New ITSM Platform

To add a new platform (e.g. ServiceNow, GitHub Issues), implement three files:

## 1. Client (`my_platform_client.py`)
Implement `ITSMClient` from `resolve_core/itsm_client.py`. All methods that call external APIs must raise `ITSMAPIError` on failure with `retryable` set appropriately.

## 2. Formatter (`my_platform_formatter.py`)
Subclass `ContentFormatter` from `resolve_core/itsm_client.py`. Render `format_description()` and `format_work_note()` in your platform's native format (HTML, Markdown, ADF, etc.).

## 3. Handler (`handler.py`)
Lambda entry point. Receives standardized events from SQS (via SNS fan-out). Use `itsm_orchestrator.create_or_update_ticket()` — it handles idempotency, DynamoDB tracking, and error logging. Your handler wires the orchestrator to your client and formatter.

## What the Orchestrator Expects
- Your client is passed to `ITSMOrchestrator(client=..., formatter=...)`.
- The orchestrator calls `client.create_ticket()`, `client.add_work_note()`, etc.
- Return `{"batchItemFailures": [...]}` from your handler for partial batch failure reporting.

## Reference Implementation: JiraClient

`resolve_core/jira_client.py` is the canonical reference implementation of `ITSMClient`. When building a new platform client, inspect `JiraClient` to see:

- **Adapter pattern:** Each `ITSMClient` method delegates to a platform-specific HTTP method (e.g., `create_ticket` → `create_issue`), translating errors to `ITSMAPIError` at the boundary.
- **Rate limiting:** 429 handling with exponential backoff lives inside the client, not the orchestrator.
- **BulkCreateResult:** Note that `ITSMClient.bulk_create_tickets()` returns the generic `BulkCreateResult` from `itsm_client.py`. Your client must return this type, not a platform-specific variant.
- **Validation methods:** `validate_connection()` and `validate_routing_target()` show how to expose setup-time checks that the onboarding wizard calls.

## CDK Stack
Deploy a new stack following the pattern in `stacks/jira_integration_stack.py`:
- SQS queue + DLQ subscribed to the SNS Integration Topic (from Core Stack)
- Lambda with reserved concurrency (rate-limit protection)
- Secrets Manager secret for platform credentials
- Grant read/write on CampaignsTable and ResourcesTable
