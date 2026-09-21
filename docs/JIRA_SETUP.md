# JIRA Cloud Setup Guide

This guide describes how to prepare an Atlassian JIRA Cloud instance for the
Compass AWS Health → ITSM integration and connect it through the Compass
onboarding wizard. The JIRA integration is validated end-to-end: ticket
creation, bidirectional status sync, account and tag routing, and campaign
management are operational.

> **Platform choice.** A single Compass deployment integrates with **either**
> JIRA **or** ServiceNow, not both simultaneously. For ServiceNow, see
> [`SERVICENOW_SETUP.md`](SERVICENOW_SETUP.md). This guide covers the JIRA path.

---

## 1. Prerequisites

Before starting, confirm the following.

- A deployed Compass stack. See the [README Quick Start](../README.md#quick-start).
  All `cdk` invocations require the `-c ops_alert_email=<address>` context
  parameter.
- Administrator access to the target JIRA Cloud site (to create the
  automation account and grant project permissions).
- The JIRA project or projects that will receive tickets already exist. At
  minimum, one project is required to act as the default routing target
  (orphan queue).

### JIRA Cloud vs. JIRA Data Center

This project targets **JIRA Cloud only**. JIRA Cloud sites use the
`*.atlassian.net` hostname (for example, `example.atlassian.net`).

JIRA Data Center (self-hosted) is **not supported** in this reference sample.
Data Center differs in authentication (no Atlassian API tokens), API version
defaults (REST API v2 with wiki markup rather than v3 with Atlassian Document
Format), and user identifier format. The onboarding wizard validates that the
supplied base URL is a `*.atlassian.net` host and rejects Data Center URLs.

---

## 2. Create the Automation Atlassian Account

Compass authenticates to JIRA as a dedicated automation account. Using a
dedicated account (rather than a person's account) keeps the integration
independent of individual users and scopes the credential's blast radius.

1. Create an Atlassian account for automation, for example
   `compass-automation@example.com`.
2. Add the account to the target JIRA Cloud site.
3. Do **not** grant the account Site Admin or Organization Admin access. The
   integration needs project-level permissions only (Section 3).

> **Recommendation:** The recommendation is to add the automation account to a
> dedicated group (for example, `compass-automation`) and grant that group the
> project permissions. Managing permissions through a group is preferred over
> per-user grants because it scales across projects and simplifies audits.

---

## 3. Grant Project Permissions

The automation account requires the following project permissions on **every**
JIRA project that will receive Compass tickets, including the default routing
project.

| Permission | Why Compass needs it |
|------------|----------------------|
| Browse Projects | Read issues and run JQL searches during bidirectional sync |
| Create Issues | Create tickets from Health events |
| Edit Issues | Update ticket fields and labels when resource status changes |
| Add Comments | Post daily burndown update comments |
| Create Attachments | Attach a CSV of affected resources for campaigns with more than 100 resources |
| Transition Issues | Reserved for status transitions; read-side sync uses JQL and does not transition tickets itself |

> **Least privilege.** Grant only the permissions above. Do not grant Site
> Admin. The automation account does not need to delete issues for production
> operation.

### How to grant permissions via a project role or group

Permissions in JIRA Cloud are assigned through the project's permission
scheme, which maps permissions to project roles (or to groups). The exact
navigation varies by project type (company-managed vs. team-managed), but the
general procedure is:

1. Open the target project in JIRA.
2. Go to **Project settings**.
3. Open the permissions view (for a company-managed project, **Permissions**;
   for a team-managed project, **Access**).
4. Add the automation account (or its group) to a project role that carries
   the six permissions listed above.
5. Repeat for every project that will receive Compass tickets.

Compass validates permissions at connection time. If a required permission is
missing, ticket creation fails with an HTTP `403` and a structured error is
logged (see Section 7).

---

## 4. Generate an API Token

Compass authenticates with JIRA Cloud using the automation account's email
address plus an Atlassian API token (HTTP Basic authentication). Password-based
authentication is not supported by JIRA Cloud for the REST API.

1. Sign in as the automation account.
2. Go to https://id.atlassian.com/manage-profile/security/api-tokens.
3. Choose **Create API token**.
4. Enter a descriptive label, for example `compass-integration`.
5. Copy the token immediately — it is shown only once.

> **Security:** Store the token only in AWS Secrets Manager (Section 5). Never
> commit it to source control, place it in environment variables for
> production use, or print it in logs. If the token is exposed, revoke it and
> generate a new one.

The token inherits the automation account's permissions and does not expire by
default; it can be revoked at any time. A single Atlassian account can hold up
to 25 active tokens.

---

## 5. Connect JIRA Through the Onboarding Wizard

After the stack is deployed, connect JIRA using the dashboard onboarding
wizard or the configuration API. This is **Step 1 (JIRA Connection)** of the
four-step onboarding flow.

You provide three values:

| Field | Stored in | Notes |
|-------|-----------|-------|
| JIRA Base URL | Amazon DynamoDB ConfigTable (`JIRA_CONNECTION` item) | For example, `https://example.atlassian.net` |
| Automation account email | AWS Secrets Manager (`compass/jira-credentials`) | For example, `compass-automation@example.com` |
| API token | AWS Secrets Manager (`compass/jira-credentials`) | Never stored in DynamoDB or environment variables |

The base URL is non-sensitive and is stored in DynamoDB. The email and API
token are credentials and are stored only in Secrets Manager.

### Test Connection

The wizard's **Test Connection** action validates the credentials before any
tickets are created. It calls the JIRA endpoint `GET /rest/api/3/myself`:

- On success, JIRA returns the authenticated account (HTTP `200`), and the
  wizard displays the connected display name.
- On failure, the wizard shows an actionable error (for example, invalid
  credentials or an unreachable URL).

The dashboard's "JIRA configured" state is derived from the `JIRA_CONNECTION`
item's `validated` flag — it becomes `true` only after a successful Test
Connection. It is never inferred from the mere existence of the Secrets Manager
secret, which CDK creates at deploy time with a placeholder value. A fresh
deploy therefore shows the "Setup incomplete" onboarding prompt until JIRA is
connected and validated.

---

## 6. Configure Routing and Validate Project Keys

Routing determines which JIRA project each Health event's tickets are created
in. Configure routing in the remaining onboarding steps.

| Step | Purpose |
|------|---------|
| Account Routing (default) | Set the default JIRA project (required) — the orphan queue for unmapped accounts. |
| Account Routing (overrides) | Optionally map specific AWS account IDs to specific JIRA projects. |
| Dispatch Window | Choose which Health events create tickets (all actionable events, PLEs only, or custom prefix-match rules such as `AWS_EKS_*`). |
| Review & Activate | Confirm the configuration summary and activate the integration. |

### Project-key validation

Every JIRA project key you configure is validated against JIRA before it is
saved. Compass calls `GET /rest/api/3/project/{projectKey}`:

- HTTP `200` confirms the project exists and is accessible; the wizard shows
  the project name.
- HTTP `404` means the project key does not exist or the automation account
  cannot access it. The wizard flags the invalid key and blocks the save until
  it is resolved.

Validating keys at setup time catches misconfiguration before any tickets are
attempted.

### Issue type

All Compass tickets use the issue type `Task` by default. The issue type is
configurable per routing rule for teams that use a different type (for example,
an ITIL-aligned team using `Change Request`). The default `Task` type works in
projects without custom configuration.

---

## 7. Ticket Behavior Reference

This section summarizes what Compass creates and updates in JIRA, so operators
know what to expect.

- **Two ticket templates.** Events **with** affected resources produce a ticket
  containing resource ARNs, a status table, and burndown tracking. Events
  **without** resources (for example, API or SDK deprecations and account
  notifications) produce an account-level ticket with event metadata only.
- **One ticket per routing target per campaign.** A campaign that affects
  multiple teams or accounts produces one ticket per resolved routing target.
- **Status updates.** For Planned Lifecycle Event (PLE) campaigns, tickets are
  updated as resource status changes from PENDING to RESOLVED. Non-PLE tickets
  are static after creation.
- **Daily burndown comments.** A daily comment reports current PENDING and
  RESOLVED counts on PLE tickets.
- **CSV attachment.** Campaigns with more than 100 resources attach the
  resource list as a CSV rather than inlining it in the description.
- **Labels.** Tickets are labeled for filtering (for example, `compass-campaign`,
  the service name, and tag- or account-derived labels).
- **Descriptions use Atlassian Document Format (ADF).** JIRA Cloud REST API v3
  requires rich text as ADF; Compass renders descriptions and comments in ADF.

### Bidirectional sync and the search endpoint

Compass polls JIRA hourly to read ticket status changes. It uses a batch JQL
search and normalizes each ticket's status to `Created`, `In Progress`, or
`Closed` using the `statusCategory.key` value (`new`, `indeterminate`, `done`).
Using the status category rather than the status name means custom JIRA
workflows are handled without per-workflow configuration.

> **Search endpoint migration (handled).** Atlassian removed the legacy
> `POST /rest/api/3/search` endpoint (change CHANGE-2046; announced 2024-10-31,
> effective 2025-05-01). Calls to the old endpoint now return HTTP `410 Gone`.
> Compass uses the replacement cursor-paginated endpoint
> `POST /rest/api/3/search/jql` (and `POST /rest/api/3/search/approximate-count`
> for count-only queries). This migration is already implemented — no customer
> action is required. This note is included so operators recognize the endpoint
> names in logs and are not surprised by references to the deprecation.

---

## 8. Rate Limiting

A single campaign can create hundreds of tickets. JIRA Cloud enforces rate
limits and returns HTTP `429 Too Many Requests` with a `Retry-After` header
when a limit is exceeded.

Compass handles bursts with several layered controls, so operators typically do
not need to take action:

- Reserved concurrency on the JIRA integration Lambda (set to 2) limits
  simultaneous callers.
- Exponential backoff with jitter on `429` responses, honoring `Retry-After`
  when present.
- Amazon SQS buffering absorbs bursts; messages wait in the queue rather than
  being dropped.

If sustained `429` responses appear in logs, the ticket-creation throughput for
that campaign is being throttled by JIRA; the messages remain queued and are
retried.

---

## 9. Troubleshooting

| Symptom | HTTP status | Likely cause | Resolution |
|---------|-------------|--------------|------------|
| Ticket operations fail; dashboard shows a sync failure | 401 | API token expired or revoked, wrong email, or malformed credentials | Regenerate the API token (Section 4) and update the `compass/jira-credentials` secret in Secrets Manager. Re-run Test Connection. |
| Ticket creation fails for a specific project | 403 | Automation account lacks a required permission on that project | Grant the six permissions in Section 3 on the affected project (via the project role or group). |
| Configuration save blocked; project flagged invalid | 404 | JIRA project key does not exist or is not accessible to the automation account | Correct the project key, or grant Browse Projects on the project. Project keys are validated at save time. |
| Ticket creation fails with a field-validation message | 400 | The JIRA project requires custom mandatory fields beyond the standard set (project, summary, description, issuetype) | Adjust the JIRA project to make the extra fields optional, or supply defaults. Compass populates standard fields only. |
| Ticket-creation throughput slows during a large campaign | 429 | JIRA rate limiting during burst creation | No action required — Compass retries with exponential backoff and SQS buffering. Verify the DLQ is empty afterward. |
| Log references `/rest/api/3/search` returning 410 | 410 | Legacy JIRA search endpoint was removed by Atlassian | Already handled — Compass uses `/rest/api/3/search/jql`. If a `410` persists against the new endpoint, capture the request and report it. |

Structured errors for failed ticket operations are logged to Amazon CloudWatch
with the event ARN, failure reason, HTTP status, and timestamp, so failures are
designed to be diagnosable.

---

## 10. Related Documents

| Document | Location | Description |
|----------|----------|-------------|
| README | [`../README.md`](../README.md) | Deployment, architecture, and ITSM setup pointers |
| ServiceNow Setup Guide | [`SERVICENOW_SETUP.md`](SERVICENOW_SETUP.md) | ServiceNow instance setup (alternative to JIRA) |
| Auth Setup | [`AUTH_SETUP.md`](AUTH_SETUP.md) | Cognito user pool and dashboard authentication |
| AWS WAF | [`WAF.md`](WAF.md) | Edge protection, deploy knobs, and logging |
