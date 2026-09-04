# Auth Setup — Cognito User Pool

## How It Works

Compass uses an Amazon Cognito User Pool for authentication (beta). The API Gateway uses a **dual-auth authorizer** that accepts either:
- A Cognito JWT token (Bearer token in `Authorization` header) — the primary authentication method
- An API key (`x-api-key` header) — for programmatic and CI/CD use

Self-registration is disabled by design. All users must be created by an administrator.

## Groups and Permissions

| Group | Access |
|-------|--------|
| **Admins** | Full read/write — create campaigns, modify configuration, manage routing |
| **Viewers** | Read-only — view campaigns, resources, and configuration (no mutations) |

## Initial Users

**No users are created automatically.** CDK deploys the Cognito User Pool empty (with the
**Admins** and **Viewers** groups defined but no members) and self-registration disabled — a fresh
deploy has **zero** users. After deploy, an administrator must create the first user and add them to
a group before anyone can sign in to the dashboard (see below).

## Adding a New User

```bash
# Create a user. Substitute a real user-pool ID and an address you control.
aws cognito-idp admin-create-user \
  --user-pool-id <USER_POOL_ID> \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com \
  --desired-delivery-mediums EMAIL
```

> **Email-delivery caution:** `--desired-delivery-mediums EMAIL` sends a Cognito invitation email
> (with a temporary password) to the address you supply. Use **only** an address you control or one
> whose owner has consented to receive it — do not point it at a fabricated or third-party address.
> For test or non-consenting scenarios, suppress the email and set a temporary password
> out-of-band instead:
>
> ```bash
> aws cognito-idp admin-create-user \
>   --user-pool-id <USER_POOL_ID> \
>   --username user@example.com \
>   --user-attributes Name=email,Value=user@example.com \
>   --message-action SUPPRESS \
>   --temporary-password '<TEMP_PASSWORD>'
> ```
>
> With `--message-action SUPPRESS`, no invitation email is sent; convey the temporary password to
> the user through a secure channel.

## Adding a User to a Group

```bash
aws cognito-idp admin-add-user-to-group \
  --user-pool-id <USER_POOL_ID> \
  --username user@example.com \
  --group-name Admins   # or Viewers
```

## First Login Flow

For an admin-created user:

1. User obtains the temporary password (from the invitation email if `EMAIL` delivery was used, or
   out-of-band if the email was suppressed).
2. User signs in with the temporary password.
3. Cognito forces a password change (`NEW_PASSWORD_REQUIRED` challenge).
4. User sets their permanent password.
5. Subsequent logins use the permanent password and return a JWT token.

---

## Related Documents

| Document | Location | Description |
|----------|----------|-------------|
| README | [`../README.md`](../README.md) | Deployment, architecture, and ITSM setup pointers |
| JIRA Setup Guide | [`JIRA_SETUP.md`](JIRA_SETUP.md) | JIRA Cloud instance setup |
| ServiceNow Setup Guide | [`SERVICENOW_SETUP.md`](SERVICENOW_SETUP.md) | ServiceNow instance setup |
| AWS WAF | [`WAF.md`](WAF.md) | Edge protection, deploy knobs, and logging |
