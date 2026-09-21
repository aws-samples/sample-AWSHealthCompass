import React, { useState, useEffect, useRef } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import RadioGroup from '@cloudscape-design/components/radio-group';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Grid from '@cloudscape-design/components/grid';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import Table from '@cloudscape-design/components/table';
import Toggle from '@cloudscape-design/components/toggle';
import Alert from '@cloudscape-design/components/alert';
import { apiFetch } from '../api';
import { DispatchRule, buildDispatchBody } from '../types/dispatch';
import { parseApiError } from '../errors';

// --- Types ---

interface DispatchConfig {
  mode: 'all' | 'ple_only' | 'custom';
  actionabilityFilter: 'all_actionable' | 'action_required_only';
  rules: DispatchRule[];
}

interface DispatchEditModalProps {
  /** Controls modal visibility */
  visible: boolean;
  /** Current dispatch configuration (pre-populated from parent state) */
  initialConfig: DispatchConfig;
  /** Called when modal should close without saving */
  onDismiss: () => void;
  /** Called after successful save — parent should refresh config */
  onSave: () => void;
}

// --- Helpers ---

/** Map Select value to API eventCategories array */
function selectValueToCategories(val: string): string[] {
  if (val === 'both') return ['scheduledChange', 'accountNotification'];
  return [val];
}

/** Map API eventCategories array to Select value */
function categoriesToSelectValue(cats: string[]): string {
  if (cats.length === 2 && cats.includes('scheduledChange') && cats.includes('accountNotification')) return 'both';
  if (cats.includes('accountNotification')) return 'accountNotification';
  return 'scheduledChange';
}

/** Map API eventCategories array to display label */
function categoriesToDisplayLabel(cats: string[]): string {
  if (cats.length >= 2 && cats.includes('scheduledChange') && cats.includes('accountNotification')) return 'Both';
  if (cats.includes('accountNotification')) return 'Account Notification';
  return 'Scheduled Change';
}

// --- Category Select Options ---

const CATEGORY_OPTIONS = [
  { label: 'Scheduled Change', value: 'scheduledChange' },
  { label: 'Account Notification', value: 'accountNotification' },
  { label: 'Both', value: 'both' },
];

// --- Component ---

