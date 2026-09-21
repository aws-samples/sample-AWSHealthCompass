# AWS HealthCompass - ServiceNow Integration

**AWS HealthCompass ServiceNow Integration** is a serverless solution that converts AWS Health planned lifecycle events into actionable change requests in ServiceNow. This solution significantly reduces operational overhead by automating the creation and management of ServiceNow change requests for AWS Health planned lifecycle events, ensuring resource owners are notified about relevant changes through configurable routing capabilities.

## Key Features

The ServiceNow integration offers several powerful capabilities that streamline operations and enhance change management:

1. **Automated Change Request Management**: Automatically creates ServiceNow change requests for AWS Health planned lifecycle events, eliminating manual monitoring and ticket creation while ensuring consistent documentation of infrastructure changes.

2. **Event-Driven Architecture**: Leverages a serverless, event-driven design that processes events in near real-time with minimal operational overhead, automatically scaling to handle event volume fluctuations.

3. **Flexible Routing Models**: Allows administrators to define custom mappings between AWS identifiers and ServiceNow change requests. Supports three deployment models:
   - **Account-based routing**: Directs change requests based on affected AWS accounts
   - **Service-based routing**: Organizes change requests by AWS service type (EC2, S3, etc.)
   - **Tag-based routing**: Directs change requests based on resource tags, enabling team-specific notifications

4. **Intelligent Change Request Updates**: Updates existing change requests when new resources are affected by the same AWS Health event, preventing duplicate tickets and providing consolidated tracking.

5. **Cross-Organization Visibility**: Aggregates health events across all accounts, providing comprehensive visibility through a single deployment.

6. **Resilient Message Processing**: Implements dead letter queues with configurable retry policies to handle processing failures, ensuring no events are lost.

7. **Secure Credential Management**: Uses AWS Secrets Manager to securely store and manage ServiceNow authentication credentials.

## Architecture

The solution consists of the following components:

1. **AWS Health Events**: Supports single account or event aggregation across Organization using AWS Health's organizational view with delegated account feature.

2. **AWS EventBridge**: Combines AWS default EventBridge and a custom EventBridge bus to efficiently aggregate and route AWS Health planned lifecycle events across an organization.

3. **AWS Lambda Functions**:
   - **HealthEventProcessorLambda**: Processes incoming AWS Health events, categorizes resources, and prepares messages for change request creation or updates
   - **HealthEventSnowIntegration**: Creates and updates ServiceNow change requests based on processed AWS Health events

4. **SQS Queues**: Provides buffering and resilience between Lambda components with Dead Letter Queue (DLQ) implementation for failed message processing.

5. **AWS Secrets Manager**: Securely stores ServiceNow credentials (username and password).

6. **DynamoDB Table**: Maintains the relationship between AWS Health events, affected resources, and their corresponding ServiceNow change requests.

7. **IAM Roles and Policies**: Provides appropriate permissions for Lambda execution and cross-account access (Tag model only).

## Prerequisites

