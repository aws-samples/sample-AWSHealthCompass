# Requirements Document

## Introduction

AWS Health Compass is a mature serverless solution that automates the creation and management of tickets from AWS Health planned lifecycle events across an AWS organization. This enhancement project aims to improve the existing system's extensibility, operational capabilities, and integration options while maintaining backward compatibility with the current production deployment.

## Glossary

- **Health_Compass**: The AWS Health Compass serverless solution
- **Health_Event**: AWS Health planned lifecycle events from AWS Health API
- **ITSM_System**: IT Service Management systems (Jira, ServiceNow, PagerDuty, etc.)
- **Event_Processor**: Lambda function that processes and categorizes AWS Health events
- **Ticket_Integrator**: Lambda functions that create/update tickets in ITSM systems
- **Routing_Engine**: System component that determines where events should be routed
- **Event_Store**: DynamoDB tables storing event tracking and configuration data
- **Credential_Manager**: AWS Secrets Manager for secure credential storage
- **Deployment_Model**: Account-based, Service-based, or Tag-based routing configuration

## Requirements

### Requirement 1: Enhanced ITSM Integration Framework

**User Story:** As a DevOps engineer, I want to integrate AWS Health Compass with additional ITSM systems beyond the current Jira and ServiceNow support, so that I can route events to the tools my organization uses.

#### Acceptance Criteria

1. THE Health_Compass SHALL maintain existing Jira integration with advanced features including project mapping, issue type configuration, ticket updates, and deployment model support
2. THE Health_Compass SHALL maintain existing ServiceNow integration and enhance it with change management capabilities distinct from the Jira integration model
3. THE Health_Compass SHALL add PagerDuty integration for incident creation with severity mapping and escalation policies
4. THE Health_Compass SHALL add Slack integration for notification delivery with channel routing and message formatting
5. THE Health_Compass SHALL add Microsoft Teams integration for notification delivery with team routing and adaptive card formatting
6. WHEN a new ITSM integration is added, THE Health_Compass SHALL maintain existing Jira and ServiceNow integrations without disruption
7. THE Health_Compass SHALL provide a standardized integration interface for adding new ITSM systems while allowing system-specific capabilities (e.g., ServiceNow change management, Jira issue tracking)
8. WHEN multiple ITSM systems are configured, THE Health_Compass SHALL route events to all configured systems simultaneously based on routing rules

### Requirement 2: ServiceNow Change Management Integration

**User Story:** As a system administrator, I want ServiceNow integration built around ServiceNow's native change management capabilities while reusing the proven Jira infrastructure patterns, so that AWS Health planned lifecycle events are managed through proper change workflows without duplicating shared logic.

#### Acceptance Criteria

##### Shared Foundation (reuse existing Jira patterns)

1. THE Health_Compass SHALL reuse the existing event processing pipeline architecture (EventBridge → SQS → Processor Lambda → SQS → Integration Lambda) for ServiceNow, mirroring the proven Jira pattern
2. THE Health_Compass SHALL reuse the existing HealthEventProcessorLambda logic for event ingestion, resource tagging, and resource grouping, avoiding duplication of the shared event processing code
3. THE Health_Compass SHALL implement ServiceNow deployment-model-specific DynamoDB mapping tables (Account, Service, Tag) following the same pattern as Jira's `AccountJiraTable`, `ServiceJiraTable`, and `TagJiraTable`
4. THE Health_Compass SHALL implement ServiceNow credential management with rotation support via AWS Secrets Manager, following the same pattern as Jira's `JiraTokenSecret`
5. THE Health_Compass SHALL reuse the existing event tracking table (`TrackEventTable`) pattern for ServiceNow change request tracking

##### ServiceNow-Specific Change Management

6. THE Health_Compass SHALL create a single ServiceNow Change Request per overall lifecycle event, grouping related AWS Health events that share the same `detail.eventMetadata.deprecated_versions` value across regions
7. THE Health_Compass SHALL support configurable change request types, allowing users to define whether planned lifecycle events create Standard or Normal changes, with Standard as the default
8. WHEN a Change Request is created, THE Health_Compass SHALL create individual Change Tasks for each affected AWS resource within that Change
9. WHEN ServiceNow CIs exist for affected AWS resources, THE Health_Compass SHALL link each Change Task to its corresponding Configuration Item in the CMDB
10. WHEN ServiceNow CIs do not exist for affected AWS resources, THE Health_Compass SHALL still create individual Change Tasks per resource with resource details captured in the task description

