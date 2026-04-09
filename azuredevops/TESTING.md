# Deployment & Testing Guide — Azure DevOps Integration

This guide walks you through deploying and testing the AWS HealthCompass Azure DevOps integration in your environment.

## Prerequisites Checklist

Before you begin, confirm you have:

- [ ] AWS account with [AWS Health organizational view](https://docs.aws.amazon.com/health/latest/ug/enable-organizational-view.html) and [delegated account](https://docs.aws.amazon.com/health/latest/ug/delegated-administrator-organizational-view.html) enabled
- [ ] Azure DevOps organization with a project using Agile, Scrum, or CMMI process template
- [ ] Azure DevOps PAT with **Work Items: Read & Write** scope (`vso.work_write`)
- [ ] S3 bucket in the deployment account for Lambda packages
- [ ] AWS CLI configured with appropriate permissions
- [ ] (Tag model only) IAM role name for cross-account tag discovery

## Step 1: Prepare Lambda Deployment Packages

```bash
cd azuredevops/code

# Create zip packages
zip HealthEventProcessorLambda.zip HealthEventProcessorLambda.py
zip HealthEventADOIntegration.zip HealthEventADOIntegration.py
```

## Step 2: Upload to S3

```bash
aws s3 cp HealthEventProcessorLambda.zip s3://<your-s3-bucket>/
aws s3 cp HealthEventADOIntegration.zip s3://<your-s3-bucket>/
```

## Step 3: Deploy CloudFormation Stack

```bash
aws cloudformation create-stack \
  --stack-name aws-health-ado-integration \
  --template-body file://azuredevops/cloudformation/cloudformation.yaml \
  --capabilities CAPABILITY_IAM \
  --parameters \
    ParameterKey=DeployModel,ParameterValue=Account \
    ParameterKey=ADOOrganizationUrl,ParameterValue=https://dev.azure.com/<your-organization> \
    ParameterKey=ADOPat,ParameterValue=<your-pat> \
    ParameterKey=S3BucketName,ParameterValue=<your-s3-bucket> \
    ParameterKey=HealthEventProcessorLambdaKey,ParameterValue=HealthEventProcessorLambda.zip \
    ParameterKey=HealthEventADOIntegrationLambdaKey,ParameterValue=HealthEventADOIntegration.zip
```

**Optional parameters** — append as needed:
```
    ParameterKey=ADOAreaPath,ParameterValue=<your-area-path> \
    ParameterKey=ADOIterationPathPrefix,ParameterValue=<your-iteration-prefix> \
    ParameterKey=EnableAutoActivate,ParameterValue=true
```

**Tag model** — append these:
```
    ParameterKey=AssumeRoleName,ParameterValue=<role-name> \
    ParameterKey=TagKey,ParameterValue=<tag-key>
```

Monitor deployment:
```bash
aws cloudformation describe-stacks --stack-name aws-health-ado-integration --query 'Stacks[0].StackStatus'
```

## Step 4: Note Stack Outputs

```bash
aws cloudformation describe-stacks \
  --stack-name aws-health-ado-integration \
  --query 'Stacks[0].Outputs' \
  --output table
```

Note down:
- `DynamoDBTrackTable` — tracking table name
- `DynamoDBMappingTable` — mapping table name (for DynamoDB configuration)
- `CustomEventBusArn` — needed for EventBridge forwarding rules
- `HealthEventProcessorRoleArn` — needed for Tag model cross-account setup

## Step 5: Configure DynamoDB Mapping Table

Using the `DynamoDBMappingTable` name from the stack outputs, add your routing entries.

**Account model example:**
```bash
aws dynamodb put-item \
  --table-name <DynamoDBMappingTable> \
  --item '{
    "Account": {"S": "DefaultProjectCode"},
    "ACADOProjectName": {"S": "<your-ado-project>"},
    "ACADOAreaPath": {"S": "<your-ado-project>\\<your-area-path>"}
  }'
```

> **Note**: Always create a `DefaultProjectCode` entry. See [README-ado.md](README-ado.md#configure-dynamodb-mapping) for Service and Tag model examples.

## Step 6: Configure EventBridge Forwarding

Follow the instructions in [README-ado.md — Configure Health Event Aggregation](README-ado.md#configure-health-event-aggregation) to create EventBridge rules that forward AWS Health events from each region to the custom event bus.

## Testing

### Test 1: Validate Stack Resources

Verify all resources were created:
```bash
# Check Lambda functions
aws lambda get-function --function-name aws-health-ado-integration-health-event-processor
aws lambda get-function --function-name aws-health-ado-integration-health-event-ado-integration

# Check SQS queues
aws sqs get-queue-url --queue-name aws-health-ado-integration-HealthEventIngestionQueue
aws sqs get-queue-url --queue-name aws-health-ado-integration-health-event-queue

# Check DynamoDB tables
aws dynamodb describe-table --table-name <DynamoDBTrackTable>
aws dynamodb describe-table --table-name <DynamoDBMappingTable>

# Check Secrets Manager
aws secretsmanager describe-secret --secret-id <ADOSecretName>
```

### Test 2: Test ADO Connectivity

Verify your PAT works against your ADO instance:
```bash
curl -s -o /dev/null -w "%{http_code}" \
  -u :<your-pat> \
  "https://dev.azure.com/<your-organization>/_apis/projects?api-version=7.1"
```

Expected: `200`

### Test 3: End-to-End Test with Sample Event

Invoke the Processor Lambda with a sample health event. Create a file `test-event.json`:

```json
{
  "Records": [
    {
      "body": "{\"id\":\"test-event-001\",\"time\":\"2026-03-23T10:00:00Z\",\"region\":\"us-east-1\",\"detail\":{\"eventArn\":\"arn:aws:health:us-east-1::event/EC2/AWS_EC2_PLANNED_LIFECYCLE_EVENT/test001\",\"service\":\"EC2\",\"eventTypeCode\":\"AWS_EC2_PLANNED_LIFECYCLE_EVENT\",\"eventTypeCategory\":\"scheduledChange\",\"eventRegion\":\"us-east-1\",\"startTime\":\"2026-04-01T00:00:00Z\",\"endTime\":\"2026-04-02T00:00:00Z\",\"eventDescription\":[{\"latestDescription\":\"Test: Amazon EC2 has detected degradation of the underlying hardware hosting your EC2 instance.\"}],\"affectedAccount\":\"123456789012\",\"affectedEntities\":[{\"entityValue\":\"arn:aws:ec2:us-east-1:123456789012:instance/i-0123456789abcdef0\",\"status\":\"UPCOMING\",\"lastUpdatedTime\":\"2026-03-23T10:00:00Z\"}]}}"
    }
  ]
}
```

Invoke the Processor Lambda:
```bash
aws lambda invoke \
  --function-name aws-health-ado-integration-health-event-processor \
  --payload file://test-event.json \
  --cli-binary-format raw-in-base64-out \
  response.json

cat response.json
```

Expected: `statusCode: 200` with `untrackedResourcesCount: 1`

### Test 4: Verify ADO Work Items

After the test event processes through both Lambdas:

1. Check CloudWatch Logs for both Lambda functions:
   ```bash
   aws logs tail /aws/lambda/aws-health-ado-integration-health-event-processor --since 5m
   aws logs tail /aws/lambda/aws-health-ado-integration-health-event-ado-integration --since 5m
   ```

2. Verify in Azure DevOps:
   - A **Feature** work item should be created with the health event details
   - A **Child Task** should be linked to the Feature
   - Both should have the correct Area Path and Iteration Path (if configured)
   - The Child Task's Effort field should be empty

3. Check DynamoDB tracking table:
   ```bash
   aws dynamodb scan --table-name <DynamoDBTrackTable>
   ```
   You should see an entry with `adoWorkItemId` matching the Feature ID in ADO.

### Test 5: Test Update Flow

Run the same test event again (Test 3). This time:
- No new Feature should be created
- A **comment** should be added to the existing Feature
- CloudWatch Logs should show "Found existing Feature" message
- If `EnableAutoActivate` is `true`, the Feature status should change to "Active" and the Iteration Path should update to the current sprint

### Troubleshooting

If tests fail, check in this order:

1. **CloudWatch Logs** — Look for error messages in both Lambda log groups
2. **SQS DLQ** — Check if messages landed in the dead letter queues:
   ```bash
   aws sqs get-queue-attributes \
     --queue-url $(aws sqs get-queue-url --queue-name aws-health-ado-integration-health-event-dlq --query QueueUrl --output text) \
     --attribute-names ApproximateNumberOfMessages
   ```
3. **DynamoDB mapping** — Verify your mapping table has the correct entries for the identifier in your test event
4. **ADO PAT** — Verify the PAT hasn't expired and has the correct scope
5. **Area Path / Iteration Path** — Verify these exist in your ADO project

## Cleanup

To remove all resources:
```bash
aws cloudformation delete-stack --stack-name aws-health-ado-integration
```

> **Note**: The DynamoDB tables have deletion protection disabled by default. If you enabled it manually, you'll need to disable it before stack deletion.
