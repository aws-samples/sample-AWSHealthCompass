"""
AWS Health Compass - Azure DevOps Integration Lambda
Creates and updates Azure DevOps work items (Feature + Child Task) based on
processed AWS Health events.
"""

import json
import os
import logging
import sys
import base64
import urllib3
import boto3
from datetime import datetime
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger('ado_lambda')
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

http = urllib3.PoolManager()

ado_org_url = os.environ.get('ADO_ORG_URL')
if not ado_org_url:
    raise ValueError("ADO_ORG_URL environment variable is not set")
# Strip trailing slash
ado_org_url = ado_org_url.rstrip('/')

ado_area_path = os.environ.get('ADO_AREA_PATH', '')
ado_iteration_prefix = os.environ.get('ADO_ITERATION_PATH_PREFIX', '')
enable_auto_activate = os.environ.get('ENABLE_AUTO_ACTIVATE', 'false').lower() == 'true'

# Parse optional custom fields for Feature work items
ado_custom_fields = []
_custom_fields_raw = os.environ.get('ADO_CUSTOM_FIELDS', '')
if _custom_fields_raw:
    try:
        ado_custom_fields = json.loads(_custom_fields_raw)
        logger.info(f"Loaded {len(ado_custom_fields)} custom fields")
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse ADO_CUSTOM_FIELDS: {e}")

# Setup boto3 session
session = boto3.session.Session()

# Setup DynamoDB tracking table
track_table_name = os.environ.get('DYNAMODB_TRACK_TABLE')
if not track_table_name:
    raise ValueError("DYNAMODB_TRACK_TABLE environment variable is not set")

dynamodb = boto3.resource('dynamodb')
track_table = dynamodb.Table(track_table_name)
logger.info(f"DynamoDB tracking table status: {track_table.table_status}")


def get_secret():
    """Retrieve ADO PAT from Secrets Manager"""
    secret_name = os.environ.get('ADO_SECRET_NAME')
    if not secret_name:
        raise ValueError("ADO_SECRET_NAME environment variable is not set")

    region_name = os.environ.get('AWS_REGION', 'us-east-1')
    client = session.client(service_name='secretsmanager', region_name=region_name)

    try:
        response = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        raise e

    secret = json.loads(response['SecretString'])
    return secret


def get_ado_headers(pat):
    """Build HTTP headers for ADO REST API using PAT authentication"""
    credentials = base64.b64encode(f":{pat}".encode()).decode()
    return {
        'Content-Type': 'application/json-patch+json',
        'Authorization': f'Basic {credentials}'
    }


def get_ado_headers_json(pat):
    """Build HTTP headers for ADO REST API with standard JSON content type (for comments)"""
    credentials = base64.b64encode(f":{pat}".encode()).decode()
    return {
        'Content-Type': 'application/json',
        'Authorization': f'Basic {credentials}'
    }


def get_iteration_path():
    """Build iteration path from prefix + bi-weekly sprint naming.
    Format: <prefix>\\Sprint N Mon FY YY-YY
    where N=1 (day 1-15) or N=2 (day 16+), Mon=abbreviated month,
    FY=financial year starting April (e.g., FY 26-27).
    """
    if not ado_iteration_prefix:
        return None
    now = datetime.now()
    sprint = 1 if now.day <= 15 else 2
    month_abbr = now.strftime('%b')
    fy_start = now.year if now.month >= 4 else now.year - 1
    fy_end = fy_start + 1
    return f"{ado_iteration_prefix}\\Sprint {sprint} {month_abbr} FY {fy_start % 100}-{fy_end % 100}"


def check_tracking_table(event_arn, resource_arn):
    """Check if a resource is already being tracked for a specific event"""
    try:
        response = track_table.get_item(
            Key={
                'resourceArn': resource_arn,
                'eventArn': event_arn
            }
        )
        if 'Item' in response:
            logger.info(f"Found tracking for resource {resource_arn} in event {event_arn}")
            return response['Item']
        logger.info(f"No tracking found for resource {resource_arn} in event {event_arn}")
        return None
    except ClientError as e:
        logger.error(f"Error querying tracking table: {e.response['Error']['Message']}")
        return None


