# AWS WAF — Edge Protection for Compass

AWS WAFv2 protects Compass's two public edges. It is created entirely by `cdk deploy`
(no manual/console steps) and **ships enforcing (`block`) by default** — every deploy
is protected out of the box. This document is the operator-facing reference for the
deploy knobs, the client-facing block-response contract, the recommended rollout, WAF
logging, the SPA-fallback behavior, and known follow-ups.

Infrastructure-as-code is **AWS CDK (Python)** — all commands below are `cdk`, not
Terraform.

---

## 1. What is protected, and where

| Edge | WebACL | Scope | Stack | Association |
|------|--------|-------|-------|-------------|
| API Gateway `prod` stage | `compass-api-regional` | `REGIONAL` | `CompassApi` (us-east-1) | `CfnWebACLAssociation` → `.../restapis/{id}/stages/prod` |
| CloudFront dashboard distribution | `compass-dashboard-cloudfront` | `CLOUDFRONT` | `CompassCore` (us-east-1) | `web_acl_id` on the `cloudfront.Distribution` |

**Why the CLOUDFRONT WebACL lives in `CompassCore`, not `CompassApi`:** the CloudFront
`Distribution` is a `CompassCore` resource and `CompassApi` already depends on
`CompassCore`. Placing the CLOUDFRONT WebACL in `CompassApi` and attaching it back to the
Core-owned Distribution would create a circular stack dependency. Creating it in
`CompassCore` and setting `web_acl_id` inline at construction is the only acyclic
arrangement. Both stacks are us-east-1, satisfying the CLOUDFRONT-scope us-east-1
requirement.

**WAF is single-region (us-east-1).** It is **not** part of the multi-region footprint —
the us-west-2 stack (`CompassEventCapture`) carries only the EventBridge Health-capture
rule and has no WAF. Do not describe WAF as replicated across regions.

Both WebACLs carry the same ordered rule set (parity via the shared `waf_rules.py`
builder):

| Priority | Rule | Type | Enforcing action |
|----------|------|------|------------------|
| 1 | `AWSManagedRulesCommonRuleSet` (CRS) | AWS managed group | `override_action = none`; per-rule override `SizeRestrictions_BODY → count` |
| 2 | `AWSManagedRulesKnownBadInputsRuleSet` | AWS managed group | `override_action = none` |
| 3 | `AWSManagedRulesAmazonIpReputationList` | AWS managed group | `override_action = none` |
| 4 | `CompassRateLimit` (per-IP, `AggregateKeyType=IP`, 300s window) | own rule | `action = block` |

`default_action = allow` on both WebACLs (denylist model for a public API/site — the
managed groups + rate rule do the blocking; WAF adds **no** authN/authZ and does not
replace the Cognito/API-key authorizer or the API Gateway usage plan, which are retained).
Total WCU ≈ 927 (< 1,500 cap). No Bot Control, Fraud Control, or `AnonymousIpList` (out of
scope; extra cost/false-positive risk).

`SizeRestrictions_BODY` is overridden to `count` (not blocked) on both WebACLs so a
legitimate bulk account-mapping import (onboarding wizard CSV/JSON) or config `PUT` near/
over the CRS 8 KB body limit is not falsely blocked. The CRS group itself stays enforcing.

---

## 2. Deploy knobs (operator/infra context parameters)

Two deploy-time CDK context parameters control WAF. They are **operator/infra knobs — not
onboarding-wizard fields.** Both are read the same way the stack already reads
`-c account` / `-c ops_alert_email` / `-c cors_allow_origin`.

| Context param | Default | Effect |
|---------------|---------|--------|
| `waf_mode` | `block` | `block` = enforcing (shipped default). `count` = observe-only: all managed-group `override_action`s and the rate-rule `action` are forced to `count`. |
| `waf_rate_limit` | `2000` | Per-IP rate-based rule `limit` (requests per 300s window). Changing it changes the synthesized `Limit` with **no code edit**. |

Example — deploy observe-only for tuning:

```bash
npx cdk deploy --all \
  -c account=$ACCOUNT \
  -c ops_alert_email=<ops-alert-email> \
  -c waf_mode=count \
  -c waf_rate_limit=2000 \
  --require-approval never
```

To ship/return to enforcing, **omit `-c waf_mode`** (it defaults to `block`).

The `waf_rate_limit` default (2000/300s ≈ 6–7 req/s sustained per IP) is generous versus
Compass's ~10K req/**month** total, but low enough to blunt volumetric abuse. Both knobs
are **cost-neutral** — changing them adds/removes no billable line item.

