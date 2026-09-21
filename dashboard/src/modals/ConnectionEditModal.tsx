import React, { useState, useEffect, useRef } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Checkbox from '@cloudscape-design/components/checkbox';
import Alert from '@cloudscape-design/components/alert';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import type { OnboardingConfig } from '../types';
import { apiFetch } from '../api';

// --- Types ---

interface ConnectionEditModalProps {
  /** Controls modal visibility */
  visible: boolean;
  /** Current configuration state (for pre-population) */
  config: OnboardingConfig;
  /** Called when modal should close without saving */
  onDismiss: () => void;
  /** Called after successful save — parent should refresh config */
  onSave: () => void;
}

type StepResult = 'pending' | 'success' | 'error' | 'skipped';

interface SaveStatus {
  integrations: StepResult;
  jira: StepResult;
  servicenow: StepResult;
}

// --- Validation helpers ---

/**
 * SEC-7: Validate JIRA Cloud URL format.
 * Must be HTTPS and end with .atlassian.net.
 * Rejects localhost, internal IPs, non-HTTPS.
 */
function validateJiraUrl(url: string): string | null {
  if (!url.trim()) return 'JIRA Base URL is required.';
  if (!url.startsWith('https://')) return 'Must be a valid JIRA Cloud URL (https://org.atlassian.net).';
  try {
    const parsed = new URL(url);
    const hostname = parsed.hostname.toLowerCase();
    if (!hostname.endsWith('.atlassian.net')) {
      return 'Must be a valid JIRA Cloud URL (https://org.atlassian.net).';
    }
    // SEC-7: Reject localhost and internal IPs
    if (hostname === 'localhost' || hostname === '127.0.0.1' || /^(10|172\.(1[6-9]|2\d|3[01])|192\.168)\./.test(hostname)) {
      return 'Must be a valid JIRA Cloud URL (https://org.atlassian.net).';
    }
  } catch {
    return 'Must be a valid JIRA Cloud URL (https://org.atlassian.net).';
  }
  return null;
}

/**
 * SEC-7: Validate ServiceNow URL format.
 * Must be HTTPS and end with .service-now.com.
 */
function validateSnowUrl(url: string): string | null {
  if (!url.trim()) return 'Instance URL is required.';
  if (!url.startsWith('https://')) return 'Must be a valid ServiceNow URL (https://org.service-now.com).';
  try {
    const parsed = new URL(url);
    const hostname = parsed.hostname.toLowerCase();
    if (!hostname.endsWith('.service-now.com')) {
      return 'Must be a valid ServiceNow URL (https://org.service-now.com).';
    }
    if (hostname === 'localhost' || hostname === '127.0.0.1' || /^(10|172\.(1[6-9]|2\d|3[01])|192\.168)\./.test(hostname)) {
      return 'Must be a valid ServiceNow URL (https://org.service-now.com).';
    }
  } catch {
    return 'Must be a valid ServiceNow URL (https://org.service-now.com).';
  }
  return null;
}

/**
 * Parse API error messages for user-friendly display.
 * SEC-4: Truncate to prevent unexpected data exposure.
 */
function parseApiError(error: unknown): string {
  if (!(error instanceof Error)) return 'An unexpected error occurred.';
  const msg = error.message;
  const match = msg.match(/^API (\d+): (.+)$/s);
  if (!match) return 'Failed to connect. Check your network and try again.';
  const status = parseInt(match[1], 10);
  const body = match[2].substring(0, 200);
  if (status === 400) return body;
  if (status === 401) return 'Authentication failed. Check your credentials.';
  if (status === 403) return 'Unable to save — check your dashboard session is still active.';
  if (status === 429) return 'Too many requests. Please wait a moment and try again.';
  if (status >= 500) return 'Server error. Please try again later.';
  return body;
}

// --- Component ---