def find_existing_workitem_for_event(event_arn, identifier):
    """Find an existing ADO work item ID for an event and identifier"""
    try:
        response = track_table.query(
            IndexName='TETkeyIndex',
            KeyConditionExpression='eventArn = :event_arn',
            ExpressionAttributeValues={
                ':event_arn': event_arn
            }
        )

        for item in response.get('Items', []):
            if 'adoWorkItemId' in item:
                work_item_id = int(item['adoWorkItemId'])
                logger.info(f"Found existing work item {work_item_id} for event {event_arn}")
                return work_item_id, item.get('adoProject', '')

        logger.info(f"No existing work item found for event {event_arn}")
        return None, None

    except ClientError as e:
        logger.error(f"Error querying tracking table for existing work item: {e.response['Error']['Message']}")
        return None, None


def store_event_tracking(event_arn, start_time, work_item_id, resource_arn, project=None):
    """Store event tracking information in DynamoDB"""
    from dateutil.relativedelta import relativedelta

    try:
        if isinstance(start_time, str):
            try:
                start_time_format = datetime.strptime(start_time, "%a, %d %b %Y %H:%M:%S %Z")
            except ValueError:
                try:
                    start_time_format = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ")
                except ValueError:
                    start_time_format = datetime.now()
        else:
            start_time_format = datetime.now()

        expiration_time = int((start_time_format + relativedelta(years=2)).timestamp())

        item = {
            'eventArn': event_arn,
            'resourceArn': resource_arn,
            'adoWorkItemId': int(work_item_id),
            'expirationTime': expiration_time
        }

        if project:
            item['adoProject'] = project

        track_table.put_item(Item=item)
        logger.info(f"Successfully stored event tracking for resource: {resource_arn} with work item ID: {work_item_id}")
        return True

    except Exception as e:
        logger.error(f"Error storing event tracking: {str(e)}")
        return False


def get_resource_arn(resource):
    """Extract resource ARN from the resource object"""
    if isinstance(resource.get('arn'), dict) and 'resource_arn' in resource['arn']:
        return resource['arn']['resource_arn']
    return resource.get('arn')


def build_feature_payload(event_body, identifier, resources, area_path):
    """Build JSON Patch document for creating a Feature work item"""
    eventTypeCode = event_body['detail']['eventTypeCode']
    service = event_body['detail']['service']
    deployModel = event_body['deployModel']

    # Build title based on deploy model
    if deployModel == 'Account':
        title = f"Account: {identifier} - AWS {service} Planned Maintenance - {eventTypeCode}"
    elif deployModel == 'Tag':
        title = f"Tag: {identifier} - AWS {service} Planned Maintenance - {eventTypeCode} - Tag Based"
    elif deployModel == 'Service':
        title = f"Service: {identifier} - AWS {service} Planned Maintenance - {eventTypeCode} - Service Based"
    else:
        title = f"AWS Planned Maintenance - {eventTypeCode}"

    # Build HTML description
    event_description = event_body['detail']['eventDescription']
    description = f"<p><b>Event Description:</b></p><p>{event_description}</p>"
    description += "<p><b>Affected Resources:</b></p>"

    for resource in resources:
        resource_arn = get_resource_arn(resource)
        if resource_arn:
            status = resource.get('status', 'UNKNOWN')
            last_updated = resource.get('last_updated_time', 'UNKNOWN')
            description += (
                f"<p>Resource: {resource_arn}<br/>"
                f"Status: {status}<br/>"
                f"Last Updated: {last_updated}</p>"
            )

    # Build JSON Patch document
    patch = [
        {"op": "add", "path": "/fields/System.Title", "value": title},
        {"op": "add", "path": "/fields/System.State", "value": "New"},
        {"op": "add", "path": "/fields/Microsoft.VSTS.Common.Priority", "value": 3},
        {"op": "add", "path": "/fields/System.Description", "value": description}
    ]

    if area_path:
        patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})

    iteration_path = get_iteration_path()
    if iteration_path:
        patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})

    # Append custom required fields
    for cf in ado_custom_fields:
        patch.append({"op": "add", "path": f"/fields/{cf['field']}", "value": cf['value']})

    return patch, title


def build_child_task_payload(feature_title, feature_id, feature_url, area_path):
    """Build JSON Patch document for creating a Child Task linked to a Feature"""
    patch = [
        {"op": "add", "path": "/fields/System.Title", "value": f"Task: {feature_title}"},
        {"op": "add", "path": "/fields/System.State", "value": "New"},
        {
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": feature_url
            }
        }
    ]

    if area_path:
        patch.append({"op": "add", "path": "/fields/System.AreaPath", "value": area_path})

    iteration_path = get_iteration_path()
    if iteration_path:
        patch.append({"op": "add", "path": "/fields/System.IterationPath", "value": iteration_path})

    return patch


