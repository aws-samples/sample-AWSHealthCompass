import React, { useState, useEffect } from 'react';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import Alert from '@cloudscape-design/components/alert';
import Spinner from '@cloudscape-design/components/spinner';
import BarChart from '@cloudscape-design/components/bar-chart';
import { apiFetch } from './api';

interface CoverageData {
  coveragePercent: number;
  totalResources: number;
  routedResources: number;
  breakdown: { resourceTag: number; accountTag: number; account: number; default: number; failed: number };
  message: string;
}

interface UnroutableResource {
  resourceArn: string;
  accountId: string;
  campaignId: string;
  reason: string;
  service: string;
}

interface UnroutableData {
  unroutableCount: number;
  resources: UnroutableResource[];
}

export default function RoutingHealth() {
  const [coverage, setCoverage] = useState<CoverageData | null>(null);
  const [unroutable, setUnroutable] = useState<UnroutableData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [cov, unr] = await Promise.all([
          apiFetch('/routing/coverage'),
          apiFetch('/routing/coverage/unroutable'),
        ]);
        setCoverage(cov);
        setUnroutable(unr);
      } catch (e: any) {
        setError(e.message);
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  if (loading) return <Box textAlign="center" margin={{ top: 'xxxl' }}><Spinner size="large" /></Box>;
  if (error) return <Alert type="error">{error}</Alert>;
  if (!coverage) return null;

  const { coveragePercent, totalResources, breakdown, message } = coverage;
  const coverageColor = coveragePercent >= 80 ? 'text-status-success' : coveragePercent >= 50 ? 'text-status-warning' : 'text-status-error';
  const showSuggestion = totalResources > 0 && (breakdown.failed / totalResources) > 0.1;

  return (
    <SpaceBetween size="l">
      <Header variant="h1">Routing Health</Header>

      {/* Coverage Metric */}
      <Container header={<Header variant="h2">Coverage</Header>}>
        {totalResources === 0 ? (
          <Alert type="info">{message || 'No routing data available yet.'}</Alert>
        ) : (
          <Box fontSize="display-l" fontWeight="bold" color={coverageColor}>
            {coveragePercent}%
            <Box variant="small" color="text-body-secondary"> routed ({coverage.routedResources}/{totalResources} resources)</Box>
          </Box>
        )}
      </Container>

      {/* Breakdown Bar Chart */}
      {totalResources > 0 && (
        <Container header={<Header variant="h2">Routing Breakdown</Header>}>
          <BarChart
            series={[
              { title: 'Resource Tag', type: 'bar', data: [{ x: 'Routing', y: breakdown.resourceTag }], color: '#1b8a37' },
              { title: 'Account Tag', type: 'bar', data: [{ x: 'Routing', y: breakdown.accountTag }], color: '#0972d3' },
              { title: 'Account', type: 'bar', data: [{ x: 'Routing', y: breakdown.account }], color: '#007d8a' },
              { title: 'Default', type: 'bar', data: [{ x: 'Routing', y: breakdown.default }], color: '#d97706' },
              { title: 'Failed', type: 'bar', data: [{ x: 'Routing', y: breakdown.failed }], color: '#d91515' },
            ]}
            stackedBars
            horizontalBars
            hideFilter
            height={80}
            xScaleType="categorical"
            empty={<Box>No data</Box>}
            noMatch={<Box>No data</Box>}
          />
        </Container>
      )}

      {/* Suggestion Alert */}
      {showSuggestion && (
        <Alert type="warning">
          Over 10% of resources failed routing. Review your routing configuration — consider adding account mappings or tag routing rules.
        </Alert>
      )}

      {/* Unroutable Resources Table */}
      {unroutable && unroutable.resources.length > 0 && (
        <Container header={<Header variant="h2">Unroutable Resources ({unroutable.unroutableCount})</Header>}>
          <Table
            items={unroutable.resources}
            columnDefinitions={[
              { id: 'arn', header: 'Resource ARN', cell: (r) => <Box variant="code">{r.resourceArn}</Box> },
              { id: 'account', header: 'Account ID', cell: (r) => r.accountId },
              { id: 'service', header: 'Service', cell: (r) => r.service },
              { id: 'campaign', header: 'Campaign', cell: (r) => r.campaignId },
              { id: 'reason', header: 'Reason', cell: (r) => r.reason },
            ]}
            empty={<Box textAlign="center">No unroutable resources</Box>}
          />
        </Container>
      )}
    </SpaceBetween>
  );
}
