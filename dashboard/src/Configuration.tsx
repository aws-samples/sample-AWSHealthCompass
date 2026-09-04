import React, { useState } from 'react';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import type { OnboardingConfig } from './types';
import ConfigurationWizard from './ConfigurationWizard';
import ConfigurationSummary from './ConfigurationSummary';

interface Props {
  config: OnboardingConfig | null;
  onSave: () => void;
}

/**
 * Configuration page orchestrator.
 * Decides whether to render the summary landing page or the setup wizard
 * based on the current configuration state.
 *
 * Decision logic:
 * - config is null → loading spinner (App.tsx still fetching)
 * - First-time user (no ITSM connection AND no default routing) → wizard
 * - forceWizard toggled by user → wizard
 * - Otherwise → summary landing page
 */
export default function Configuration({ config, onSave }: Props) {
  const [forceWizard, setForceWizard] = useState(false);

  // Loading state — App.tsx hasn't loaded config yet
  if (config === null) {
    return (
      <Box textAlign="center" margin={{ top: 'xxxl' }}>
        <Spinner size="large" />
      </Box>
    );
  }

  // Determine if this is a first-time user with no configuration at all
  const hasJiraConnection = config.jira?.validated === true;
  const hasSnowConnection = config.servicenow?.validated === true;
  const hasAnyConnection = hasJiraConnection || hasSnowConnection;
  // STORY-139 (§5.2, F-7): platform-aware routing signal. Keying solely off the
  // JIRA-named `routing.defaultProject` misclassified a returning SNOW-only
  // customer (who has SNOW routing but no JIRA default project) as a first-time
  // user. `hasAnyConnection` already covers the common returning-SNOW case;
  // this makes the routing signal itself platform-neutral. JIRA-only is
  // unchanged (AC-139.9): hasSnowRouting is false, hasJiraRouting reduces to the
  // prior check.
  const hasJiraRouting = !!config.routing?.defaultProject;
  const hasSnowRouting =
    !!(config.routing as any)?.snowAssignmentGroupId ||
    (config.routing?.accountMappingCount ?? 0) > 0;
  const hasRouting = hasJiraRouting || hasSnowRouting;
  const isFirstTimeUser = !hasAnyConnection && !hasRouting;

  // Show wizard if: first-time user OR user explicitly requested it
  const showWizard = isFirstTimeUser || forceWizard;

  if (showWizard) {
    return (
      <ConfigurationWizard
        config={config}
        onSave={() => {
          setForceWizard(false);
          onSave();
        }}
      />
    );
  }

  return (
    <ConfigurationSummary
      config={config}
      onRunWizard={() => setForceWizard(true)}
      onConfigChanged={onSave}
    />
  );
}
