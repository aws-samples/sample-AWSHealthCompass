export type Platform = 'jira' | 'servicenow';

export interface PlatformLabels {
  routingTarget: string;
  routingTargetPlural: string;
  defaultRouting: string;
  defaultRoutingKey: string;
  ticketColumn: string;
  statusColumn: string;
  connectionHeader: string;
  testButton: string;
  bulkFormat: string;
  setupAlert: string;
  connectedAlert: string;
  createAlert: string;
  routingPlaceholder: string;
  orphanNote: string;
}

const JIRA_LABELS: PlatformLabels = {
  routingTarget: 'JIRA Project',
  routingTargetPlural: 'JIRA projects',
  defaultRouting: 'Default Project',
  defaultRoutingKey: 'Default Project Key',
  ticketColumn: 'JIRA Ticket',
  statusColumn: 'JIRA Status',
  connectionHeader: 'JIRA Connection',
  testButton: 'Test JIRA Connection',
  bulkFormat: 'account_id,jira_project',
  setupAlert: 'configure your JIRA connection and account routing to start creating tickets',
  connectedAlert: 'JIRA connected. Set up account routing to map Health events to the right JIRA projects.',
  createAlert: 'Tickets will be routed to JIRA projects based on your account mapping configuration.',
  routingPlaceholder: 'JIRA Project Key',
  orphanNote: 'Unmapped accounts will route to the default project.',
};

const SNOW_LABELS: PlatformLabels = {
  routingTarget: 'Assignment Group',
  routingTargetPlural: 'assignment groups',
  defaultRouting: 'Default Assignment Group',
  defaultRoutingKey: 'Default Assignment Group',
  ticketColumn: 'ServiceNow Record',
  statusColumn: 'ServiceNow State',
  connectionHeader: 'ServiceNow Connection',
  testButton: 'Test ServiceNow Connection',
  bulkFormat: 'account_id,assignment_group',
  setupAlert: 'configure your ServiceNow connection and account routing to start creating tickets',
  connectedAlert: 'ServiceNow connected. Set up account routing to map Health events to the right assignment groups.',
  createAlert: 'Tickets will be routed to assignment groups based on your account mapping configuration.',
  routingPlaceholder: 'Assignment Group Name',
  orphanNote: 'Unmapped accounts route to the default assignment group.',
};

export function getPlatformLabels(platform: Platform): PlatformLabels {
  return platform === 'jira' ? JIRA_LABELS : SNOW_LABELS;
}