1. **AWS Health Organizational View**: Enable [AWS Health organizational view](https://docs.aws.amazon.com/health/latest/ug/enable-organizational-view.html) and [AWS Health delegated account](https://docs.aws.amazon.com/health/latest/ug/delegated-administrator-organizational-view.html) to aggregate events across your organization.

2. **Deployment Account**: Deploy the solution in the AWS Health delegated account (referenced as `deployment-account`).

3. **ServiceNow Instance**: 
   - ServiceNow instance URL (e.g., `https://your-instance.service-now.com`)
   - ServiceNow username with appropriate permissions to create and update change requests
   - ServiceNow password

4. **S3 Bucket**: S3 bucket for Lambda deployment packages in the deployment account.

5. **Cross-Account Role** (Tag deployment model only):
   - IAM Role name to be used for cross-account access to linked accounts
   - Tag key to monitor for routing events

## Deployment Instructions

### 1. Prepare Lambda Packages

```bash
# Create deployment packages
zip -r HealthEventProcessorLambda.zip HealthEventProcessorLambda.py
zip -r HealthEventSnowIntegration.zip HealthEventSnowIntegration.py
```

### 2. Upload to S3

1. Login to your `deployment-account`
2. Switch to your preferred deployment region
3. Upload the Lambda zip files to your S3 bucket
4. Ensure CloudFormation has access to this bucket

### 3. Deploy CloudFormation Template

1. Open AWS CloudFormation console in your deployment account
2. Select "Create Stack" → "With new resources (standard)"
3. Choose "Upload a template file" → Select `cloudformation-snow.yaml`
4. Click "Next"
5. Provide the following parameters:

#### Required Parameters

| Parameter | Description | Example |
|-----------|-------------|---------|
| **Stack name** | Name for your CloudFormation stack | `aws-health-servicenow-integration` |
| **DeployModel** | Deployment model (Account/Service/Tag) | `Account` |
| **ServiceNowUrl** | ServiceNow instance URL | `https://your-instance.service-now.com` |
| **ServiceNowUsername** | ServiceNow username | `integration.user` |
| **ServiceNowPassword** | ServiceNow password | `your-password` |
| **S3BucketName** | S3 bucket containing Lambda packages | `my-lambda-deployment-bucket` |
| **HealthEventProcessorLambdaKey** | S3 key for processor Lambda | `HealthEventProcessorLambda.zip` |
| **HealthEventSnowIntegrationLambdaKey** | S3 key for ServiceNow Lambda | `HealthEventSnowIntegration.zip` |

#### Conditional Parameters (Tag Model Only)

| Parameter | Description | Example |
|-----------|-------------|---------|
| **AssumeRoleName** | IAM role name for cross-account access | `HealthEventTagRole` |
| **TagKey** | Tag key to monitor for routing | `Environment` |

6. Click "Next" → Configure stack options → Click "Next"
7. Review configuration and select "I acknowledge that AWS CloudFormation might create IAM resources"
8. Click "Create stack"

### 4. Monitor Deployment

Monitor stack creation progress in the CloudFormation console. Once complete, note the outputs for resource details.

## Configuration

### Configure Health Event Aggregation

Create rules to send AWS Health events from default EventBridge to the custom EventBridge bus:

1. **Locate Custom Event Bus**:
   - In your deployment account and region, find the custom EventBridge bus from CloudFormation outputs
   - Note the custom Event bus ARN from EventBridge console

2. **Create EventBridge Rules** (repeat for each region):
   - Go to EventBridge console → Event buses → Select default Event bus → Create rule
   - **Name**: `health-event-forwarding-rule`
   - **Rule type**: Rule with an event pattern
   - **Event source**: AWS events or EventBridge partner events
   - **Event pattern**: Use pattern form
     - **Event source**: AWS service
     - **AWS Service**: Health
     - **Event type**: All events
   - **Target**: EventBridge event bus
     - Select your custom event bus from dropdown
     - **Execution role**: Create a new role for this specific resource

3. **Repeat for All Regions**: Create similar rules in all AWS regions to forward events to your deployment region.

### ServiceNow Change Request Configuration

The solution automatically creates ServiceNow change requests with the following structure:

#### Change Request Fields

| Field | Description | Example |
|-------|-------------|---------|
| **Short Description** | Summary based on deployment model | `Account: 123456789012 - AWS EC2 Planned Maintenance - AWS_EC2_PLANNED_LIFECYCLE_EVENT` |
| **Description** | AWS Health event description | `Event Description: Scheduled maintenance for EC2 instances...` |
| **Work Notes** | Detailed resource information | Resource ARN, Status, Last Updated time |
| **Type** | Change request type | `Normal` |
| **Category** | Change category | `AWS` |

#### Work Notes Format

**For New Change Requests:**
```
Affected Resources:

Resource: arn:aws:ec2:us-east-1:123456789012:instance/i-1234567890abcdef0
Status: OPEN
Last Updated: 2023-10-15T10:30:00Z

Resource: arn:aws:ec2:us-east-1:123456789012:instance/i-0987654321fedcba0
Status: OPEN
Last Updated: 2023-10-15T10:30:00Z
```

**For Change Request Updates:**
```
Update for resources:

Resource: arn:aws:ec2:us-east-1:123456789012:instance/i-abcdef1234567890
Status: CLOSED
Last Updated: 2023-10-15T14:30:00Z
```

## Deployment Models

### Account Model

Routes AWS Health events based on affected AWS account ID.

**Use Case**: Organizations with dedicated teams responsible for specific AWS accounts.

**Configuration**: No additional setup required beyond basic deployment.

### Service Model

Routes events based on affected AWS service (EC2, S3, RDS, etc.).

**Use Case**: Organizations with specialized teams focused on specific AWS services.

**Configuration**: No additional setup required beyond basic deployment.

### Tag Model

Routes events based on resource tags, enabling fine-grained control over change request assignment.

**Use Case**: Organizations using resource tagging for ownership and team attribution.

**Additional Requirements**:
1. Create IAM roles in all linked accounts
2. Configure tag key monitoring
3. Set up cross-account trust relationships

#### Cross-Account IAM Role Setup (Tag Model)

Create the following role in every linked account:

**Role Permission Policy:**
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "tag:GetResources",
                "tag:GetTagKeys",
                "tag:GetTagValues"
            ],
            "Resource": "*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetBucketTagging",
                "iam:ListRoleTags",
                "iam:ListUserTags",
                "route53:ListTagsForResource",
                "autoscaling:DescribeTags"
            ],
            "Resource": "*"
        }
    ]
}
```

**Role Trust Policy:**
```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::<deployment-account-id>:role/<stack-name>-HealthEventProcessorRole-<random-suffix>"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
```

> **Note**: Replace `<deployment-account-id>` and `<stack-name>` with your actual values. The processor role name can be found in CloudFormation outputs.

## Testing the Solution

### 1. Verify Deployment

Check CloudFormation stack outputs for:
- DynamoDB table name
- Lambda function ARNs
- SQS queue URLs
- ServiceNow secret name

### 2. Test ServiceNow Connectivity

1. **Manual Test**: Use the ServiceNow Integration Lambda function test console
2. **Check Logs**: Monitor CloudWatch Logs for both Lambda functions
3. **Verify Credentials**: Ensure ServiceNow credentials in Secrets Manager are correct

### 3. Simulate Health Events

You can test with sample events from the `test/` directory:
- `lambda_testevent1.json` - Initial event
- `lambda_testevent1_update1.json` - Update event

### 4. Monitor Processing

1. **SQS Queues**: Check for messages in processing queues
2. **DLQ**: Monitor dead letter queues for failed messages
3. **CloudWatch Logs**: Review Lambda execution logs
4. **ServiceNow**: Verify change requests are created/updated

## Troubleshooting

### Common Issues

#### 1. Lambda Function Errors

**Symptoms**: Functions failing with errors in CloudWatch Logs

**Solutions**:
- Verify IAM permissions are correctly configured
- Check ServiceNow credentials in Secrets Manager
- Ensure ServiceNow URL is accessible from Lambda
- Validate ServiceNow user has change request creation permissions

#### 2. ServiceNow Authentication Failures

**Symptoms**: HTTP 401/403 errors in logs

**Solutions**:
- Verify ServiceNow username and password in Secrets Manager
- Check ServiceNow user account is active and not locked
- Ensure user has appropriate roles (e.g., `change_manager`, `itil`)
- Test credentials manually via ServiceNow REST API

#### 3. Missing Change Requests

**Symptoms**: Events processed but no ServiceNow tickets created

**Solutions**:
- Check SQS queues for stuck messages
- Verify EventBridge rules are properly configured
- Review DynamoDB tracking table for event records
- Check ServiceNow instance for created change requests

#### 4. Cross-Account Tag Discovery Issues (Tag Model)

**Symptoms**: Resources not found or permission errors

**Solutions**:
- Verify IAM roles are correctly set up in all accounts
- Check trust relationships between accounts
- Ensure tag permissions are properly configured
- Test role assumption manually using AWS CLI

### Debugging Steps

1. **Check CloudWatch Logs**:
   ```bash
   aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/your-stack-name"
   ```

2. **Monitor SQS Queues**:
   ```bash
   aws sqs get-queue-attributes --queue-url <queue-url> --attribute-names All
   ```

3. **Verify DynamoDB Records**:
   ```bash
   aws dynamodb scan --table-name <tracking-table-name>
   ```

4. **Test ServiceNow API**:
   ```bash
   curl -u username:password -H "Content-Type: application/json" \
        -X GET "https://your-instance.service-now.com/api/now/table/change_request?sysparm_limit=1"
   ```

### Log Analysis

#### Successful Processing Logs
```
INFO - Processing event: arn:aws:health:global::event/... with deploy model: Account
INFO - No existing change found for event ..., creating new change
INFO - Successfully created ServiceNow change CHG0000123 with sys_id abc123...
```

#### Error Patterns
```
ERROR - Failed to create change. Response code: 401
ERROR - Error querying tracking table: ...
ERROR - Could not extract resource ARN from resource: ...
```

## Security Considerations

### Credential Management
- ServiceNow credentials are stored securely in AWS Secrets Manager
- Credentials are encrypted at rest and in transit
- Consider implementing credential rotation policies

### Network Security
- Lambda functions operate within AWS VPC (optional)
- Consider implementing VPC endpoints for enhanced security
- ServiceNow communication occurs over HTTPS

### IAM Security
- IAM roles follow least privilege principle
- Cross-account roles have minimal required permissions
- Regular review of IAM policies recommended

### Monitoring and Auditing
- All API calls are logged in CloudTrail
- Lambda execution logs in CloudWatch
- ServiceNow change request audit trail

## Performance and Scaling

### Capacity Planning
- Lambda functions automatically scale based on event volume
- SQS queues provide buffering for high-volume scenarios
- DynamoDB uses on-demand capacity mode

### Monitoring Metrics
- Lambda invocation count and duration
- SQS queue depth and message age
- DynamoDB read/write capacity utilization
- ServiceNow API response times

## Cost Optimization

### Resource Usage
- Lambda functions charged per invocation and duration
- DynamoDB charged per read/write operations
- SQS charged per message
- Secrets Manager charged per secret per month

### Cost Monitoring
- Use AWS Cost Explorer to track solution costs
- Set up billing alerts for unexpected usage
- Monitor CloudWatch metrics for optimization opportunities

## Support and Maintenance

### Regular Maintenance Tasks
1. **Monitor Dead Letter Queues**: Review and reprocess failed messages
2. **Update ServiceNow Credentials**: Rotate passwords as per security policy
3. **Review CloudWatch Logs**: Check for errors and performance issues
4. **Update Lambda Functions**: Apply security patches and feature updates

### Backup and Recovery
- DynamoDB point-in-time recovery enabled
- Lambda function code stored in S3
- CloudFormation template provides infrastructure as code
- ServiceNow change requests provide audit trail

## Contributing

Contributions to improve the ServiceNow integration are welcome. Please follow standard pull request procedures and ensure:

1. Code follows existing patterns and conventions
2. Changes are tested with sample events
3. Documentation is updated accordingly
4. Security considerations are addressed

## License

This solution is provided as-is for educational and operational purposes. Please review and comply with your organization's security and compliance requirements before deployment.