export default function DispatchEditModal({ visible, initialConfig, onDismiss, onSave }: DispatchEditModalProps) {
  // --- Form state ---
  const [mode, setMode] = useState<'all' | 'ple_only' | 'custom'>('all');
  const [actionabilityFilter, setActionabilityFilter] = useState<'all_actionable' | 'action_required_only'>('all_actionable');
  const [rules, setRules] = useState<DispatchRule[]>([]);

  // --- Add-rule form state ---
  const [newPattern, setNewPattern] = useState('');
  const [newCategory, setNewCategory] = useState<string>('scheduledChange');
  const [patternError, setPatternError] = useState('');

  // --- Save state ---
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // --- Cancel confirmation ---
  const [showDiscardConfirm, setShowDiscardConfirm] = useState(false);

  // --- Dirty tracking snapshot ---
  const initialSnapshot = useRef<string>('');

  // --- State initialization on modal open ---
  useEffect(() => {
    if (!visible) return;
    const m = initialConfig.mode || 'all';
    const af = initialConfig.actionabilityFilter || 'all_actionable';
    const r = initialConfig.rules ? JSON.parse(JSON.stringify(initialConfig.rules)) : [];
    setMode(m);
    setActionabilityFilter(af);
    setRules(r);
    // Clear transient state
    setNewPattern('');
    setNewCategory('scheduledChange');
    setPatternError('');
    setSaving(false);
    setSaveError(null);
    setShowDiscardConfirm(false);
    // Store snapshot for dirty detection
    initialSnapshot.current = JSON.stringify({ mode: m, actionabilityFilter: af, rules: r });
  }, [visible, initialConfig]);

  // --- Derived values ---
  const isDirty = JSON.stringify({ mode, actionabilityFilter, rules }) !== initialSnapshot.current;
  const noRulesWarning = mode === 'custom' && rules.length === 0;
  const allDisabledWarning = mode === 'custom' && rules.length > 0 && rules.every(r => !r.enabled);

  // --- Handlers ---

  /** Dismiss with dirty check */
  const handleDismiss = () => {
    if (saving) return;
    if (isDirty) {
      setShowDiscardConfirm(true);
    } else {
      onDismiss();
    }
  };

  /** Add a new rule from the add-rule form */
  const handleAddRule = () => {
    setPatternError('');
    const trimmed = newPattern.trim();
    if (!trimmed) {
      setPatternError('Event type pattern is required');
      return;
    }
    if (!trimmed.toUpperCase().startsWith('AWS_')) {
      setPatternError('Pattern must start with AWS_');
      return;
    }
    const newRule: DispatchRule = {
      ruleId: `rule-${Date.now()}`,
      eventTypePattern: trimmed,
      eventCategories: selectValueToCategories(newCategory),
      enabled: true,
    };
    setRules([...rules, newRule]);
    setNewPattern('');
    // newCategory intentionally retained for rapid sequential adds
  };

  /** Remove a rule by ID */
  const handleRemoveRule = (ruleId: string) => {
    setRules(rules.filter(r => r.ruleId !== ruleId));
  };

  /** Toggle a rule's enabled state */
  const handleToggleRule = (ruleId: string) => {
    setRules(rules.map(r => r.ruleId === ruleId ? { ...r, enabled: !r.enabled } : r));
  };

  /** Save dispatch configuration */
  const handleSave = async () => {
    setSaving(true);
    setSaveError(null);
    try {
      await apiFetch('/config/dispatch', {
        method: 'POST',
        body: JSON.stringify(buildDispatchBody(mode, actionabilityFilter, rules)),
      });
      onSave();
    } catch (e: unknown) {
      setSaveError(parseApiError(e));
    } finally {
      setSaving(false);
    }
  };

  /** Discard changes and close */
  const handleDiscardConfirm = () => {
    setShowDiscardConfirm(false);
    onDismiss();
  };

  // --- Table column definitions ---
  const columnDefinitions = [
    {
      id: 'pattern',
      header: 'Event type pattern',
      cell: (rule: DispatchRule) => <Box variant="code">{rule.eventTypePattern}</Box>,
    },
    {
      id: 'category',
      header: 'Event category',
      cell: (rule: DispatchRule) => categoriesToDisplayLabel(rule.eventCategories),
    },
    {
      id: 'enabled',
      header: 'Enabled',
      cell: (rule: DispatchRule) => (
        <Toggle
          checked={rule.enabled}
          onChange={() => handleToggleRule(rule.ruleId)}
          ariaLabel={`Enable rule ${rule.eventTypePattern}`}
        />
      ),
    },
    {
      id: 'actions',
      header: '',
      cell: (rule: DispatchRule) => (
        <Button
          variant="icon"
          iconName="remove"
          ariaLabel={`Remove rule ${rule.eventTypePattern}`}
          onClick={() => handleRemoveRule(rule.ruleId)}
        />
      ),
    },
  ];

  // --- Render ---

  return (
    <>
      <Modal
        visible={visible}
        onDismiss={saving ? () => {} : handleDismiss}
        closeAriaLabel="Close"
        size="medium"
        header="Edit Dispatch Window"
        data-testid="dispatch-modal"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={handleDismiss}
                disabled={saving}
                data-testid="cancel-dispatch"
              >
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSave}
                loading={saving}
                data-testid="save-dispatch"
              >
                Save Changes
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="l">
          {/* Save error alert */}
          {saveError && (
            <Alert
              type="error"
              dismissible
              onDismiss={() => setSaveError(null)}
            >
              {saveError}
            </Alert>
          )}

          {/* Mode selection */}
          <FormField
            label="Dispatch mode"
            description="Controls which Health events create ITSM tickets"
          >
            <RadioGroup
              value={mode}
              onChange={({ detail }) => setMode(detail.value as 'all' | 'ple_only' | 'custom')}
              items={[
                {
                  value: 'all',
                  label: 'All actionable events',
                  description: 'Tickets for all scheduledChange and accountNotification events with ACTION_REQUIRED or ACTION_MAY_BE_REQUIRED',
                },
                {
                  value: 'ple_only',
                  label: 'Planned Lifecycle Events only',
                  description: 'Only event types ending with _PLANNED_LIFECYCLE_EVENT',
                },
                {
                  value: 'custom',
                  label: 'Custom rules',
                  description: 'Define specific event type patterns that create tickets',
                },
              ]}
            />
          </FormField>

          {/* Actionability filter */}
          <FormField
            label="Actionability filter"
            description="Which actionability levels create tickets"
          >
            <RadioGroup
              value={actionabilityFilter}
              onChange={({ detail }) => setActionabilityFilter(detail.value as 'all_actionable' | 'action_required_only')}
              items={[
                {
                  value: 'all_actionable',
                  label: 'All actionable events',
                  description: 'ACTION_REQUIRED + ACTION_MAY_BE_REQUIRED events create tickets',
                },
                {
                  value: 'action_required_only',
                  label: 'ACTION_REQUIRED only',
                  description: 'Only events explicitly marked ACTION_REQUIRED create tickets — reduces ticket noise',
                },
              ]}
            />
          </FormField>

          {/* Custom rules section — visible only when mode=custom */}
          {mode === 'custom' && (
            <Container
              header={
                <Header variant="h3">
                  Custom dispatch rules
                </Header>
              }
            >
              <SpaceBetween size="m">
                {/* Add rule form */}
                <Grid gridDefinition={[{ colspan: 6 }, { colspan: 4 }, { colspan: 2 }]}>
                  <FormField errorText={patternError}>
                    <Input
                      value={newPattern}
                      onChange={({ detail }) => { setNewPattern(detail.value); setPatternError(''); }}
                      placeholder="AWS_EKS_*"
                      ariaLabel="Event type pattern"
                      onKeyDown={({ detail }) => { if (detail.key === 'Enter') handleAddRule(); }}
                    />
                  </FormField>
                  <Select
                    selectedOption={CATEGORY_OPTIONS.find(o => o.value === newCategory) || CATEGORY_OPTIONS[0]}
                    onChange={({ detail }) => setNewCategory(detail.selectedOption.value || 'scheduledChange')}
                    options={CATEGORY_OPTIONS}
                    ariaLabel="Event category"
                  />
                  <Button
                    iconName="add-plus"
                    variant="normal"
                    onClick={handleAddRule}
                    ariaLabel="Add dispatch rule"
                    data-testid="add-dispatch-rule"
                  >
                    Add
                  </Button>
                </Grid>

                {/* Rules table */}
                <Table
                  variant="embedded"
                  items={rules}
                  columnDefinitions={columnDefinitions}
                  empty={
                    <Box textAlign="center" color="text-body-secondary" padding="s">
                      No custom rules defined. Add rules above to control which events create tickets.
                    </Box>
                  }
                  ariaLabels={{ tableLabel: 'Custom dispatch rules' }}
                />

                {/* Warning: no rules or all disabled */}
                {noRulesWarning && (
                  <Alert type="warning">
                    No dispatch rules configured. No tickets will be created for any Health events.
                  </Alert>
                )}
                {allDisabledWarning && (
                  <Alert type="warning">
                    All dispatch rules are disabled. No tickets will be created for any Health events.
                  </Alert>
                )}
              </SpaceBetween>
            </Container>
          )}
        </SpaceBetween>
      </Modal>

      {/* Discard confirmation dialog */}
      <Modal
        visible={showDiscardConfirm}
        onDismiss={() => setShowDiscardConfirm(false)}
        closeAriaLabel="Close"
        size="small"
        header="Discard unsaved changes?"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button
                variant="link"
                onClick={() => setShowDiscardConfirm(false)}
              >
                Keep editing
              </Button>
              <Button
                variant="primary"
                onClick={handleDiscardConfirm}
                data-testid="dispatch-discard"
              >
                Discard
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        Your dispatch window changes have not been saved and will be lost.
      </Modal>
    </>
  );
}