def build_comment_payload(resources):
    """Build comment payload for updating an existing Feature"""
    text = "Update for resources:<br/><br/>"
    for resource in resources:
        resource_arn = get_resource_arn(resource)
        if resource_arn:
            status = resource.get('status', 'UNKNOWN')
            last_updated = resource.get('last_updated_time', 'UNKNOWN')
            text += (
                f"Resource: {resource_arn}<br/>"
                f"Status: {status}<br/>"
                f"Last Updated: {last_updated}<br/><br/>"
            )
    return {"text": text}


def create_work_item(project, work_item_type, patch, headers):
    """Create a work item in ADO"""
    url = f"{ado_org_url}/{project}/_apis/wit/workitems/${work_item_type}?api-version=7.1"
    logger.info(f"Creating {work_item_type} in project {project}")

    response = http.request('POST', url, headers=headers, body=json.dumps(patch))

    if response.status in [200, 201]:
        data = json.loads(response.data)
        logger.info(f"Successfully created {work_item_type} with ID: {data['id']}")
        return data
    else:
        logger.error(f"Failed to create {work_item_type}. Status: {response.status}, Response: {response.data.decode()}")
        return None


def add_comment(project, work_item_id, payload, headers):
    """Add a comment to an existing work item"""
    url = f"{ado_org_url}/{project}/_apis/wit/workItems/{work_item_id}/comments?api-version=7.1-preview.4"
    logger.info(f"Adding comment to work item {work_item_id} in project {project}")

    response = http.request('POST', url, headers=headers, body=json.dumps(payload))

    if response.status == 200:
        data = json.loads(response.data)
        logger.info(f"Successfully added comment to work item {work_item_id}")
        return data
    else:
        logger.error(f"Failed to add comment to work item {work_item_id}. Status: {response.status}, Response: {response.data.decode()}")
        return None


def activate_work_item(project, work_item_id, headers):
    """Update a work item's state to Active and set current iteration path"""
    patch = [
        {"op": "replace", "path": "/fields/System.State", "value": "Active"}
    ]
    iteration_path = get_iteration_path()
    if iteration_path:
        patch.append({"op": "replace", "path": "/fields/System.IterationPath", "value": iteration_path})

    url = f"{ado_org_url}/{project}/_apis/wit/workitems/{work_item_id}?api-version=7.1"
    logger.info(f"Activating work item {work_item_id} in project {project}")

    response = http.request('PATCH', url, headers=headers, body=json.dumps(patch))

    if response.status == 200:
        logger.info(f"Successfully activated work item {work_item_id}")
        return json.loads(response.data)
    else:
        logger.error(f"Failed to activate work item {work_item_id}. Status: {response.status}, Response: {response.data.decode()}")
        return None


def get_project_and_area_path(event_body, identifier):
    """Look up ADO project name and area path from DynamoDB mapping table"""
    deploy_model = event_body['deployModel']
    mapping_table_name = os.environ.get('ADO_DYNAMODB_TABLE')
    if not mapping_table_name:
        raise ValueError("ADO_DYNAMODB_TABLE environment variable is not set")

    mapping_table = dynamodb.Table(mapping_table_name)

    # Model-specific key/attribute names
    config = {
        'Account': {'key': 'Account', 'project_attr': 'ACADOProjectName', 'area_attr': 'ACADOAreaPath'},
        'Service': {'key': 'Service', 'project_attr': 'SADOProjectName', 'area_attr': 'SADOAreaPath'},
        'Tag': {'key': 'HostTag', 'project_attr': 'HTADOProjectName', 'area_attr': 'HTADOAreaPath'}
    }

    model_config = config.get(deploy_model)
    if not model_config:
        raise ValueError(f"Invalid deploy model: {deploy_model}")

    # Look up identifier, fall back to DefaultProjectCode
    for lookup_key in [identifier, 'DefaultProjectCode']:
        try:
            response = mapping_table.get_item(Key={model_config['key']: lookup_key})
            if 'Item' in response:
                item = response['Item']
                project = item.get(model_config['project_attr'])
                # Use fixed ADOAreaPath if set, otherwise use DynamoDB value
                area_path = ado_area_path if ado_area_path else item.get(model_config['area_attr'], '')
                logger.info(f"Found mapping for {lookup_key}: project={project}, area_path={area_path}")
                return project, area_path
        except ClientError as e:
            logger.error(f"Error looking up mapping for {lookup_key}: {e.response['Error']['Message']}")

    logger.error(f"No mapping found for identifier {identifier} or DefaultProjectCode")
    return None, None


