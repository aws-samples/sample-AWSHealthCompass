"""Shared WAFv2 rule builder — STORY-131 (DD-3 / DD-5 / DD-6 / DD-7).

Region-agnostic helper that returns the ordered DD-3 rule list used by BOTH
WebACLs so they stay in parity:

    p1  AWSManagedRulesCommonRuleSet        (override SizeRestrictions_BODY -> count, DD-5)
    p2  AWSManagedRulesKnownBadInputsRuleSet
    p3  AWSManagedRulesAmazonIpReputationList
    p4  CompassRateLimit (per-IP rate-based, DD-4)

WCU budget: 700 + 200 + 25 + 2 = ~927 (< 1500 cap). No AnonymousIpList / Bot
Control / Fraud Control (TR-12).

Centralizes:
  * DD-5/TR-6 — CRS `SizeRestrictions_BODY` -> `count` override (parity on both
    WebACLs) so a legit bulk-import body >8 KB is not falsely blocked.
  * DD-7/TR-10 — `waf_mode` toggle: 'block' (default, enforcing) vs 'count'
    (observe-only; forces ALL managed-group override_actions AND the rate-rule
    action to `count`).
  * DD-6/TR-9 — the API-edge custom 403 JSON block response is layered onto the
    priority-4 rate rule ONLY when `rate_rule_custom_response_key` is supplied
    (ApiStack). CoreStack (CloudFront) passes None -> AWS-default 403 (Option A).

aws_cdk.aws_wafv2 is L1/Cfn-only (no stable L2) — see .kiro/refs/aws/waf-cdk.md.
"""
from aws_cdk import aws_wafv2 as wafv2


def _visibility(metric_name: str) -> wafv2.CfnWebACL.VisibilityConfigProperty:
    return wafv2.CfnWebACL.VisibilityConfigProperty(
        cloud_watch_metrics_enabled=True,
        metric_name=metric_name,
        sampled_requests_enabled=True,
    )


def build_waf_rules(*, waf_mode: str, rate_limit: int,
                    cors_allow_origin: str = None,
                    rate_rule_custom_response_key: str = None) -> list:
    """Return the ordered DD-3 rule list.

    Args:
        waf_mode: 'block' (enforcing, shipped default) or 'count' (observe-only).
        rate_limit: per-IP request threshold per 300s window (DD-4).
        cors_allow_origin: resolved CORS origin for the API-edge custom block
            response header (ignored unless rate_rule_custom_response_key set).
        rate_rule_custom_response_key: WebACL custom_response_bodies key. When
            set AND waf_mode=='block', the rate rule emits the DD-6 custom 403
            JSON. When None -> AWS-default 403 (CloudFront edge, Option A).
    """
    count_mode = waf_mode == "count"

    # DD-7/TR-10: count mode forces all managed groups to observe-only.
    group_override = (
        wafv2.CfnWebACL.OverrideActionProperty(count={})
        if count_mode
        else wafv2.CfnWebACL.OverrideActionProperty(none={})
    )

    # DD-5/TR-6: never disable CRS; override only SizeRestrictions_BODY -> count
    # so legit bulk-import bodies >8 KB are not falsely blocked. Parity on both.
    crs_overrides = [
        wafv2.CfnWebACL.RuleActionOverrideProperty(
            name="SizeRestrictions_BODY",
            action_to_use=wafv2.CfnWebACL.RuleActionProperty(count={}),
        )
    ]

    rules = [
        wafv2.CfnWebACL.RuleProperty(
            name="AWS-CommonRuleSet",
            priority=1,
            override_action=group_override,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name="AWSManagedRulesCommonRuleSet",
                    rule_action_overrides=crs_overrides,
                ),
            ),
            visibility_config=_visibility("CommonRuleSet"),
        ),
        wafv2.CfnWebACL.RuleProperty(
            name="AWS-KnownBadInputs",
            priority=2,
            override_action=group_override,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name="AWSManagedRulesKnownBadInputsRuleSet",
                ),
            ),
            visibility_config=_visibility("KnownBadInputs"),
        ),
        wafv2.CfnWebACL.RuleProperty(
            name="AWS-AmazonIpReputationList",
            priority=3,
            override_action=group_override,
            statement=wafv2.CfnWebACL.StatementProperty(
                managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                    vendor_name="AWS",
                    name="AWSManagedRulesAmazonIpReputationList",
                ),
            ),
            visibility_config=_visibility("IpReputation"),
        ),
    ]

    # p4 — per-IP rate-based rule (DD-4). Own rule -> `action`, not override_action.
    if count_mode:
        # DD-7/TR-10: observe-only. No custom response (count doesn't block).
        rate_action = wafv2.CfnWebACL.RuleActionProperty(count={})
    elif rate_rule_custom_response_key:
        # DD-6/TR-9 — API edge only: custom 403 JSON + CORS header so the
        # blocked browser request is still CORS-readable JSON.
        rate_action = wafv2.CfnWebACL.RuleActionProperty(
            block=wafv2.CfnWebACL.BlockActionProperty(
                custom_response=wafv2.CfnWebACL.CustomResponseProperty(
                    response_code=403,
                    custom_response_body_key=rate_rule_custom_response_key,
                    response_headers=[
                        wafv2.CfnWebACL.CustomHTTPHeaderProperty(
                            name="Access-Control-Allow-Origin",
                            value=cors_allow_origin or "*",
                        ),
                    ],
                ),
            ),
        )
    else:
        # CloudFront edge (Option A) — AWS-default 403.
        rate_action = wafv2.CfnWebACL.RuleActionProperty(block={})

    rules.append(
        wafv2.CfnWebACL.RuleProperty(
            name="CompassRateLimit",
            priority=4,
            action=rate_action,
            statement=wafv2.CfnWebACL.StatementProperty(
                rate_based_statement=wafv2.CfnWebACL.RateBasedStatementProperty(
                    limit=rate_limit,
                    evaluation_window_sec=300,
                    aggregate_key_type="IP",
                ),
            ),
            visibility_config=_visibility("CompassRateLimit"),
        )
    )

    return rules