export default function ConnectionEditModal({ visible, config, onDismiss, onSave }: ConnectionEditModalProps) {
  // --- Platform toggles ---
  const [jiraEnabled, setJiraEnabled] = useState(false);
  const [snowEnabled, setSnowEnabled] = useState(false);

  // --- JIRA form fields ---
  const [jiraBaseUrl, setJiraBaseUrl] = useState('');
  const [jiraEmail, setJiraEmail] = useState('');
  const [jiraApiToken, setJiraApiToken] = useState('');

  // --- ServiceNow form fields ---
  const [snowInstanceUrl, setSnowInstanceUrl] = useState('');
  const [snowClientId, setSnowClientId] = useState('');
  const [snowClientSecret, setSnowClientSecret] = useState('');
  const [snowUsername, setSnowUsername] = useState('');
  const [snowPassword, setSnowPassword] = useState('');

  // --- Test connection state ---
  const [jiraTesting, setJiraTesting] = useState(false);
  const [jiraTestResult, setJiraTestResult] = useState<{ ok: boolean; msg: string } | null>(null);
  const [snowTesting, setSnowTesting] = useState(false);
  const [snowTestResult, setSnowTestResult] = useState<{ ok: boolean; msg: string } | null>(null);

  // --- Credential edit mode flags ---
  const [jiraTokenEditMode, setJiraTokenEditMode] = useState(false);
  const [snowSecretEditMode, setSnowSecretEditMode] = useState(false);
  const [snowPasswordEditMode, setSnowPasswordEditMode] = useState(false);

  // Refs for focus management
  const jiraTokenInputRef = useRef<HTMLInputElement>(null);
  const snowSecretInputRef = useRef<HTMLInputElement>(null);
  const snowPasswordInputRef = useRef<HTMLInputElement>(null);

  // --- Save state ---
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<SaveStatus>({
    integrations: 'pending',
    jira: 'pending',
    servicenow: 'pending',
  });

  // --- Validation errors ---
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  // --- Derived values ---
  // canTest allows testing with stored credentials (sentinel state)
  const jiraCredsConfigured = !!(config.jira?.credentialsConfigured || config.jira?.validated);
  const snowCredsConfigured = !!(config.servicenow?.credentialsConfigured || config.servicenow?.validated);

  const canTestJira = !!(
    jiraBaseUrl.trim() && jiraEmail.trim() &&
    (
      (jiraTokenEditMode && jiraApiToken.trim()) ||
      (!jiraTokenEditMode && jiraCredsConfigured)
    )
  );
  const canTestSnow = !!(
    snowInstanceUrl.trim() && snowClientId.trim() && snowUsername.trim() &&
    (
      ((snowSecretEditMode && snowClientSecret.trim()) || (!snowSecretEditMode && snowCredsConfigured)) &&
      ((snowPasswordEditMode && snowPassword.trim()) || (!snowPasswordEditMode && snowCredsConfigured))
    )
  );
  const canSave = (jiraEnabled || snowEnabled) && !saving;

  // --- State initialization on modal open ---
  useEffect(() => {
    if (!visible) return;
    // Derive platforms from config
    const platforms = config.platforms || [config.platform || 'jira'];
    setJiraEnabled(platforms.includes('jira'));
    setSnowEnabled(platforms.includes('servicenow'));
    // JIRA fields — pre-populate non-sensitive fields
    setJiraBaseUrl(config.jira?.baseUrl ?? '');
    setJiraEmail(config.jira?.email ?? config.jira?.validatedUser ?? '');
    setJiraApiToken(''); // SEC-8: ALWAYS blank — secrets never pre-populated
    // ServiceNow fields — pre-populate non-sensitive fields
    setSnowInstanceUrl(config.servicenow?.instanceUrl ?? '');
    setSnowClientId(config.servicenow?.clientId ?? '');
    setSnowClientSecret(''); // SEC-8: ALWAYS blank
    setSnowUsername(config.servicenow?.username ?? '');
    setSnowPassword(''); // SEC-8: ALWAYS blank
    // Reset credential edit modes (start in sentinel state if configured)
    setJiraTokenEditMode(false);
    setSnowSecretEditMode(false);
    setSnowPasswordEditMode(false);
    // Clear transient state
    setJiraTestResult(null);
    setSnowTestResult(null);
    setSaveError(null);
    setSaveStatus({ integrations: 'pending', jira: 'pending', servicenow: 'pending' });
    setFieldErrors({});
  }, [visible, config]);

  // --- Handlers ---

  /**
   * SEC-2: Clear all credential state before dismissing.
   * ADVISORY-1: Clear credential input state on modal dismiss.
   */
  const handleCancel = () => {
    setJiraApiToken('');
    setSnowClientSecret('');
    setSnowPassword('');
    setJiraTokenEditMode(false);
    setSnowSecretEditMode(false);
    setSnowPasswordEditMode(false);
    onDismiss();
  };

  /**
   * Test JIRA connection.
   * If credentials unchanged (sentinel state), omit apiToken from body.
   * Backend uses stored credentials for the test.
   * SEC-3: No credential values logged.
   */
  const testJira = async () => {
    setJiraTesting(true);
    setJiraTestResult(null);
    try {
      const testBody: Record<string, string> = { baseUrl: jiraBaseUrl, email: jiraEmail };
      // Only include apiToken if user has entered a new value
      if (jiraTokenEditMode && jiraApiToken.trim()) {
        testBody.apiToken = jiraApiToken;
      }
      const r = await apiFetch('/config/jira/test', {
        method: 'POST',
        body: JSON.stringify(testBody),
      });
      if (r.status === 'connected') {
        setJiraTestResult({ ok: true, msg: `Connected as ${r.user}` });
      } else {
        setJiraTestResult({ ok: false, msg: r.message || 'Connection failed' });
      }
    } catch (e: unknown) {
      setJiraTestResult({ ok: false, msg: parseApiError(e) });
    } finally {
      setJiraTesting(false);
    }
  };

  /**
   * Test ServiceNow connection via POST /config/servicenow/test.
   * Omit credential fields that are unchanged (sentinel state).
   * SEC-3: No credential values logged.
   */
  const testServiceNow = async () => {
    setSnowTesting(true);
    setSnowTestResult(null);
    try {
      const testBody: Record<string, string> = {
        instanceUrl: snowInstanceUrl,
        clientId: snowClientId,
        username: snowUsername,
      };
      // Only include credential fields if user has entered new values
      if (snowSecretEditMode && snowClientSecret.trim()) {
        testBody.clientSecret = snowClientSecret;
      }
      if (snowPasswordEditMode && snowPassword.trim()) {
        testBody.password = snowPassword;
      }
      const r = await apiFetch('/config/servicenow/test', {
        method: 'POST',
        body: JSON.stringify(testBody),
      });
      if (r.valid) {
        const roles = (r.roles || []).join(', ');
        setSnowTestResult({
          ok: true,
          msg: `Connected as: ${r.displayName || snowUsername}${roles ? ` | Roles: ${roles}` : ''}`,
        });
      } else {
        setSnowTestResult({ ok: false, msg: (r.errors || ['Validation failed']).join('; ') });
      }
    } catch (e: unknown) {
      setSnowTestResult({ ok: false, msg: parseApiError(e) });
    } finally {
      setSnowTesting(false);
    }
  };

  /**
   * Client-side validation before save.
   * Credential fields only required for first-time setup or when edit mode active.
   * Returns true if all fields are valid.
   */
  const validate = (): boolean => {
    const errors: Record<string, string> = {};

    if (jiraEnabled) {
      const urlErr = validateJiraUrl(jiraBaseUrl);
      if (urlErr) errors.jiraBaseUrl = urlErr;

      if (!jiraEmail.trim()) {
        errors.jiraEmail = 'Email is required.';
      }

      // Token required only for first-time setup (no existing credentials)
      if (!jiraCredsConfigured && !jiraApiToken.trim()) {
        errors.jiraApiToken = 'API token is required for first-time JIRA setup.';
      }
    }

    if (snowEnabled) {
      const urlErr = validateSnowUrl(snowInstanceUrl);
      if (urlErr) errors.snowInstanceUrl = urlErr;

      if (!snowClientId.trim()) {
        errors.snowClientId = 'Client ID is required.';
      }
      if (!snowUsername.trim()) {
        errors.snowUsername = 'Username is required.';
      }

      // First-time setup: secrets required
      if (!snowCredsConfigured && !snowClientSecret.trim()) {
        errors.snowClientSecret = 'Client Secret is required for first-time ServiceNow setup.';
      }
      if (!snowCredsConfigured && !snowPassword.trim()) {
        errors.snowPassword = 'Password is required for first-time ServiceNow setup.';
      }
    }

    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  /**
   * Save connection changes.
   * Credential keys are conditionally included in request bodies.
   * - If credential edit mode is inactive (sentinel state) → omit key → backend preserves.
   * - If credential edit mode is active with a value → include key → backend updates.
   * SEC-3: No credential values logged.
   */
  const handleSave = async () => {
    if (!validate()) return;

    setSaving(true);
    setSaveError(null);
    const errors: string[] = [];
    const status: SaveStatus = { ...saveStatus };

    // Step 1: Save enabled platforms (always)
    if (status.integrations !== 'success') {
      try {
        const platforms: string[] = [];
        if (jiraEnabled) platforms.push('jira');
        if (snowEnabled) platforms.push('servicenow');
        await apiFetch('/config/integrations', {
          method: 'PUT',
          body: JSON.stringify({ platforms }),
        });
        status.integrations = 'success';
      } catch (e: unknown) {
        status.integrations = 'error';
        errors.push(`Platform settings: ${parseApiError(e)}`);
        setSaveStatus(status);
        setSaveError(errors.join('\n'));
        setSaving(false);
        return;
      }
    }

    // Step 2: Save JIRA (always attempt if enabled — partial or full)
    if (jiraEnabled && status.jira !== 'success') {
      try {
        const jiraBody: Record<string, string> = {
          baseUrl: jiraBaseUrl,
          email: jiraEmail,
        };
        // Only include apiToken if user activated edit mode AND typed a value
        if (jiraTokenEditMode && jiraApiToken.trim()) {
          jiraBody.apiToken = jiraApiToken;
        }
        await apiFetch('/config/jira', {
          method: 'POST',
          body: JSON.stringify(jiraBody),
        });
        status.jira = 'success';
      } catch (e: unknown) {
        status.jira = 'error';
        const errMsg = parseApiError(e);
        errors.push(`JIRA: ${errMsg}`);
        // Auto-activate credential edit mode on auth failure
        if (errMsg.toLowerCase().includes('credential') || errMsg.toLowerCase().includes('authentication')) {
          setJiraTokenEditMode(true);
        }
      }
    } else if (status.jira === 'pending') {
      status.jira = 'skipped';
    }

    // Step 3: Save ServiceNow (always attempt if enabled — partial or full)
    if (snowEnabled && status.servicenow !== 'success') {
      try {
        const snowBody: Record<string, string> = {
          instanceUrl: snowInstanceUrl,
          clientId: snowClientId,
          username: snowUsername,
        };
        // Only include credential fields if user entered new values
        if (snowSecretEditMode && snowClientSecret.trim()) {
          snowBody.clientSecret = snowClientSecret;
        }
        if (snowPasswordEditMode && snowPassword.trim()) {
          snowBody.password = snowPassword;
        }
        await apiFetch('/config/servicenow', {
          method: 'POST',
          body: JSON.stringify(snowBody),
        });
        status.servicenow = 'success';
      } catch (e: unknown) {
        status.servicenow = 'error';
        const errMsg = parseApiError(e);
        errors.push(`ServiceNow: ${errMsg}`);
        // Auto-activate credential edit mode on auth failure
        if (errMsg.toLowerCase().includes('credential') || errMsg.toLowerCase().includes('authentication')) {
          setSnowSecretEditMode(true);
          setSnowPasswordEditMode(true);
        }
      }
    } else if (status.servicenow === 'pending') {
      status.servicenow = 'skipped';
    }

    setSaveStatus(status);
    setSaving(false);

    if (errors.length > 0) {
      setSaveError(errors.join('\n'));
    } else {
      // SEC-2: Clear credentials before closing
      setJiraApiToken('');
      setSnowClientSecret('');
      setSnowPassword('');
      setJiraTokenEditMode(false);
      setSnowSecretEditMode(false);
      setSnowPasswordEditMode(false);
      onSave();
    }
  };

  /**
   * Clear a specific field error when the user modifies that field.
   */
  const clearFieldError = (field: string) => {
    if (fieldErrors[field]) {
      setFieldErrors((prev) => {
        const next = { ...prev };
        delete next[field];
        return next;
      });
    }
  };

  // --- Render ---

  return (
    <Modal
      visible={visible}
      onDismiss={saving ? () => {} : handleCancel}
      closeAriaLabel="Close"
      size="medium"
      header="Edit ITSM Connections"
      data-testid="connection-modal"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button
              variant="link"
              onClick={handleCancel}
              disabled={saving}
              data-testid="cancel-connections"
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleSave}
              loading={saving}
              disabled={!canSave}
              data-testid="save-connections"
            >
              Save
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">

        {/* Platform Toggle Section */}
        <SpaceBetween size="s">
          <Box variant="h3">Platforms</Box>
          <Checkbox
            checked={jiraEnabled}
            onChange={({ detail }) => { setJiraEnabled(detail.checked); }}
            disabled={saving}
          >
            JIRA Cloud —{' '}
            <Box variant="small" display="inline" color="text-body-secondary">
              Atlassian-hosted JIRA (*.atlassian.net)
            </Box>
          </Checkbox>
          <Checkbox
            checked={snowEnabled}
            onChange={({ detail }) => { setSnowEnabled(detail.checked); }}
            disabled={saving}
          >
            ServiceNow —{' '}
            <Box variant="small" display="inline" color="text-body-secondary">
              ServiceNow ITSM instance (*.service-now.com)
            </Box>
          </Checkbox>
          {!jiraEnabled && !snowEnabled && (
            <Alert type="warning">At least one platform must be enabled.</Alert>
          )}
        </SpaceBetween>

        {/* JIRA Connection Form */}
        {jiraEnabled && (
          <Container header={<Header variant="h3">JIRA Connection</Header>}>
            <SpaceBetween size="m">

              <FormField
                label="JIRA Base URL"
                errorText={fieldErrors.jiraBaseUrl}
              >
                <Input
                  value={jiraBaseUrl}
                  onChange={({ detail }) => { setJiraBaseUrl(detail.value); clearFieldError('jiraBaseUrl'); }}
                  placeholder="https://yourorg.atlassian.net"
                  disabled={saving}
                  autoComplete="url"
                />
              </FormField>

              <FormField
                label="JIRA Email"
                errorText={fieldErrors.jiraEmail}
              >
                <Input
                  value={jiraEmail}
                  onChange={({ detail }) => { setJiraEmail(detail.value); clearFieldError('jiraEmail'); }}
                  placeholder="automation@company.com"
                  disabled={saving}
                  autoComplete="one-time-code"
                />
              </FormField>

              <FormField
                label="JIRA API Token"
                description={
                  jiraTokenEditMode
                    ? 'Enter a new API token to replace the existing one.'
                    : 'Stored in AWS Secrets Manager.'
                }
                errorText={fieldErrors.jiraApiToken}
              >
                {/* Credential sentinel pattern */}
                {jiraCredsConfigured && !jiraTokenEditMode ? (
                  <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                    <StatusIndicator type="success">Configured</StatusIndicator>
                    <Button
                      variant="inline-link"
                      onClick={() => setJiraTokenEditMode(true)}
                      disabled={saving}
                      ariaLabel="Change JIRA API Token"
                    >
                      Change
                    </Button>
                  </SpaceBetween>
                ) : (
                  <SpaceBetween size="xxs">
                    <Input
                      ref={jiraTokenInputRef}
                      value={jiraApiToken}
                      onChange={({ detail }) => { setJiraApiToken(detail.value); clearFieldError('jiraApiToken'); }}
                      type="password"
                      disabled={saving}
                      autoComplete="one-time-code"
                      autoFocus={jiraTokenEditMode && jiraCredsConfigured}
                    />
                    {jiraTokenEditMode && jiraCredsConfigured && (
                      <Button
                        variant="inline-link"
                        onClick={() => { setJiraTokenEditMode(false); setJiraApiToken(''); }}
                        disabled={saving}
                        ariaLabel="Cancel change to JIRA API Token"
                      >
                        Cancel change
                      </Button>
                    )}
                  </SpaceBetween>
                )}
              </FormField>

              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  onClick={testJira}
                  loading={jiraTesting}
                  disabled={!canTestJira || saving}
                  ariaLabel="Test JIRA connection"
                  data-testid="test-jira-connection"
                >
                  Test Connection
                </Button>
                {jiraTestResult && (
                  <StatusIndicator type={jiraTestResult.ok ? 'success' : 'error'}>
                    {jiraTestResult.msg}
                  </StatusIndicator>
                )}
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}

        {/* ServiceNow Connection Form */}
        {snowEnabled && (
          <Container header={<Header variant="h3">ServiceNow Connection</Header>}>
            <SpaceBetween size="m">

              <FormField
                label="Instance URL"
                constraintText="Must be https:// and end with .service-now.com"
                errorText={fieldErrors.snowInstanceUrl}
              >
                <Input
                  value={snowInstanceUrl}
                  onChange={({ detail }) => { setSnowInstanceUrl(detail.value); clearFieldError('snowInstanceUrl'); }}
                  placeholder="https://yourorg.service-now.com"
                  disabled={saving}
                  autoComplete="url"
                />
              </FormField>

              <FormField
                label="Client ID"
                description="OAuth application Client ID"
                errorText={fieldErrors.snowClientId}
              >
                <Input
                  value={snowClientId}
                  onChange={({ detail }) => { setSnowClientId(detail.value); clearFieldError('snowClientId'); }}
                  placeholder="OAuth Client ID"
                  disabled={saving}
                  autoComplete="one-time-code"
                />
              </FormField>

              <FormField
                label="Client Secret"
                description={
                  snowSecretEditMode
                    ? 'Enter a new Client Secret to replace the existing one.'
                    : 'Stored in AWS Secrets Manager.'
                }
                errorText={fieldErrors.snowClientSecret}
              >
                {/* Credential sentinel pattern */}
                {snowCredsConfigured && !snowSecretEditMode ? (
                  <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                    <StatusIndicator type="success">Configured</StatusIndicator>
                    <Button
                      variant="inline-link"
                      onClick={() => setSnowSecretEditMode(true)}
                      disabled={saving}
                      ariaLabel="Change ServiceNow Client Secret"
                    >
                      Change
                    </Button>
                  </SpaceBetween>
                ) : (
                  <SpaceBetween size="xxs">
                    <Input
                      ref={snowSecretInputRef}
                      value={snowClientSecret}
                      onChange={({ detail }) => { setSnowClientSecret(detail.value); clearFieldError('snowClientSecret'); }}
                      type="password"
                      disabled={saving}
                      autoComplete="one-time-code"
                      autoFocus={snowSecretEditMode && snowCredsConfigured}
                    />
                    {snowSecretEditMode && snowCredsConfigured && (
                      <Button
                        variant="inline-link"
                        onClick={() => { setSnowSecretEditMode(false); setSnowClientSecret(''); }}
                        disabled={saving}
                        ariaLabel="Cancel change to ServiceNow Client Secret"
                      >
                        Cancel change
                      </Button>
                    )}
                  </SpaceBetween>
                )}
              </FormField>

              <FormField
                label="Username"
                description="Integration user (requires 'itil' role)"
                errorText={fieldErrors.snowUsername}
              >
                <Input
                  value={snowUsername}
                  onChange={({ detail }) => { setSnowUsername(detail.value); clearFieldError('snowUsername'); }}
                  placeholder="resolve_integration"
                  disabled={saving}
                  autoComplete="one-time-code"
                />
              </FormField>

              <FormField
                label="Password"
                description={
                  snowPasswordEditMode
                    ? 'Enter a new password to replace the existing one.'
                    : 'Stored in AWS Secrets Manager.'
                }
                errorText={fieldErrors.snowPassword}
              >
                {/* Credential sentinel pattern */}
                {snowCredsConfigured && !snowPasswordEditMode ? (
                  <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                    <StatusIndicator type="success">Configured</StatusIndicator>
                    <Button
                      variant="inline-link"
                      onClick={() => setSnowPasswordEditMode(true)}
                      disabled={saving}
                      ariaLabel="Change ServiceNow Password"
                    >
                      Change
                    </Button>
                  </SpaceBetween>
                ) : (
                  <SpaceBetween size="xxs">
                    <Input
                      ref={snowPasswordInputRef}
                      value={snowPassword}
                      onChange={({ detail }) => { setSnowPassword(detail.value); clearFieldError('snowPassword'); }}
                      type="password"
                      disabled={saving}
                      autoComplete="one-time-code"
                      autoFocus={snowPasswordEditMode && snowCredsConfigured}
                    />
                    {snowPasswordEditMode && snowCredsConfigured && (
                      <Button
                        variant="inline-link"
                        onClick={() => { setSnowPasswordEditMode(false); setSnowPassword(''); }}
                        disabled={saving}
                        ariaLabel="Cancel change to ServiceNow Password"
                      >
                        Cancel change
                      </Button>
                    )}
                  </SpaceBetween>
                )}
              </FormField>

              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  onClick={testServiceNow}
                  loading={snowTesting}
                  disabled={!canTestSnow || saving}
                  ariaLabel="Test ServiceNow connection"
                  data-testid="test-snow-connection"
                >
                  Test Connection
                </Button>
                {snowTestResult && (
                  <StatusIndicator type={snowTestResult.ok ? 'success' : 'error'}>
                    {snowTestResult.msg}
                  </StatusIndicator>
                )}
              </SpaceBetween>
            </SpaceBetween>
          </Container>
        )}

        {/* Save error display */}
        {saveError && (
          <Alert type="error" header="Save failed">
            {saveError}
          </Alert>
        )}

      </SpaceBetween>
    </Modal>
  );
}