def lambda_handler(event, context):
    """Main Lambda handler"""

    # Parse SQS message
    event_body = json.loads(event['Records'][0]['body'])

    eventArn = event_body['detail']['eventArn']
    deployModel = event_body['deployModel']
    startTime = event_body['detail'].get('startTime', '')

    logger.info(f"Processing event: {eventArn} with deploy model: {deployModel}")

    # Get ADO PAT from Secrets Manager
    ado_secret = get_secret()
    pat = ado_secret.get('pat', ado_secret.get('PAT', ''))
    if not pat:
        raise ValueError("PAT not found in secret")

    headers_patch = get_ado_headers(pat)
    headers_json = get_ado_headers_json(pat)

    # Process untracked resources (new work items)
    untracked_resources = event_body.get('untrackedResources', {})
    if untracked_resources is None or untracked_resources == []:
        untracked_resources = {}
    elif not isinstance(untracked_resources, dict):
        logger.warning(f"untrackedResources is not a dictionary: {type(untracked_resources)}. Converting to empty dict.")
        untracked_resources = {}

    for identifier, resources in untracked_resources.items():
        logger.info(f"Processing resources for {deployModel} {identifier} with {len(resources)} resources")

        # Look up project and area path
        project, area_path = get_project_and_area_path(event_body, identifier)
        if not project:
            logger.error(f"No project mapping found for identifier {identifier}, skipping")
            continue

        # Check if we already have a Feature for this event
        existing_work_item_id, existing_project = find_existing_workitem_for_event(eventArn, identifier)

        if existing_work_item_id:
            logger.info(f"Found existing Feature {existing_work_item_id} for event {eventArn}")

            # Add comment to existing Feature
            comment_payload = build_comment_payload(resources)
            add_comment(existing_project or project, existing_work_item_id, comment_payload, headers_json)

            # Auto-activate if enabled
            if enable_auto_activate:
                activate_work_item(existing_project or project, existing_work_item_id, headers_patch)

            # Track all resources
            for resource in resources:
                resource_arn = get_resource_arn(resource)
                if resource_arn:
                    store_event_tracking(eventArn, startTime, existing_work_item_id, resource_arn, project)
        else:
            logger.info(f"No existing Feature found for event {eventArn}, creating new Feature + Child Task")

            # Step 1: Create Feature
            feature_patch, feature_title = build_feature_payload(event_body, identifier, resources, area_path)
            feature_data = create_work_item(project, 'Feature', feature_patch, headers_patch)

            if feature_data:
                feature_id = feature_data['id']
                feature_url = feature_data['url']

                logger.info(f"Successfully created Feature {feature_id} in project {project}")

                # Step 2: Create Child Task
                task_patch = build_child_task_payload(feature_title, feature_id, feature_url, area_path)
                task_data = create_work_item(project, 'Task', task_patch, headers_patch)

                if task_data:
                    logger.info(f"Successfully created Child Task {task_data['id']} linked to Feature {feature_id}")
                else:
                    logger.error(f"Failed to create Child Task for Feature {feature_id}")

                # Track all resources against the Feature
                for resource in resources:
                    resource_arn = get_resource_arn(resource)
                    if resource_arn:
                        store_event_tracking(eventArn, startTime, feature_id, resource_arn, project)
                    else:
                        logger.warning(f"Could not extract resource ARN from resource: {resource}")

    # Process tracked resources (updates to existing Features)
    logger.info(f"Processing tracked resources for {deployModel} mode")

    tracked_resources = event_body.get('trackedResources', [])
    if tracked_resources:
        # Group resources by adoWorkItemId
        work_item_groups = {}
        for resource in tracked_resources:
            resource_arn = resource.get('arn')
            if not resource_arn:
                logger.warning(f"Resource missing arn: {resource}")
                continue

            tracking_info = check_tracking_table(eventArn, resource_arn)
            if tracking_info and 'adoWorkItemId' in tracking_info:
                wi_id = int(tracking_info['adoWorkItemId'])
                wi_project = tracking_info.get('adoProject', '')
                key = (wi_id, wi_project)
                if key not in work_item_groups:
                    work_item_groups[key] = []
                work_item_groups[key].append(resource)
            else:
                logger.warning(f"No tracking info found for resource: {resource_arn}")

        # Add comments to each Feature
        for (wi_id, wi_project), resources in work_item_groups.items():
            logger.info(f"Updating Feature {wi_id} with {len(resources)} resources")
            comment_payload = build_comment_payload(resources)
            add_comment(wi_project, wi_id, comment_payload, headers_json)

            # Auto-activate if enabled
            if enable_auto_activate:
                activate_work_item(wi_project, wi_id, headers_patch)


logger.info('Lambda function initialized')