##### Assignment Group and Ownership

11. THE Health_Compass SHALL support three assignment group models: (a) a single team owns the entire Change, (b) resources in the same AWS account are assigned to the same team, (c) each resource/CI is individually assigned to its owning team
12. THE Health_Compass SHALL determine resource and assignment group ownership primarily through tags on the AWS resource or account
13. THE Health_Compass SHALL support an optional customer-provided API for looking up resource ownership when tag-based mapping is insufficient

##### Backward Compatibility

14. WHEN ServiceNow enhancements are deployed, THE Health_Compass SHALL maintain backward compatibility with existing ServiceNow configurations

### Requirement 3: Advanced Event Filtering and Routing

**User Story:** As a system administrator, I want enhanced filtering and routing capabilities, so that I can precisely control which events go to which teams and systems.

#### Acceptance Criteria

1. THE Routing_Engine SHALL support filtering by AWS service type with granular service-specific rules
2. THE Routing_Engine SHALL support filtering by event severity levels (low, medium, high, critical)
3. THE Routing_Engine SHALL support filtering by geographic region with multi-region awareness
4. THE Routing_Engine SHALL support time-based routing rules for business hours and maintenance windows
5. WHEN multiple routing rules match an event, THE Routing_Engine SHALL apply all matching rules in priority order
6. THE Routing_Engine SHALL support regex pattern matching for resource names and descriptions
7. THE Routing_Engine SHALL support exclusion rules to prevent specific events from creating tickets

### Requirement 4: Comprehensive Monitoring and Observability

### Requirement 4: Comprehensive Monitoring and Observability

**User Story:** As a platform engineer, I want comprehensive monitoring and alerting for AWS Health Compass, so that I can ensure the system is operating correctly and troubleshoot issues quickly.

#### Acceptance Criteria

1. THE Health_Compass SHALL emit CloudWatch metrics for event processing rates and success/failure counts
2. THE Health_Compass SHALL emit CloudWatch metrics for integration response times and error rates
3. THE Health_Compass SHALL create CloudWatch alarms for critical system failures and performance degradation
4. THE Health_Compass SHALL provide structured logging with correlation IDs across all components
5. WHEN system errors occur, THE Health_Compass SHALL send notifications to designated monitoring channels
6. THE Health_Compass SHALL provide dashboards showing system health and processing statistics
7. THE Health_Compass SHALL track and report on ticket creation success rates per ITSM system

### Requirement 5: Enhanced Security and Compliance

**User Story:** As a security engineer, I want enhanced security features and compliance capabilities, so that AWS Health Compass meets enterprise security requirements.

#### Acceptance Criteria

1. THE Credential_Manager SHALL support credential rotation without service interruption
2. THE Health_Compass SHALL encrypt all data in transit using TLS 1.2 or higher
3. THE Health_Compass SHALL encrypt all data at rest using AWS KMS customer-managed keys
4. THE Health_Compass SHALL implement least-privilege IAM policies for all components
5. THE Health_Compass SHALL log all API calls and configuration changes for audit purposes
6. THE Health_Compass SHALL support VPC endpoints for all AWS service communications
7. WHEN sensitive data is processed, THE Health_Compass SHALL mask or redact it in logs

### Requirement 6: Cost Optimization and Resource Management

**User Story:** As a cloud architect, I want cost optimization features and better resource management, so that AWS Health Compass operates efficiently and cost-effectively.

#### Acceptance Criteria

1. THE Health_Compass SHALL implement intelligent batching to reduce Lambda invocation costs
2. THE Health_Compass SHALL use DynamoDB on-demand billing with automatic scaling
3. THE Health_Compass SHALL implement SQS message batching to reduce API calls
4. THE Health_Compass SHALL provide cost reporting and optimization recommendations
5. WHEN event volumes are low, THE Health_Compass SHALL automatically scale down resources
6. THE Health_Compass SHALL implement data lifecycle policies for Event_Store cleanup
7. THE Health_Compass SHALL support reserved capacity planning for predictable workloads