> ### ⚠️ Ops note — `waf_mode=count` is TRANSIENT-TUNING-ONLY
>
> In `count` mode WAF **silently yields zero blocking with no client-visible signal** —
> every attack signature is evaluated and counted (metrics/logs increment) but nothing is
> ever blocked, and clients see completely normal responses. There is **no** client-facing
> indication that protection is off. Treat `count` mode as a **short-lived tuning window
> only**: deploy it to observe false positives against real traffic, then redeploy in the
> default `block` mode. **Ship and leave the default `block`.** Never leave a production
> deployment in `count` mode — it is functionally unprotected while appearing healthy.

---

## 3. Rollout — recommended COUNT-first-then-BLOCK sequence

The shipped default is enforcing, but for a cautious rollout against unfamiliar traffic:

1. **Deploy observe-only:** `cdk deploy --all ... -c waf_mode=count`.
2. **Observe** WAF `CountedRequests` metrics (CloudWatch namespace `AWS/WAFV2`) and the WAF
   logs (§5) for a representative window. Confirm no legitimate traffic (bulk import,
   dashboard, CI/monitoring) is being counted-then-would-be-blocked.
3. **Promote to enforcing:** redeploy **without** `-c waf_mode` (defaults to `block`). The
   flip is an in-place WebACL update — managed groups go `count → none` (enforce) and the
   rate rule goes `count → block`. No WebACL or CloudFront Distribution replacement.

The count→block flip is verifiable read-only with `aws wafv2 get-web-acl`: in count mode the
managed groups show `OverrideAction=Count` and the rate rule shows `Action=Count`
(`BlockedRequests = 0`); in block mode the managed groups show `OverrideAction=None` and the
rate rule shows `Action=Block`.

---

## 4. Block-response contract (what a blocked client sees)

WAF blocks short-circuit **before** the API Gateway CORS gateway-response injection, so the
response shape is defined at the WAF layer. The contract differs by rule and edge:

| Edge / rule | Blocked response |
|-------------|------------------|
| **API edge — `CompassRateLimit` (per-IP rate rule)** | HTTP **403** with custom JSON body `{"error":{"code":"WAF_BLOCKED","message":"Request blocked by security policy."}}` (`Content-Type: application/json`) **plus** `Access-Control-Allow-Origin: <cors_allow_origin>` (the same value ApiStack already resolves). This keeps the "every API response is CORS-readable JSON" invariant for the rule most likely to catch a mis-tuned-threshold legitimate user. |
| **API edge — managed-group blocks (CRS / KnownBadInputs / IpReputation)** | AWS-default **403** (`{"message":"Forbidden"}`), no custom body/CORS header. These fire on genuine attack signatures; an opaque 403 is an accepted tradeoff. |
| **CloudFront edge — all rules** | AWS-default **403** at the edge (origin never reached). A blocked static-asset request is a browser-native load failure the SPA cannot re-shape. |

If `cors_allow_origin` is later narrowed from `*` to a specific origin (separate CORS
lockdown work), the API rate-rule custom response automatically emits the same resolved
value — keep the two consistent.

### Distinguishing the three 403s (support skill)

- **WAF block** — `403`, no app JSON (except the API rate-rule `WAF_BLOCKED` JSON), and (for
  managed-group / CloudFront blocks) **no** `Access-Control-Allow-Origin`; in a browser it
  looks like an opaque CORS/network error. Confirm in WAF logs (`action=BLOCK`, §5).
- **Auth failure** — `401/403` **with** the app/CORS shape (via the CORS gateway responses).
- **Domain error** — `4xx/5xx` with an `{"error":{...}}` app JSON body.

---

## 5. WAF logging

Each WebACL logs to its own CloudWatch Logs group. **Log-group names must begin with the
mandatory `aws-waf-logs-` prefix** or the logging configuration fails at deploy.

| Log group | Edge |
|-----------|------|
| `aws-waf-logs-compass-api` | REGIONAL (API Gateway) |
| `aws-waf-logs-compass-cloudfront` | CLOUDFRONT (dashboard) |

- **Credential headers `authorization` and `x-api-key` are redacted** (`redacted_fields`) on
  both logging configs — Cognito JWTs / API keys never land in WAF logs (expect `REDACTED`).
- Retention 30 days, `RemovalPolicy.DESTROY` (cost-conscious; volume is far under the free
  allotment → ≈$0). Note for incident response: 30 days is a short forensic window.
- A CloudWatch Logs **resource policy** for `delivery.logs.amazonaws.com` (scoped to each log
  group, with `aws:SourceAccount` / `aws:SourceArn` confused-deputy guards) is provisioned so
  WAF's vended-log delivery is authorized. A green deploy does not by itself prove delivery —
  confirm real `action=ALLOW`/`action=BLOCK` records appear in both groups.
- **Reading logs:** filter for `action = BLOCK` and inspect `terminatingRuleId` /
  `ruleGroupList` to see whether a managed group or `CompassRateLimit` fired.
