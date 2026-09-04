import React, { useState } from 'react';
import Tabs from '@cloudscape-design/components/tabs';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Button from '@cloudscape-design/components/button';
import AttributeEditor from '@cloudscape-design/components/attribute-editor';
import RadioGroup from '@cloudscape-design/components/radio-group';
import Alert from '@cloudscape-design/components/alert';
import Spinner from '@cloudscape-design/components/spinner';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { apiFetch } from './api';

function RouteTestTab() {
  const [service, setService] = useState('EKS');
  const [accountId, setAccountId] = useState('');
  const [resourceTags, setResourceTags] = useState<{key:string,value:string}[]>([]);
  const [accountTags, setAccountTags] = useState<{key:string,value:string}[]>([]);
  const [eventTypeCode, setEventTypeCode] = useState('');
  const [traceResult, setTraceResult] = useState<any>(null);
  const [pipelineResult, setPipelineResult] = useState<any>(null);
  const [loading, setLoading] = useState(false);

  const buildPayload = () => {
    const tagsObj = Object.fromEntries(resourceTags.filter(t => t.key).map(t => [t.key, t.value]));
    const accTagsObj = Object.fromEntries(accountTags.filter(t => t.key).map(t => [t.key, t.value]));
    return { service, accountId, resourceTags: tagsObj, accountTags: accTagsObj, eventTypeCode };
  };

  const dryRun = async () => {
    setLoading(true);
    setTraceResult(null);
    setPipelineResult(null);
    try {
      const result = await apiFetch('/test/route', { method: 'POST', body: JSON.stringify(buildPayload()) });
      setTraceResult(result);
    } catch (e: any) {
      setTraceResult({ error: e.message });
    } finally {
      setLoading(false);
    }
  };

  const fullPipeline = async () => {
    setLoading(true);
    setTraceResult(null);
    setPipelineResult(null);
    try {
      const result = await apiFetch('/generate-events', { method: 'POST', body: JSON.stringify({ singleEvent: buildPayload() }) });
      setPipelineResult(result);
    } catch (e: any) {
      setPipelineResult({ error: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <SpaceBetween size="l">
      <Container header={<Header variant="h2">Route Test</Header>}>
        <SpaceBetween size="m">
          <Alert type="info">Test routing resolution without creating tickets (Dry Run) or send a full synthetic event through the pipeline.</Alert>
          <FormField label="Service">
            <Input value={service} onChange={({ detail }) => setService(detail.value)} placeholder="EKS" />
          </FormField>
          <FormField label="Account ID">
            <Input value={accountId} onChange={({ detail }) => setAccountId(detail.value)} placeholder="111111111111" />
          </FormField>
          <FormField label="Event Type Code">
            <Input value={eventTypeCode} onChange={({ detail }) => setEventTypeCode(detail.value)} placeholder="AWS_EKS_PLANNED_LIFECYCLE_EVENT" />
          </FormField>
          <FormField label="Resource Tags">
            <AttributeEditor
              items={resourceTags}
              onAddButtonClick={() => setResourceTags([...resourceTags, { key: '', value: '' }])}
              onRemoveButtonClick={({ detail: { itemIndex } }) => setResourceTags(resourceTags.filter((_, i) => i !== itemIndex))}
              addButtonText="Add tag"
              removeButtonText="Remove"
              empty="No resource tags"
              definition={[
                { label: 'Key', control: (item, idx) => <Input value={item.key} onChange={({ detail }) => { const t = [...resourceTags]; t[idx] = { ...t[idx], key: detail.value }; setResourceTags(t); }} placeholder="Team" /> },
                { label: 'Value', control: (item, idx) => <Input value={item.value} onChange={({ detail }) => { const t = [...resourceTags]; t[idx] = { ...t[idx], value: detail.value }; setResourceTags(t); }} placeholder="platform" /> },
              ]}
            />
          </FormField>
          <FormField label="Account Tags">
            <AttributeEditor
              items={accountTags}
              onAddButtonClick={() => setAccountTags([...accountTags, { key: '', value: '' }])}
              onRemoveButtonClick={({ detail: { itemIndex } }) => setAccountTags(accountTags.filter((_, i) => i !== itemIndex))}
              addButtonText="Add tag"
              removeButtonText="Remove"
              empty="No account tags"
              definition={[
                { label: 'Key', control: (item, idx) => <Input value={item.key} onChange={({ detail }) => { const t = [...accountTags]; t[idx] = { ...t[idx], key: detail.value }; setAccountTags(t); }} placeholder="Owner" /> },
                { label: 'Value', control: (item, idx) => <Input value={item.value} onChange={({ detail }) => { const t = [...accountTags]; t[idx] = { ...t[idx], value: detail.value }; setAccountTags(t); }} placeholder="cloud-platform" /> },
              ]}
            />
          </FormField>
          <SpaceBetween size="s" direction="horizontal">
            <Button onClick={dryRun} loading={loading}>Dry Run</Button>
            <Button variant="primary" onClick={fullPipeline} loading={loading}>Full Pipeline</Button>
          </SpaceBetween>
        </SpaceBetween>
      </Container>

      {loading && <Spinner size="large" />}

      {traceResult && (
        <Container header={<Header variant="h2">Routing Trace</Header>}>
          {traceResult.error ? (
            <Alert type="error">{traceResult.error}</Alert>
          ) : (
            <SpaceBetween size="s">
              {traceResult.resolvedProject && (
                <Alert type="success">Resolved to project: <strong>{traceResult.resolvedProject}</strong> (via {traceResult.resolvedBy})</Alert>
              )}
              {traceResult.fallbackChain?.map((step: any, i: number) => (
                <StatusIndicator key={i} type={step.result ? 'success' : 'stopped'}>
                  {step.method}: checked "{step.checked}" → {step.result || 'no match'}
                </StatusIndicator>
              ))}
              {!traceResult.resolvedProject && !traceResult.fallbackChain && (
                <Alert type="info"><pre>{JSON.stringify(traceResult, null, 2)}</pre></Alert>
              )}
            </SpaceBetween>
          )}
        </Container>
      )}

      {pipelineResult && (
        <Container header={<Header variant="h2">Pipeline Result</Header>}>
          {pipelineResult.error ? (
            <Alert type="error">{pipelineResult.error}</Alert>
          ) : (
            <Alert type="success">Event published successfully. {pipelineResult.published} event(s) sent. Click Sync to see results.</Alert>
          )}
        </Container>
      )}
    </SpaceBetween>
  );
}

function BulkGenerationTab() {
  const [count, setCount] = useState('10');
  const [generating, setGenerating] = useState(false);
  const [result, setResult] = useState<{ type: 'success' | 'error'; msg: string } | null>(null);

  const generate = async () => {
    setGenerating(true);
    setResult(null);
    try {
      const r = await apiFetch('/generate-events', {
        method: 'POST',
        body: JSON.stringify({ count: parseInt(count) }),
      });
      setResult({ type: 'success', msg: `${r.published || r.count} events triggered. Click Sync to see them.` });
    } catch (e: any) {
      setResult({ type: 'error', msg: e.message });
    } finally {
      setGenerating(false);
    }
  };

  return (
    <Container header={<Header variant="h2">Bulk Event Generation</Header>}>
      <SpaceBetween size="m">
        <Alert type="info">Inject synthetic Health events into the pipeline for testing.</Alert>
        <RadioGroup
          value={count}
          onChange={({ detail }) => setCount(detail.value)}
          items={[
            { value: '10', label: '10 events' },
            { value: '100', label: '100 events' },
            { value: '1000', label: '1000 events' },
          ]}
        />
        <Button variant="primary" loading={generating} onClick={generate}>Generate Events</Button>
        {result && (
          <StatusIndicator type={result.type}>{result.msg}</StatusIndicator>
        )}
      </SpaceBetween>
    </Container>
  );
}

export default function Testing() {
  return (
    <SpaceBetween size="l">
      <Header variant="h1">Testing</Header>
      <Tabs
        tabs={[
          { label: 'Route Test', id: 'route-test', content: <RouteTestTab /> },
          { label: 'Bulk Generation', id: 'bulk-generation', content: <BulkGenerationTab /> },
        ]}
      />
    </SpaceBetween>
  );
}
