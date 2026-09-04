"""
AWS Health Compass - Health Event Processor Lambda for Azure DevOps Integration
Processes incoming AWS Health events, categorizes resources, and prepares messages
for work item creation or updates in Azure DevOps.
"""

import json
import logging
import os
from typing import Dict, Any, List, Tuple
from datetime import datetime
import boto3


# Setup Logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

deploy_model = os.environ.get('DEPLOY_MODEL')
tag_key = os.environ.get('TAG_KEY')
if not deploy_model:
    raise ValueError("DEPLOY_MODEL environment variable is not set")
if deploy_model == 'Tag' and not tag_key:
    raise ValueError("TAG_KEY environment variable is not set")

class ResourceProcessor:
    def __init__(self):
        """Initialize with configuration based on deployment model"""
        self.dynamodb = boto3.client('dynamodb')
        self.table_name = os.environ.get('ADO_DYNAMODB_TABLE')
        self.deploy_model = os.environ.get('DEPLOY_MODEL')
        self.sqs = boto3.client('sqs')
        self.queue_url = os.environ.get('SQS_QUEUE_URL')
        self.organizations = boto3.client('organizations')

        if not self.table_name:
            raise ValueError("ADO_DYNAMODB_TABLE environment variable is not set")
        if not self.deploy_model:
            raise ValueError("DEPLOY_MODEL environment variable is not set")
        if not self.queue_url:
            raise ValueError("SQS_QUEUE_URL environment variable is not set")

        # Model-specific configurations
        self.config = {
            'Account': {
                'primary_key': 'Account',
                'project_key': 'ACADOProjectName',
                'area_path_key': 'ACADOAreaPath',
                'index_name': 'ACkeyIndex'
            },
            'Service': {
                'primary_key': 'Service',
                'project_key': 'SADOProjectName',
                'area_path_key': 'SADOAreaPath',
                'index_name': 'SkeyIndex'
            },
            'Tag': {
                'primary_key': 'HostTag',
                'project_key': 'HTADOProjectName',
                'area_path_key': 'HTADOAreaPath',
                'index_name': 'HTkeyIndex'
            }
        }

        if self.deploy_model not in self.config:
            raise ValueError(f"Invalid deploy model: {self.deploy_model}")

        self.model_config = self.config[self.deploy_model]

    def send_to_queue(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send message to SQS queue"""
        try:
            response = self.sqs.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(payload)
            )
            logger.info(f"Message sent to queue: {response['MessageId']}")
            logger.info(f"{json.dumps(payload)}")
            return response
        except Exception as e:
            logger.error(f"Error sending message to queue: {str(e)}")
            raise

    def prepare_queue_message(self, event: Dict[str, Any],
                            tracked_resources: Dict[str, List[Dict]],
                            untracked_resources: Dict[str, List[Dict]]) -> Dict[str, Any]:
        """Prepare a single message containing both tracked and untracked resources"""
        event_details = event['detail']

        return {
            'timestamp': datetime.utcnow().isoformat(),
            'detail': {
                'eventTypeCode': event_details['eventTypeCode'],
                'eventDescription': event_details['eventDescription'][0]['latestDescription'],
                'startTime': event_details.get('startTime'),
                'endTime': event_details.get('endTime'),
                'eventArn': event_details['eventArn'],
                'service': event_details['service'],
                'eventRegion': event_details['eventRegion'],
                'account': event_details['affectedAccount'],
                'eventTypeCategory': event_details['eventTypeCategory'],
                'id': event['id'],
                'time': event['time'],
                'region': event['region']
            },
            'deployModel': self.deploy_model,
            'trackedResources': tracked_resources,
            'untrackedResources': untracked_resources
        }


def get_tags_using_resource_groups(resource_arn: str, region: str, target_account: str) -> str:
    """Get tags using Resource Groups Tagging API with cross-account support"""
    try:
        sts = boto3.client('sts')
        current_account = sts.get_caller_identity()['Account']
        logger.info(f"get_tags_using_resource_groups {resource_arn} in account {target_account}")

        if current_account == target_account:
            logger.info("get_tags_using_resource_groups same account processing")
            resourcetagging = boto3.client('resourcegroupstaggingapi', region_name=region)
        else:
            assume_role_name = os.environ.get('ASSUME_ROLE_NAME')
            if not assume_role_name:
                raise ValueError("ASSUME_ROLE_NAME environment variable not set")
            logger.info(f"get_tags_using_resource_groups cross-account processing {target_account}")
            role_arn = f"arn:aws:iam::{target_account}:role/{assume_role_name}"

            assumed_role = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName="AssumeRoleSession"
            )

            resourcetagging = boto3.client(
                'resourcegroupstaggingapi',
                region_name=region,
                aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
                aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
                aws_session_token=assumed_role['Credentials']['SessionToken']
            )
            logger.info(f"get_tags_using_resource_groups Successfully assumed role {assume_role_name}")

        response = resourcetagging.get_resources(
            ResourceARNList=[resource_arn]
        )

        if response['ResourceTagMappingList']:
            tags = response['ResourceTagMappingList'][0].get('Tags', [])
            for tag in tags:
                if tag['Key'] == tag_key:
                    return tag['Value']

        return 'HOST_TAG_NOT_AVAILABLE'

    except Exception as e:
        logger.error(f"Error getting {tag_key} tag using Resource Groups: {str(e)}")
        return 'HOST_TAG_NOT_AVAILABLE'


def needs_special_handling(service: str) -> bool:
    """Check if service needs special handling for tag retrieval"""
    special_handling_services = {
        's3': True,
        'iam': True,
        'route53': True,
        'cloudfront': True,
        'autoscaling': True
    }
    return special_handling_services.get(service, False)


def get_tags_special_handling(resource_arn: str, region: str, target_account: str, service_name: str) -> str:
    """Handle tag retrieval for services with special requirements"""
    try:
        sts = boto3.client('sts')
        current_account = sts.get_caller_identity()['Account']
        logger.info(f"Special handling for Service {service_name}: Processing resource {resource_arn} in account {target_account}")

        if current_account == target_account:
            logger.info(f"Special handling {service_name}: Processing using same account")
            tagapi = boto3.client(service_name)

            if service_name == 's3':
                bucket_name = resource_arn.split(':')[-1].split('/')[-1]
                logger.info(f"Special handling for Service {service_name}: Processing {bucket_name}")
                if '/' in resource_arn.split(':')[-1]:
                    logger.warning("S3 object tagging not supported")
                    return 'HOST_TAG_NOT_AVAILABLE'
                response = tagapi.get_bucket_tagging(Bucket=bucket_name)
            elif service_name == 'autoscaling':
                asg_name = resource_arn.split(':')[-1].split('/')[-1]
                logger.info(f"Extracted ASG name: {asg_name}")
                response = tagapi.describe_tags(
                    Filters=[{'Name': 'auto-scaling-group', 'Values': [asg_name]}]
                )
            else:
                logger.warning(f"Special handling for {service_name} not implemented")
                return 'HOST_TAG_NOT_AVAILABLE'
        else:
            assume_role_name = os.environ.get('ASSUME_ROLE_NAME')
            if not assume_role_name:
                logger.error("ASSUME_ROLE_NAME environment variable not set")
                return 'HOST_TAG_NOT_AVAILABLE'
            logger.info(f"Special handling: Using cross-account access to target {target_account} from {current_account}")
            role_arn = f"arn:aws:iam::{target_account}:role/{assume_role_name}"

            assumed_role = sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName="AssumeRoleSession"
            )
            session = boto3.client(
                    service_name,
                    region_name=region,
                    aws_access_key_id=assumed_role['Credentials']['AccessKeyId'],
                    aws_secret_access_key=assumed_role['Credentials']['SecretAccessKey'],
                    aws_session_token=assumed_role['Credentials']['SessionToken']
                )
            logger.info(f"Successfully assumed role {assume_role_name}")
            if service_name == 's3':
                bucket_name = resource_arn.split(':')[-1].split('/')[-1]
                logger.info(f"Special handling for Service {service_name}: Processing {bucket_name}")
                if '/' in resource_arn.split(':')[-1]:
                    logger.warning("S3 object tagging not supported")
                    return 'HOST_TAG_NOT_AVAILABLE'
                response = session.get_bucket_tagging(Bucket=bucket_name)
            elif service_name == 'autoscaling':
                asg_name = resource_arn.split(':')[-1].split('/')[-1]
                logger.info(f"Extracted ASG name: {asg_name}")
                response = session.describe_tags(
                    Filters=[{'Name': 'auto-scaling-group', 'Values': [asg_name]}]
                )
            else:
                logger.warning(f"Special handling for {service_name} not implemented")
                return 'HOST_TAG_NOT_AVAILABLE'

        if service_name in ['s3', 'cloudfront']:
            for tag in response.get('TagSet', []):
                if tag['Key'] == tag_key:
                    logger.info(f"Found {tag_key} tag with value: {tag['Value']}")
                    return tag['Value']
        else:
            for tag in response.get('Tags', []):
                logger.info(f"Special handling for {service_name}: Found tag: {tag}")
                if tag['Key'] == tag_key:
                    logger.info(f"Found Host tag with value: {tag['Value']}")
                    return tag['Value']
        return 'HOST_TAG_NOT_AVAILABLE'
    except Exception as e:
        logger.error(f"Error in special handling for {service_name}: {str(e)}")
        return 'HOST_TAG_NOT_AVAILABLE'


def get_entity_details(resource_arn: str, event: Dict[str, Any]) -> Dict[str, Any]:
    """Get entity details from event for a specific resource ARN"""
    affected_entities = event.get('detail', {}).get('affectedEntities', [])
    logger.info("get_entity_details: Looking up affected entities")
    for entity in affected_entities:
        if entity.get('entityValue') == resource_arn:
            return {
                'entity_value': entity.get('entityValue'),
                'status': entity.get('status'),
                'last_updated_time': entity.get('lastUpdatedTime')
            }
    logger.warning(f"No entity details found for resource ARN: {resource_arn}")
    return {}


def get_host_tag_for_resource(resource_arn: str, affected_account: str, region: str) -> str:
    """Get tag for a resource with fallback for unsupported services"""
    try:
        arn_parts = resource_arn.split(':')
        service_name = arn_parts[2].lower()
        arn_region = arn_parts[3]
        region = arn_region if arn_region else region
        arn_account = arn_parts[4]
        target_account = arn_account if arn_account else affected_account
        logger.info(f"get_host_tag_for_resource {resource_arn}, Service {service_name}, region {region} target account {target_account}")

        if needs_special_handling(service_name):
            return get_tags_special_handling(resource_arn, region, target_account, service_name)

        return get_tags_using_resource_groups(resource_arn, region, target_account)

    except Exception as e:
        logger.error(f"Error getting {tag_key} tag: {str(e)}")
        return 'HOST_TAG_NOT_AVAILABLE'


def get_resources_by_identifier(untracked_resources: List[Dict[str, Any]], event: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Groups resources by their identifier based on deployment model"""
    resources_by_identifier = {}

    try:
        service = event['detail']['service']
        region = event['detail']['eventRegion']
        affected_account = event['detail'].get('affectedAccount')
        main_account = event.get('account')

        logger.info(f"get_resources_by_identifier: Deployment model {deploy_model} service {service} region {region} affected_account {affected_account} main_account {main_account}")

        for resource_item in untracked_resources:
            try:
                resource_arn = resource_item.get('arn')
                if not resource_arn:
                    logger.warning(f"Resource item missing resource_arn field: {resource_item}")
                    continue

                if deploy_model == 'Account':
                    identifier = affected_account or main_account
                elif deploy_model == 'Service':
                    identifier = event['detail']['service']
                elif deploy_model == 'Tag':
                    identifier = get_host_tag_for_resource(resource_arn, affected_account, region)

                if identifier not in resources_by_identifier:
                    resources_by_identifier[identifier] = []

                resources_by_identifier[identifier].append({
                    'arn': resource_arn,
                    'status': resource_item.get('status'),
                    'last_updated_time': resource_item.get('last_updated_time')
                })

            except Exception as e:
                logger.error(f"Error processing resource {resource_item}: {e}")
                if 'HOST_TAG_NOT_AVAILABLE' not in resources_by_identifier:
                    resources_by_identifier['HOST_TAG_NOT_AVAILABLE'] = []
                resources_by_identifier['HOST_TAG_NOT_AVAILABLE'].append({
                    'arn': resource_item.get('resource_arn', 'unknown'),
                    'status': resource_item.get('status', 'unknown'),
                    'last_updated_time': resource_item.get('last_updated_time', 'unknown'),
                    'host_tag': 'HOST_TAG_NOT_AVAILABLE'
                })
        logger.info(f"resources_by_identifier: {resources_by_identifier}")
        return resources_by_identifier

    except KeyError as e:
        logger.error(f"Missing required field in event: {e}")
        raise ValueError(f"Invalid event structure: missing {e}") from e
    except Exception as e:
        logger.error(f"Error processing event: {e}")
        raise


def check_existing_event(event_arn: str, resource_arn: str) -> list:
    """Check if event ARN exists in DynamoDB and return ticket details"""
    try:
        logger.info(f"check_existing_event: Checking tracking for event_arn: {event_arn} and resource_arn: {resource_arn}")
        dynamodb = boto3.resource('dynamodb')
        table_name = os.environ.get('DYNAMODB_TRACK_TABLE')
        table = dynamodb.Table(table_name)

        # Direct lookup using primary key
        try:
            response = table.get_item(
                Key={
                    'resourceArn': resource_arn,
                    'eventArn': event_arn
                }
            )
            if 'Item' in response:
                logger.info(f"check_existing_event: Found existing tracking via direct lookup: {response['Item']}")
                return [response['Item']]
        except Exception as e:
            logger.warning(f"Direct lookup failed: {str(e)}")

        # Fallback to GSI
        response = table.query(
            IndexName='TETkeyIndex',
            KeyConditionExpression='eventArn = :event_arn',
            FilterExpression='resourceArn = :resource_arn',
            ExpressionAttributeValues={
                ':event_arn': event_arn,
                ':resource_arn': resource_arn
            }
        )

        items = response.get('Items', [])
        if items:
            logger.info(f"check_existing_event: Found existing tracking via GSI: {items}")
        else:
            logger.info("check_existing_event: No existing tracking found via GSI, trying scan")
            scan_response = table.scan(
                FilterExpression='eventArn = :event_arn AND resourceArn = :resource_arn',
                ExpressionAttributeValues={
                    ':event_arn': event_arn,
                    ':resource_arn': resource_arn
                }
            )
            scan_items = scan_response.get('Items', [])
            if scan_items:
                logger.info(f"check_existing_event: Found existing tracking via scan: {scan_items}")
                return scan_items
            else:
                logger.info("check_existing_event: No existing tracking found via any method")

        return items

    except Exception as e:
        logger.error(f"Error checking existing event: {str(e)}")
        return []


def generate_resource_arn(service, region, resource_id, affected_account):
    """Generate AWS resource ARN based on service type and resource ID"""
    logger.info(f"generate_resource_arn: Generating ARN for service: {service}, resource_id: {resource_id}")

    if 'arn:' in resource_id:
        return resource_id

    try:
        if service == 'EC2':
            return f"arn:aws:ec2:{region}:{affected_account}:instance/{resource_id}"
        if service == 'S3':
            return f"arn:aws:s3:::{resource_id}"
        if service == 'EBS':
            if ':' in resource_id:
                resource_id = resource_id.split(':')[-1]
            return f"arn:aws:ec2:{region}:{affected_account}:volume/{resource_id}"

        logger.warning(f"Unsupported service type: {service}")
        return resource_id

    except Exception as e:
        logger.error(f"Error generating ARN: {str(e)}")
        raise


def group_resources_by_tracking_status(event: Dict[str, Any]) -> Tuple[List[Dict[str, str]], List[str]]:
    """Groups resources into tracked and untracked based on DynamoDB records"""
    try:
        logger.info("group_resources_by_tracking_status: Checking for event tracking and creating list of existing vs new")

        detail = event.get('detail', {})
        event_arn = detail.get('eventArn')
        service = detail.get('service')
        region = detail.get('eventRegion')
        affected_account = detail.get('affectedAccount')

        tracked_resources: List[Dict[str, str]] = []
        untracked_resources: List[str] = []

        resource_list = []
        for entity in event.get('detail', {}).get('affectedEntities', []):
            if entity.get('entityValue'):
                resource_list.append(entity.get('entityValue'))

        logger.info(f"Processing {len(resource_list)} resources from event {event_arn}")

        for resource_id in resource_list:
            resource_arn = generate_resource_arn(
                service=service,
                region=region,
                resource_id=resource_id,
                affected_account=affected_account
            )

            entity_info = get_entity_details(resource_arn, event)

            existing_tracking = check_existing_event(
                event_arn=event_arn,
                resource_arn=resource_arn
            )

            if existing_tracking:
                logger.info(f"Resource {resource_arn} is already tracked with ticket info: {existing_tracking}")
                resource_info = {
                    'arn': entity_info.get('entity_value'),
                    'status': entity_info.get('status'),
                    'last_updated_time': entity_info.get('last_updated_time')
                }
                for tracking_item in existing_tracking:
                    if 'adoWorkItemId' in tracking_item:
                        resource_info['ticket_id'] = int(tracking_item['adoWorkItemId'])
                tracked_resources.append(resource_info)
            else:
                logger.info(f"Resource {resource_arn} is not tracked yet")
                untracked_resources.append({
                    'arn': entity_info.get('entity_value'),
                    'status': entity_info.get('status'),
                    'last_updated_time': entity_info.get('last_updated_time')
                })

        logger.info(f"Found {len(tracked_resources)} tracked and {len(untracked_resources)} untracked resources")
        return tracked_resources, untracked_resources

    except Exception as e:
        logger.error(f"Error grouping resources: {str(e)}")
        raise


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """Main Lambda handler"""
    try:
        logger.info(f"Received SQS event:{json.dumps(event)}")
        event_body = json.loads(event['Records'][0]['body'])

        untracked_resources_by_identifier = {}

        tracked_resources, untracked_resources = group_resources_by_tracking_status(event_body)

        logger.info(f"main:TrackedResources:{tracked_resources}")
        logger.info(f"main:UntrackedResources:{untracked_resources}")

        if untracked_resources:
            logger.info(f"main:Processing {len(untracked_resources)} untracked resources")
            untracked_resources_by_identifier = get_resources_by_identifier(untracked_resources, event_body)

        logger.info(f"main:UntrackedResourcesByIdentifier:{untracked_resources_by_identifier}")
        processor = ResourceProcessor()
        message = processor.prepare_queue_message(
            event_body,
            tracked_resources,
            untracked_resources_by_identifier
        )

        response = processor.send_to_queue(message)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Successfully processed event and sent to queue',
                'messageId': response['MessageId'],
                'trackedResourcesCount': len(tracked_resources),
                'untrackedResourcesCount': len(untracked_resources)
            })
        }

    except Exception as e:
        logger.error(f"Error processing event: {str(e)}")
        raise