- **Sampled requests caution:** the WAF "sampled requests" store (`sampled_requests_enabled`)
  is **not** covered by `redacted_fields` and may transiently (~3h) hold un-redacted
  credential headers, readable via `wafv2:GetSampledRequests`. Access is IAM-gated — keep it
  restricted to break-glass admin/security; do not grant it to any runtime role.

---

## 6. SPA deep-link fallback change (CloudFront)

To make CloudFront WAF blocks return a **true 403** (rather than being silently rewritten to
`200` + the SPA shell), the dashboard distribution's error-response handling is configured as
follows:

- The `403 → 200 /index.html` mapping is **not** present; only `404 → 200
  /index.html` (ttl 0) remains. The dashboard OAI is granted **`s3:ListBucket`** on the
  bucket ARN so S3 returns **`404 Not Found`** (not `403 Access Denied`) for a missing object
  key.

Net effect:

- **WAF-edge 403** → no `403 → 200` rule exists → client receives a **true 403** (AWS-default
  WAF error page / CloudFront "Request blocked" page), not the SPA shell; the WAF log shows
  `action=BLOCK` for the same request and `BlockedRequests` increments.
- **SPA deep link** (browser requests a client-side route that is not a real object) → S3
  returns `404` → `404 → 200 /index.html` fires → **200 + SPA shell**, so client-side routing
  works unchanged.
- **Genuine S3-origin 403** → passes through as a true 403 instead of being masked as `200` —
  strictly more correct.

Security note: `s3:ListBucket` is granted **only** to the CloudFront OAI canonical user, on
the **bucket ARN** (not `/*`). The bucket stays `BlockPublicAccess.BLOCK_ALL`; no
public/anonymous listing is introduced. Clients can still only `GET` objects through the
distribution. The distribution uses OAI (no OAC migration).

---

## 7. Operational runbook notes

### 7.1 Tuning a managed-rule false positive
- Symptom: a legitimate operator/user gets a spurious `403` (e.g., mid-onboarding on a large
  bulk import, or a tool that sends no `User-Agent`).
- First response: redeploy with `-c waf_mode=count`, confirm the affected traffic now passes,
  and inspect WAF logs/sampled requests to identify the matching rule. **Return to `block`
  after tuning** (see the ops note in §2 — do not leave `count` in place).
- Known pre-mitigated cases: CRS `SizeRestrictions_BODY` (8 KB) is already overridden to
  `count` for the bulk-import path; **CI/monitoring/`curl` clients MUST send a `User-Agent`
  header** (CRS `NoUserAgent_HEADER` blocks missing-UA requests).
- Targeted fix: override the specific offending managed sub-rule to `count` via
  `rule_action_overrides` in `waf_rules.py` (do **not** disable the whole managed group).
- If warranted, raise the per-IP limit with a higher `-c waf_rate_limit`.

### 7.2 Responding to a block spike
- Read WAF logs (§5) to determine whether the spike is genuine abuse (leave enforcing; the
  rate rule / IpReputation is doing its job) or a false positive (tune per §7.1).
- Logging is the mandatory minimum block-visibility control. An optional CloudWatch alarm on
  `BlockedRequests` → `OpsAlertsTopic` is a possible enhancement (§8).

---

## 8. Known follow-ups (deferred, non-blocking)

| Item | Status | Notes |
|------|--------|-------|
| Cognito-JWT do-no-harm validation | Deferred | The empirical check that a valid Cognito JWT is not falsely blocked has not been run because a fresh deployment's user pool has **0 users** (no real JWT can be minted). API-key do-no-harm is validated. Complete this once a real pool user exists. |
| `BlockedRequests` → `OpsAlertsTopic` alarm | Deferred (optional) | A CloudWatch alarm on each WebACL's `BlockedRequests` metric wired to the ops-alert topic is not implemented. WAF logging is the mandatory monitoring floor in its place. Caveat if later implemented: `OpsAlertsTopic` uses the AWS-managed `alias/aws/sns` key, which does not authorize CloudWatch to publish — such an alarm can deploy green yet silently not deliver (use a customer-managed KMS key or prove end-to-end delivery). |

Do not place real secrets, API keys, or operator email addresses in documentation — use
placeholders such as `<ops-alert-email>` and `<api-key>`.

---

## 9. Related documentation

| Document | Location | Relationship |
|----------|----------|--------------|
| README — Infrastructure Footprint / cost / deploy | [`../README.md`](../README.md) | WAF rows, ~$18.88/mo total, deploy knobs |
| JIRA Setup Guide | [`JIRA_SETUP.md`](JIRA_SETUP.md) | JIRA Cloud instance setup |
| ServiceNow Setup Guide | [`SERVICENOW_SETUP.md`](SERVICENOW_SETUP.md) | ServiceNow instance setup |
| Auth Setup | [`AUTH_SETUP.md`](AUTH_SETUP.md) | Cognito user pool and dashboard authentication |
