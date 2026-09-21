/**
 * Unit tests for the shared dispatch wire-contract serializer.
 *
 * `buildDispatchBody` is the single boundary at which in-memory dispatch
 * state becomes the `POST /config/dispatch` wire body. These tests pin:
 *   - custom mode emits camelCase rules (the 4 canonical fields, correct order)
 *   - all / ple_only modes OMIT `rules` entirely
 *   - actionabilityFilter is passed through unchanged
 *   - only the 4 canonical fields are copied (no mass-assignment of extra keys)
 *   - byte-identical JSON to the payload pinned by DispatchEditModal.test.tsx
 */
import { describe, it, expect } from 'vitest';
import { buildDispatchBody, DispatchRule } from '../dispatch';

const eksRule: DispatchRule = {
  ruleId: 'rule-1',
  eventTypePattern: 'AWS_EKS_*',
  eventCategories: ['scheduledChange'],
  enabled: true,
};

const rdsRule: DispatchRule = {
  ruleId: 'rule-2',
  eventTypePattern: 'AWS_RDS_*',
  eventCategories: ['scheduledChange', 'accountNotification'],
  enabled: false,
};

// ---------------------------------------------------------------------------
// Custom mode — rules included, camelCase, exact fields
// ---------------------------------------------------------------------------

describe('buildDispatchBody — custom mode', () => {
  it('includes rules with exact camelCase field names (not snake_case)', () => {
    const body = buildDispatchBody('custom', 'all_actionable', [eksRule]);

    expect(body.mode).toBe('custom');
    expect(body.actionabilityFilter).toBe('all_actionable');
    expect(body.rules).toHaveLength(1);

    const r = body.rules![0];
    expect(r).toEqual({
      ruleId: 'rule-1',
      eventTypePattern: 'AWS_EKS_*',
      eventCategories: ['scheduledChange'],
      enabled: true,
    });

    // The regression this story fixes: camelCase keys, never snake_case.
    expect(r).toHaveProperty('ruleId');
    expect(r).toHaveProperty('eventTypePattern');
    expect(r).toHaveProperty('eventCategories');
    expect(r).toHaveProperty('enabled');
    expect(r).not.toHaveProperty('rule_id');
    expect(r).not.toHaveProperty('event_type_pattern');
    expect(r).not.toHaveProperty('event_categories');
  });

  it('emits exactly the 4 canonical fields in fixed order per rule', () => {
    const body = buildDispatchBody('custom', 'all_actionable', [eksRule]);
    expect(Object.keys(body.rules![0])).toEqual([
      'ruleId',
      'eventTypePattern',
      'eventCategories',
      'enabled',
    ]);
  });

  it('does NOT leak extra in-memory keys into the wire body (anti-mass-assignment)', () => {
    const dirtyRule = {
      ...eksRule,
      // Simulated stray/legacy properties that must never reach the server.
      event_type_pattern: 'snake_should_not_leak',
      extra: 'nope',
      isDirty: true,
    } as unknown as DispatchRule;

    const body = buildDispatchBody('custom', 'all_actionable', [dirtyRule]);

    expect(Object.keys(body.rules![0]).sort()).toEqual([
      'enabled',
      'eventCategories',
      'eventTypePattern',
      'ruleId',
    ]);
    expect(body.rules![0]).not.toHaveProperty('extra');
    expect(body.rules![0]).not.toHaveProperty('isDirty');
    expect(body.rules![0]).not.toHaveProperty('event_type_pattern');
    // The camelCase field still carries the real value, not the snake stray.
    expect(body.rules![0].eventTypePattern).toBe('AWS_EKS_*');
  });

  it('preserves rule order and count for multiple rules', () => {
    const body = buildDispatchBody('custom', 'action_required_only', [eksRule, rdsRule]);
    expect(body.rules).toHaveLength(2);
    expect(body.rules!.map(r => r.ruleId)).toEqual(['rule-1', 'rule-2']);
    expect(body.rules![1].eventCategories).toEqual(['scheduledChange', 'accountNotification']);
    expect(body.rules![1].enabled).toBe(false);
  });

  it('produces JSON byte-identical to the DispatchEditModal pinned payload', () => {
    const body = buildDispatchBody('custom', 'all_actionable', [eksRule, rdsRule]);
    expect(JSON.stringify(body)).toBe(
      JSON.stringify({
        mode: 'custom',
        actionabilityFilter: 'all_actionable',
        rules: [
          { ruleId: 'rule-1', eventTypePattern: 'AWS_EKS_*', eventCategories: ['scheduledChange'], enabled: true },
          { ruleId: 'rule-2', eventTypePattern: 'AWS_RDS_*', eventCategories: ['scheduledChange', 'accountNotification'], enabled: false },
        ],
      }),
    );
  });
});

// ---------------------------------------------------------------------------
// Non-custom modes — rules omitted
// ---------------------------------------------------------------------------

describe('buildDispatchBody — non-custom modes OMIT rules', () => {
  it('all mode omits rules even when a non-empty rules array is passed', () => {
    const body = buildDispatchBody('all', 'all_actionable', [eksRule, rdsRule]);
    expect(body.rules).toBeUndefined();
    // undefined is dropped by JSON.stringify — verify the key is absent on the wire.
    expect(Object.prototype.hasOwnProperty.call(JSON.parse(JSON.stringify(body)), 'rules')).toBe(false);
    expect(JSON.stringify(body)).toBe(
      JSON.stringify({ mode: 'all', actionabilityFilter: 'all_actionable' }),
    );
  });

  it('ple_only mode omits rules even when a non-empty rules array is passed', () => {
    const body = buildDispatchBody('ple_only', 'action_required_only', [eksRule]);
    expect(body.rules).toBeUndefined();
    expect(JSON.stringify(body)).toBe(
      JSON.stringify({ mode: 'ple_only', actionabilityFilter: 'action_required_only' }),
    );
  });
});

// ---------------------------------------------------------------------------
// actionabilityFilter passthrough
// ---------------------------------------------------------------------------

describe('buildDispatchBody — actionabilityFilter passthrough', () => {
  it.each(['all_actionable', 'action_required_only'] as const)(
    'passes %s through unchanged in every mode',
    filter => {
      expect(buildDispatchBody('all', filter, []).actionabilityFilter).toBe(filter);
      expect(buildDispatchBody('ple_only', filter, []).actionabilityFilter).toBe(filter);
      expect(buildDispatchBody('custom', filter, [eksRule]).actionabilityFilter).toBe(filter);
    },
  );
});