### Requirement 7: Multi-Region Deployment and Disaster Recovery

**User Story:** As a reliability engineer, I want multi-region deployment capabilities and disaster recovery features, so that AWS Health Compass remains available during regional outages.

#### Acceptance Criteria

1. THE Health_Compass SHALL support deployment across multiple AWS regions
2. THE Health_Compass SHALL replicate critical configuration data across regions
3. THE Health_Compass SHALL implement automatic failover between regions
4. WHEN a regional failure occurs, THE Health_Compass SHALL continue processing events in alternate regions
5. THE Health_Compass SHALL provide cross-region event deduplication
6. THE Health_Compass SHALL support active-active deployment patterns
7. THE Health_Compass SHALL maintain consistent routing rules across all deployed regions

### Requirement 8: Enhanced Error Handling and Recovery

**User Story:** As a DevOps engineer, I want robust error handling and recovery mechanisms, so that temporary failures don't result in lost events or duplicate tickets.

#### Acceptance Criteria

1. WHEN ITSM system integration fails, THE Health_Compass SHALL retry with exponential backoff
2. THE Health_Compass SHALL implement circuit breaker patterns for external system calls
3. THE Health_Compass SHALL provide manual retry capabilities for failed events
4. WHEN events cannot be processed after maximum retries, THE Health_Compass SHALL store them for manual review
5. THE Health_Compass SHALL detect and prevent duplicate ticket creation across system restarts
6. THE Health_Compass SHALL implement graceful degradation when downstream systems are unavailable
7. WHEN system recovery occurs, THE Health_Compass SHALL automatically resume processing queued events

### Requirement 9: Configuration Management and Deployment

**User Story:** As a platform engineer, I want improved configuration management and deployment capabilities, so that I can manage AWS Health Compass configurations efficiently across environments.

#### Acceptance Criteria

1. THE Health_Compass SHALL support environment-specific configuration management
2. THE Health_Compass SHALL validate configuration changes before deployment
3. THE Health_Compass SHALL support blue-green deployment patterns
4. THE Health_Compass SHALL provide configuration versioning and rollback capabilities
5. WHEN configuration changes are made, THE Health_Compass SHALL apply them without service interruption
6. THE Health_Compass SHALL support Infrastructure as Code deployment using CloudFormation or CDK
7. THE Health_Compass SHALL provide configuration drift detection and remediation

### Requirement 10: Enhanced Testing and Quality Assurance

**User Story:** As a software engineer, I want comprehensive testing capabilities, so that I can ensure AWS Health Compass changes don't break existing functionality.

#### Acceptance Criteria

1. THE Health_Compass SHALL provide unit test coverage for all Lambda functions
2. THE Health_Compass SHALL provide integration tests for all ITSM system integrations
3. THE Health_Compass SHALL support end-to-end testing with synthetic AWS Health events
4. THE Health_Compass SHALL provide performance testing capabilities for load validation
5. THE Health_Compass SHALL implement automated testing in CI/CD pipelines
6. THE Health_Compass SHALL provide test data generation for various event scenarios
7. WHEN tests fail, THE Health_Compass SHALL prevent deployment to production environments

### Requirement 11: Backward Compatibility and Migration

**User Story:** As a system administrator, I want seamless migration from the current system to the enhanced version, so that existing functionality continues to work without disruption.

#### Acceptance Criteria

1. THE Health_Compass SHALL maintain compatibility with existing Jira and ServiceNow integrations
2. THE Health_Compass SHALL preserve existing routing configurations during upgrades
3. THE Health_Compass SHALL migrate existing Event_Store data without data loss
4. THE Health_Compass SHALL support gradual rollout of new features
5. WHEN legacy configurations are detected, THE Health_Compass SHALL automatically migrate them to new formats
6. THE Health_Compass SHALL provide rollback capabilities to previous versions
7. THE Health_Compass SHALL maintain API compatibility for any external integrations