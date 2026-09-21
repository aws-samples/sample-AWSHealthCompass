# ServiceNow Setup Guide

This guide describes how to prepare a ServiceNow instance for the Compass AWS
Health → ITSM integration and connect it through the Compass onboarding wizard.

> **Beta status.** The ServiceNow integration is a Beta capability. The
> configuration and routing plane is complete, and ticket **execution is
> verified working**: a Health event routed to a configured ServiceNow
> assignment group creates a real record in the ServiceNow instance. See
> [Section 9, Known Limitations](#9-known-limitations-beta) for the items that
> are still deferred (for example, per-resource status write-back detail and
> CSV attachment on the ServiceNow path).

> **Platform choice.** A single Compass deployment integrates with **either**
> ServiceNow **or** JIRA, not both simultaneously. Running JIRA and ServiceNow
> at the same time (dual-platform / per-row routing) is planned, not delivered.
> For the JIRA path, see [`JIRA_SETUP.md`](JIRA_SETUP.md).

---

## 1. Prerequisites and Supported Instances

Before starting, confirm the following.

- A deployed Compass stack **with the ServiceNow integration enabled**. Deploy
  with the CDK context flag `-c deploy_servicenow=true`:

  ```bash
  npx cdk deploy --all -c account=$ACCOUNT -c ops_alert_email=<address> -c deploy_servicenow=true
  ```

  The `ops_alert_email` context parameter is required for all deploys — see the
  [README Quick Start](../README.md#quick-start).
- Administrator access to the target ServiceNow instance (to activate plugins,
  register an OAuth application, create the integration user, and set a system
  property).

### Supported instance types

| Instance type | Use |
|---------------|-----|
| Personal Developer Instance (PDI) | Development and testing. Free from https://developer.servicenow.com. Note that a PDI hibernates after 10 days of inactivity and must be woken from the developer portal. |
| Enterprise instance | Production and pre-production evaluation. |

Compass authenticates using OAuth 2.0 (Section 3). The instance URL must match
the `*.service-now.com` pattern; the onboarding wizard validates this and
rejects internal or private URLs (SSRF protection).

---

## 2. Activate Required Plugins

Confirm the following plugins are active on the instance. On most enterprise
instances these are already active. On a PDI they are typically active by
default, but verify — a hibernated PDI can wake with a plugin inactive.

| Plugin | Purpose |
|--------|---------|
| ITSM / Change Management (`com.snc.change.management`) | Enables the `change_request` table (the default record type). |
| Incident Management (`com.snc.incident`) | Enables the `incident` table (alternative record type). |
| OAuth 2.0 (`com.snc.platform.security.oauth2`) | Enables the `oauth_token.do` token endpoint used for authentication. |
| Configuration Management Database (CMDB) (`com.snc.cmdb`) | Required only if you plan to use CMDB-based routing (not yet delivered — see [Section 9](#9-known-limitations-beta)). Not required for assignment-group routing. |

To verify a plugin: in ServiceNow, go to **System Definition → Plugins**,
search for the plugin, and confirm it shows **Active**. Activate it if not.

---

## 3. Set Up OAuth 2.0 Authentication

Compass authenticates to ServiceNow using OAuth 2.0. The recommended grant for
this server-to-server integration is **client credentials**. Two setup paths
produce equivalent credentials; use whichever your instance version presents.

### 3.1 Mandatory system property

Client-credentials grant is gated by a system property. If it is not set to
`true`, every token request fails with `access_denied`.

1. In ServiceNow, open `sys_properties.list`.
2. Find or create the property `glide.oauth.inbound.client.credential.grant_type.enabled`.
3. Set its value to `true` (type: `true | false`, application scope: `Global`).
4. Save.

> **Common failure:** Omitting this property is the most frequent cause of
> `{"error":"server_error","error_description":"access_denied"}` from
> `oauth_token.do`. Set it before testing the connection.

### 3.2 Path A — New Inbound Integration Experience (recommended)

Newer ServiceNow releases (Washington DC and later) present a guided wizard.

1. Go to **System OAuth → Application Registry**.
2. Choose **New**.
3. Choose **New Inbound Integration Experience**.
4. Choose **New Integration**.
5. In the connection-type modal, choose **OAuth - Client credentials grant**.
6. Enter an application name, for example `Compass Integration`.
7. Set the **OAuth Application User** to the integration user created in
   Section 4. This determines whose roles and access controls apply to API
   calls made with the token.
8. Save.
9. Copy the **Client ID** and **Client Secret**.

> The **OAuth Application User** field is sometimes hidden on the default form
> layout. If it is not visible, adjust the form layout to add it, or set it via
> the list view.

### 3.3 Path B — Legacy Application Registry

Older releases (or the deprecated UI link) use the classic flow.

1. Go to **System OAuth → Application Registry**.
2. Choose **New**.
3. Choose **Create an OAuth API endpoint for external clients**.
4. Enter an application name, for example `Compass Integration`.
5. Save.
6. Copy the **Client ID** and **Client Secret**.

Both paths create records in the same underlying `oauth_entity` table and
produce credentials that work identically against the `oauth_token.do` token
endpoint. The legacy path still functions; its UI is labeled deprecated.

> **Security:** Store the Client ID and Client Secret only in AWS Secrets
> Manager (Section 6). Never commit them, place them in environment variables
> for production use, or print them in logs.

---

## 4. Create the Integration User and Roles

Create a dedicated ServiceNow user for the integration rather than reusing a
person's account. This user's roles determine what the OAuth token can do.

1. Go to **User Administration → Users**.
2. Choose **New**.
3. Enter a User ID, for example `compass_integration`.
4. Set a strong password and mark the user **Active**.
5. Save.
6. Assign the roles below.

| Role | Grants |
|------|--------|
| `itil` | Create and update records in the `incident` and `change_request` tables. Required for ticket execution. |
| `rest_api_explorer` (or an equivalent REST-access role) | REST API access for the token. |

The integration also performs read lookups of assignment groups
(`sys_user_group`). The `itil` role provides sufficient read access for
assignment-group lookups on a standard instance. If CMDB routing is adopted
later, add read access to the relevant CMDB tables at that time.

> **Least privilege.** Do not grant the integration user the `admin` role.
> Grant only the roles needed for record creation and the lookups above.

---

## 5. Set Up Assignment Groups and Find the sys_id

ServiceNow routing targets are **assignment groups**, identified by their
`sys_id` (a 32-character identifier). Compass's routing configuration stores
the `sys_id`, not the group name.

1. Confirm the assignment groups that will own Compass-created records exist
   under **User Administration → Groups**. Create them if needed (each as an
   ITIL-type group with at least the integration user or a responsible team as
   members).
2. Find each group's `sys_id`. You can read it from the group record's URL in
   the ServiceNow UI, or query it via the REST API:

   ```bash
   curl "https://example.service-now.com/api/now/table/sys_user_group?sysparm_query=name=Cloud Operations&sysparm_fields=sys_id,name" \
     -H "Accept: application/json" \
     -u "<USER>:<PASSWORD>"
   ```

   The response contains the `sys_id` to use in routing configuration.

Use the returned `sys_id` values when configuring routing in the dashboard
(Section 8) — one for the default (orphan-queue) assignment group and,
optionally, one per mapped AWS account or tag value.

---

## 6. Connect ServiceNow Through the Onboarding Wizard

After the stack is deployed with the ServiceNow integration enabled, connect
ServiceNow using the dashboard onboarding wizard or the configuration API.

You provide the instance URL and the OAuth credentials:

| Field | Stored in | Notes |
|-------|-----------|-------|
| Instance URL | Amazon DynamoDB ConfigTable | For example, `https://example.service-now.com`. Validated against `*.service-now.com`. |
| Client ID | AWS Secrets Manager (`compass/servicenow-credentials`) | From the OAuth application (Section 3). |
| Client Secret | AWS Secrets Manager (`compass/servicenow-credentials`) | Never stored in DynamoDB or environment variables. |
| Username / Password | AWS Secrets Manager (`compass/servicenow-credentials`) | Used where the configured grant requires them. |

The instance URL is non-sensitive and is stored in DynamoDB. All OAuth
credentials are stored only in Secrets Manager.

### Configuration via the dashboard UI

1. Open the dashboard **Configuration** page.
2. On **Step 1**, choose **ServiceNow** as the ITSM platform.
3. Enter the instance URL.
4. Enter the OAuth credentials.
5. Choose **Test Connection** to validate connectivity and user roles before
   saving.

### Configuration via API

Store credentials by calling the configuration endpoint:

```bash
curl -X POST "$API_URL/api/config/servicenow" \
  -H "x-api-key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "instance_url": "https://example.service-now.com",
    "client_id": "<CLIENT_ID>",
    "client_secret": "<CLIENT_SECRET>",
    "username": "compass_integration",
    "password": "<INTEGRATION_USER_PASSWORD>"
  }'
```

Validate connectivity and permissions before creating tickets:

```bash
curl -X POST "$API_URL/api/config/servicenow/test" \
  -H "x-api-key: $API_KEY"
```

Test Connection returns HTTP `200` with the authenticated user details on
success, or an error with guidance on failure (invalid credentials, missing
roles, or an unreachable instance).

> **SSRF protection:** The `instance_url` is validated against the
> `*.service-now.com` allowlist pattern. Internal or private URLs are rejected.

---

## 7. Record Type Selection

Compass creates one of two ServiceNow record types per campaign.

| Record type | Table | Default use |
|-------------|-------|-------------|
| Change Request | `change_request` | Planned Lifecycle Events (`scheduledChange`). Standard change type (pre-approved). This is the default. |
| Incident | `incident` | Account notifications and security-related advisories (`accountNotification`). |

The record type is configurable per routing rule. By default, `scheduledChange`
events create change requests and `accountNotification` events create
incidents. The current default behavior creates **one change request per
campaign**.

---

## 8. Dashboard Routing Experience for a ServiceNow-Only Deployment

This section documents the ServiceNow-only dashboard routing, tag-routing, and
activation-readiness behavior. This behavior is complete and validated at the
configuration and routing plane.

### 8.1 Routing configuration via the dashboard UI

For a **ServiceNow-only** deployment, the dashboard follows the configured
platform and renders ServiceNow routing directly — no manual ConfigTable
seeding required. Platform is derived from the `platforms` array returned by
`GET /api/config/summary` (`platforms == ["servicenow"]` for a ServiceNow-only
deployment).

On the **Configuration → Edit Routing** modal for a ServiceNow-only deployment:

- **Default Assignment Group** (`sys_id`), **per-account assignment group**
  (with add-row), and **Record Type** (Change Request / Incident) render and
  are editable.
- JIRA-only fields (JIRA project, JIRA issue type) are **hidden**.
- App-wide labels read ServiceNow terms — "Assignment Group", "ServiceNow
  Record", "ServiceNow State" — on the Dashboard resource table, Campaigns
  split-panel, and Create-Tickets modal.
- The configuration-summary routing table shows the **ServiceNow Group** column
  (and hides the JIRA Project column), so a fresh ServiceNow-only customer can
  see where to begin mapping.
- Routing-save validation errors surface **inline** on the specific control.
  The `CFG_SNOW_*` error codes from the routing-save API
  (`CFG_INVALID_SNOW_GROUP_ID`, `CFG_INVALID_SNOW_RECORD_TYPE`,
  `CFG_INVALID_SNOW_GROUP_NAME`, `CFG_SNOW_GROUP_NOT_FOUND`, and the
  section-level `CFG_SNOW_NOT_CONFIGURED`) map to the Default Assignment Group /
  Record Type / per-account row / section-level alert respectively.
- A fully-configured ServiceNow-only customer is **not** shown the false "Setup
  incomplete — configure your JIRA connection" prompt and is **not** re-shown
  onboarding as a first-time user solely because a JIRA-named field is absent.

### 8.2 Tag-based routing to ServiceNow

Tag-based routing (route an event to a target by a resource or account **tag
value**, for example `Team=platform`) supports ServiceNow targets on a
ServiceNow-only deployment.

- In the **tag-routing mapping editor**, a ServiceNow-only deployment renders a
  **ServiceNow Group** (assignment-group `sys_id`) field and a **Record Type**
  selector (Change Request / Incident); the JIRA-only tag target fields (JIRA
  project / issue type) are **hidden**.
- The tag save API (`POST /api/config/routing/tags`) persists
  `snow_assignment_group_id` / `snow_assignment_group_name` / `snow_record_type`
  on the `TAG_ROUTING#{value}` item — the exact shape the routing engine already
  reads. This is config capture only; there is no routing-engine change and no
  DynamoDB key or schema change.
- Targets are validated at save time (format-then-existence): a malformed
  `sys_id` yields `CFG_INVALID_SNOW_GROUP_ID` with no network call; a
  well-formed but non-existent group yields `CFG_SNOW_GROUP_NOT_FOUND`; no
  validated ServiceNow connection yields a top-level `CFG_SNOW_NOT_CONFIGURED`.
  No JIRA connection is required and no JIRA-worded error appears.
- A saved ServiceNow tag mapping round-trips: reopening the tag-routing editor
  reloads the ServiceNow target.

### 8.3 Activation readiness

Dispatch and activation **readiness** is platform-aware for a ServiceNow-only
deployment:

- A ServiceNow-only deployment is **ready to activate** when the ServiceNow
  connection is validated **and** a ServiceNow default assignment group is set.
  Readiness is **not** blocked by a missing JIRA connection or missing JIRA
  default project.
- Readiness warnings (`GET /api/config/status`) and activation errors
  (`POST /api/config/activate`) are **ServiceNow-worded** — for example,
  "Default ServiceNow assignment group not configured. Tickets cannot be
  created until a default assignment group is set." No "JIRA connection" or
  "Default JIRA project" wording appears on a ServiceNow-only deploy.
- Activation error codes include `CFG_SNOW_NOT_VALIDATED` and
  `CFG_DEFAULT_GROUP_MISSING` (ServiceNow analogues of `CFG_JIRA_NOT_VALIDATED`
  and `CFG_DEFAULT_PROJECT_MISSING`).
- The **dispatch selection** decision (category / actionability /
  prefix-wildcard) is **unchanged** — only readiness computation and wording
  are platform-aware.

---

## 9. Known Limitations (Beta)

State of the ServiceNow integration today:

- **Ticket execution is verified working.** A Health event routed to a
  configured ServiceNow assignment group creates a real record in the
  ServiceNow instance (`change_request` by default). A ServiceNow-only
  deployment correctly creates ServiceNow records and does **not** create JIRA
  tickets.
- **Configuration and routing plane is complete end-to-end.** Connecting
  ServiceNow, configuring default / per-account / tag routing to ServiceNow
  assignment groups, sync Lambda polling ServiceNow, a platform-aware dashboard,
  and correct ServiceNow-worded activation readiness are all delivered and
  validated — with no JIRA wording and no JIRA regression.

The following are **deferred** and not yet delivered. Do not rely on them as
working today:

- **Per-resource status write-back detail** (resource-level burndown /
  `RESOURCE_UPDATE` fidelity on the ServiceNow path). Campaign-level ticket
  creation works; detailed per-resource status write-back is deferred.
- **CSV attachment for campaigns with more than 100 resources** on the
  ServiceNow path.
- **First-class clickable ServiceNow ticket rendering in the dashboard.**
- **Dual-platform simultaneous operation** (JIRA and ServiceNow at the same
  time, or per-row routing across both). A deployment supports ServiceNow-only
  **or** JIRA-only, not both.
- **CMDB-based routing** (routing by a Configuration Item's support group).
  Current routing uses assignment groups directly.

Additional operational notes:

- The `/api/config/servicenow` and `/api/config/servicenow/test` endpoints are
  registered in API Gateway and handled by the API Lambda, which is granted
  read/write on the ServiceNow credentials secret. Like all endpoints, they
  require Cognito JWT or API-key authentication.
- OAuth token refresh is handled automatically by the ServiceNow integration
  Lambda.

---

## 10. Troubleshooting

The token endpoint is `POST https://<instance>.service-now.com/oauth_token.do`.
The path has not changed across recent releases; `/oauth2/token` is not a
ServiceNow endpoint and returns a redirect to the login page.

| Symptom | Likely cause | Resolution |
|---------|--------------|------------|
| `{"error":"server_error","error_description":"access_denied"}` from the token endpoint | `glide.oauth.inbound.client.credential.grant_type.enabled` not set to `true` | Set the system property to `true` (Section 3.1) and retry. |
| `access_denied` after the property is set | OAuth 2.0 plugin inactive (common after PDI hibernation) | Verify `com.snc.platform.security.oauth2` is **Active** under **System Definition → Plugins**; activate if needed. |
| `access_denied` with plugin active and property set | Client secret was rotated (for example, on a PDI upgrade), or the OAuth application record is inactive | In **Application Registry**, confirm the application is **Active**, regenerate the Client Secret, and update the `compass/servicenow-credentials` secret in Secrets Manager. |
| Token issued but API calls return `403` | Integration user lacks the required role, or the OAuth Application User is not set | Confirm the `itil` role on the integration user (Section 4) and that the OAuth application's **OAuth Application User** is the integration user. |
| HTTP `401` on API calls | Access token expired and refresh failed, or credentials are wrong | Verify credentials in Secrets Manager; the integration refreshes tokens automatically, so a persistent `401` indicates invalid stored credentials. Re-run Test Connection. |
| Token exchange appears to succeed in a browser but fails from a client | Redirect not followed on the token request | The token endpoint may respond with a redirect; the request must follow it. Compass's client handles this; when testing manually with `curl`, add `-L` to follow redirects. |
| Test Connection reports an unreachable instance | Wrong instance URL, or the URL is not a `*.service-now.com` host | Correct the instance URL; internal or private URLs are rejected by the allowlist. |
| Routing save blocked with `CFG_INVALID_SNOW_GROUP_ID` | Malformed assignment-group `sys_id` | Re-copy the `sys_id` (Section 5); it is validated for format before any network call. |
| Routing save blocked with `CFG_SNOW_GROUP_NOT_FOUND` | The `sys_id` is well-formed but no matching group exists | Confirm the group exists and the `sys_id` is correct. |
| Routing save blocked with `CFG_SNOW_NOT_CONFIGURED` | No validated ServiceNow connection | Complete Test Connection (Section 6) before saving routing. |

---

## 11. Related Documents

| Document | Location | Description |
|----------|----------|-------------|
| README | [`../README.md`](../README.md) | Deployment, architecture, and ITSM setup pointers |
| JIRA Setup Guide | [`JIRA_SETUP.md`](JIRA_SETUP.md) | JIRA Cloud instance setup (alternative to ServiceNow) |
| Auth Setup | [`AUTH_SETUP.md`](AUTH_SETUP.md) | Cognito user pool and dashboard authentication |
| AWS WAF | [`WAF.md`](WAF.md) | Edge protection, deploy knobs, and logging |
