import json
import os
import logging
import sys
import urllib3
import boto3
from botocore.exceptions import ClientError

# Configure logging with a formatter
logger = logging.getLogger('snow_lambda')
logger.setLevel(logging.INFO)

# Create console handler and set level
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)

# Create formatter
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Add formatter to console handler
console_handler.setFormatter(formatter)

# Add console handler to logger
logger.addHandler(console_handler)


http = urllib3.PoolManager()
snow_url = os.environ.get('SNOW_URL')
if not snow_url:
    raise ValueError("SNOW_URL environment variable is not set")

#setup boto3 session
session = boto3.session.Session()

#setup ddb table name for tracking
track_table_name = os.environ.get('DYNAMODB_TRACK_TABLE')
if not track_table_name:
    raise ValueError("DYNAMODB_TRACK_TABLE environment variable is not set")

dynamodb = boto3.resource('dynamodb')
track_table = dynamodb.Table(track_table_name)
logger.info(f"DynamoDB tracking table status: {track_table.table_status}")


def get_secret():
    secret_name = os.environ.get('SNOW_SECRET_NAME')
    if not secret_name:
        raise ValueError("SNOW_SECRET_NAME environment variable is not set")

    region_name = os.environ.get('AWS_REGION', 'us-east-1')

    # Create a Secrets Manager client
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )

    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        # For a list of exceptions thrown, see
        # https://docs.aws.amazon.com/secretsmanager/latest/apireference/API_GetSecretValue.html
        raise e

    # Decrypts secret using the associated KMS key.
    secret = json.loads(get_secret_value_response['SecretString'])

    return secret

def check_tracking_table(event_arn, resource_arn):
    """
    Check if a resource is already being tracked for a specific event

    Args:
        event_arn: The ARN of the event
        resource_arn: The ARN of the resource

    Returns:
        dict: The tracking item if found, None otherwise
    """
    try:
        # Query using the primary key (resourceArn and eventArn)
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

def find_existing_change_for_event(event_arn, identifier):
    """
    Find an existing ServiceNow change for an event and identifier

    Args:
        event_arn: The ARN of the event
        identifier: The identifier (account, service, or tag)

    Returns:
        str: The sys_id of the existing change if found, None otherwise
    """
    try:
        # Query using GSI for eventArn
        response = track_table.query(
            IndexName='TETkeyIndex',
            KeyConditionExpression='eventArn = :event_arn',
            ExpressionAttributeValues={
                ':event_arn': event_arn
            }
        )

        # Look for any item with a snowSysID
        for item in response.get('Items', []):
            if 'snowSysID' in item:
                logger.info(f"Found existing change with sys_id {item['snowSysID']} for event {event_arn}")
                return item['snowSysID']

        logger.info(f"No existing change found for event {event_arn}")
        return None

    except ClientError as e:
        logger.error(f"Error querying tracking table for existing change: {e.response['Error']['Message']}")
        return None

def find_resources_by_event_and_sys_id(event_arn, sys_id):
    """
    Find all resources tracked for a specific event and ServiceNow sys_id

    Args:
        event_arn: The ARN of the event
        sys_id: The ServiceNow sys_id

    Returns:
        list: List of resource ARNs
    """
    try:
        # Query using GSI for eventArn and filter by snowSysID
        response = track_table.query(
            IndexName='TETkeyIndex',
            KeyConditionExpression='eventArn = :event_arn',
            FilterExpression='snowSysID = :sys_id',
            ExpressionAttributeValues={
                ':event_arn': event_arn,
                ':sys_id': sys_id
            }
        )

        resources = []
        for item in response.get('Items', []):
            resources.append(item['resourceArn'])

        logger.info(f"Found {len(resources)} resources for event {event_arn} and sys_id {sys_id}")
        return resources

    except ClientError as e:
        logger.error(f"Error querying tracking table by sys_id: {e.response['Error']['Message']}")
        return []

def store_event_tracking(event_arn, start_time, sys_id, resource_arn, change_number=None):
    """
    Store event tracking information in DynamoDB tracking table

    Args:
        event_arn: The ARN of the event
        start_time: Event start time
        sys_id: The ServiceNow sys_id
        resource_arn: The resource ARN
        change_number: The ServiceNow change number (optional)

    Returns:
        bool: True if successful, False if failed
    """
    from datetime import datetime
    from dateutil.relativedelta import relativedelta

    try:
        # Parse start time and calculate expiration time (2 years from start)
        if isinstance(start_time, str):
            try:
                start_time_format = datetime.strptime(start_time, "%a, %d %b %Y %H:%M:%S %Z")
            except ValueError:
                # Try alternative format
                try:
                    start_time_format = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%S.%fZ")
                except ValueError:
                    # If all parsing fails, use current time
                    start_time_format = datetime.now()
        else:
            start_time_format = datetime.now()

        expiration_time = int((start_time_format + relativedelta(years=2)).timestamp())

        # Create item to store in DynamoDB
        item = {
            'eventArn': event_arn,
            'resourceArn': resource_arn,
            'snowSysID': sys_id,  # Use snowSysID instead of ticketId for ServiceNow
            'expirationTime': expiration_time
        }

        # Add change number if provided
        if change_number:
            item['ChangeID'] = change_number

        # Put item in the tracking table
        track_table.put_item(Item=item)
        logger.info(f"Successfully stored event tracking for resource: {resource_arn} with sys_id: {sys_id}")
        return True

    except Exception as e:
        logger.error(f"Error storing event tracking: {str(e)}")
        return False

def get_resource_arn(resource):
    """
    Extract resource ARN from the resource object

    Args:
        resource: Resource object from the event

    Returns:
        str: Resource ARN
    """
    if isinstance(resource.get('arn'), dict) and 'resource_arn' in resource['arn']:
        return resource['arn']['resource_arn']
    return resource.get('arn')

def create_change(headers, payload):
    # Format resources list for description

    response = http.request('POST', f"{snow_url}/api/now/table/change_request",
                          headers=headers, body=json.dumps(payload))

    if response.status == 201:
        data = json.loads(response.data)
        logger.info(f"Successfully created change: {data['result']['number']} with sys_id: {data['result']['sys_id']}")
    else:
        logger.error(f"Failed to create change. Response code: {response.status}")

    return response

def update_change(sys_id, headers, payload):
    # Update an existing change request

    logger.debug(f"Updating change with sys_id: {sys_id}")
    #logger.debug(f"Headers: {headers}")
    logger.debug(f"URL: {snow_url}/api/now/table/change_request/{sys_id}")

    response = http.request('PATCH', f"{snow_url}/api/now/table/change_request/{sys_id}",
                          headers=headers, body=json.dumps(payload))

    if response.status == 200:
        data = json.loads(response.data)
        logger.info(f"Successfully updated change: {data['result']['number']} with sys_id: {data['result']['sys_id']}")
    else:
        data = json.loads(response.data)
        logger.error(f"Failed to update change with sys_id: {sys_id}. Response code: {response.status}")
        logger.error(f"Response data: {data}")

    return response

def get_payload(event_body, identifier, resources, is_update=False):
    """
    Generate payload for ServiceNow change request

    Args:
        event_body: The event body
        identifier: The identifier (account, service, or tag)
        resources: List of resources
        is_update: Whether this is an update to an existing change (default: False)

    Returns:
        dict: Payload for ServiceNow change request
    """
    # For updates, we only need to include work_notes with resource details
    if is_update:
        work_notes = "Update for resources:\n\n"

        # Add resource details to work_notes
        for resource in resources:
            resource_arn = get_resource_arn(resource)
            if resource_arn:
                status = resource['arn'].get('status', 'UNKNOWN') if isinstance(resource.get('arn'), dict) else resource.get('status', 'UNKNOWN')
                last_updated_time = resource['arn'].get('last_updated_time', 'UNKNOWN') if isinstance(resource.get('arn'), dict) else resource.get('last_updated_time', 'UNKNOWN')
                work_notes += (
                    f"Resource: {resource_arn}\n"
                    f"Status: {status}\n"
                    f"Last Updated: {last_updated_time}\n\n"
                )

        return {
            "work_notes": work_notes
        }

    # For new changes, include all fields
    # Extract common fields
    eventTypeCode = event_body['detail']['eventTypeCode']
    service = event_body['detail']['service']
    deployModel = event_body['deployModel']

    # Get the event description (in queue-item format, it's a direct string)
    event_description = event_body['detail']['eventDescription']

    # Build payload based on deploy model
    if deployModel == 'Account':
        short_desc = f"Account: {identifier} - AWS {service} Planned Maintenance - {eventTypeCode}"
    elif deployModel == 'Tag':
        short_desc = f"Tag: {identifier} - AWS {service} Planned Maintenance - {eventTypeCode} - Tag Based"
    elif deployModel == 'Service':
        short_desc = f"Service: {identifier} - AWS {service} Planned Maintenance - {eventTypeCode} - Service Based"
    else:
        short_desc = f"AWS Planned Maintenance - {eventTypeCode}"

    # Create description with just the event description
    description = f"Event Description:\n{event_description}"

    # Create work_notes with detailed resource information (matching update format)
    work_notes = "Affected Resources:\n\n"

    # Add resource details to work_notes
    for resource in resources:
        resource_arn = get_resource_arn(resource)
        if resource_arn:
            status = resource['arn'].get('status', 'UNKNOWN') if isinstance(resource.get('arn'), dict) else resource.get('status', 'UNKNOWN')
            last_updated_time = resource['arn'].get('last_updated_time', 'UNKNOWN') if isinstance(resource.get('arn'), dict) else resource.get('last_updated_time', 'UNKNOWN')
            work_notes += (
                f"Resource: {resource_arn}\n"
                f"Status: {status}\n"
                f"Last Updated: {last_updated_time}\n\n"
            )

    payload = {
        "short_description": short_desc,
        "description": description,
        "work_notes": work_notes,
        "type": "Normal",
        "category": "AWS"
    }

    return payload



def lambda_handler(event, context):

    # Parse SQS message
    event_body = json.loads(event['Records'][0]['body'])

    # Parse Event Information
    eventArn = event_body['detail']['eventArn']
    deployModel = event_body['deployModel']
    startTime = event_body['detail'].get('startTime', '')

    logger.info(f"Processing event: {eventArn} with deploy model: {deployModel}")

    # Pull secret and populate credentials from secrets manager
    snow_secret = get_secret()
    snow_creds = snow_secret['username'] + ":" + snow_secret['password']

    # Populate headers for http requests
    headers = urllib3.make_headers(basic_auth=snow_creds)

    # Get untrackedResources and ensure it's a dictionary
    untracked_resources = event_body.get('untrackedResources', {})
    if untracked_resources is None or untracked_resources == []:
        logger.info("No untracked resources found or untracked_resources is empty array")
        untracked_resources = {}
    elif not isinstance(untracked_resources, dict):
        logger.warning(f"untrackedResources is not a dictionary: {type(untracked_resources)}. Converting to empty dict.")
        untracked_resources = {}

    # Process each identifier's resources
    for identifier, resources in untracked_resources.items():
        logger.info(f"Processing resources for {deployModel} {identifier} with {len(resources)} resources")

        # Check if we already have a change for this event
        existing_sys_id = find_existing_change_for_event(eventArn, identifier)

        if existing_sys_id:
            logger.info(f"Found existing change with sys_id {existing_sys_id} for event {eventArn}")

            # Create update payload using get_payload with is_update=True
            update_payload = get_payload(event_body, identifier, resources, is_update=True)

            # Track all resources
            for resource in resources:
                resource_arn = get_resource_arn(resource)
                if resource_arn:
                    # Make sure this resource is tracked
                    store_event_tracking(eventArn, startTime, existing_sys_id, resource_arn)

            # Update the change request
            response = update_change(existing_sys_id, headers, update_payload)
        else:
            logger.info(f"No existing change found for event {eventArn}, creating new change")

            # Create ServiceNow change request
            payload = get_payload(event_body, identifier, resources, is_update=False)
            response = create_change(headers, payload)

            if response.status == 201:
                data = json.loads(response.data)
                sys_id = data['result']['sys_id']
                change_number = data['result']['number']

                logger.info(f"Successfully created ServiceNow change {change_number} with sys_id {sys_id}")

                # Track all resources in the tracking table
                for resource in resources:
                    resource_arn = get_resource_arn(resource)
                    if resource_arn:
                        store_event_tracking(eventArn, startTime, sys_id, resource_arn, change_number)
                    else:
                        logger.warning(f"Could not extract resource ARN from resource: {resource}")

    # Process tracked resources from the trackedResources field
    logger.info(f"Processing tracked resources for {deployModel} mode")

    # Get trackedResources as a list
    tracked_resources = event_body.get('trackedResources', [])
    if tracked_resources:
        # Group resources by snowSysID
        sys_id_groups = {}
        for resource in tracked_resources:
            # Check if this resource has tracking info
            resource_arn = resource.get('arn')
            if not resource_arn:
                logger.warning(f"Resource missing arn: {resource}")
                continue

            # Look up tracking info for this resource
            tracking_info = check_tracking_table(eventArn, resource_arn)
            if tracking_info and 'snowSysID' in tracking_info:
                sys_id = tracking_info['snowSysID']
                if sys_id not in sys_id_groups:
                    sys_id_groups[sys_id] = []
                sys_id_groups[sys_id].append(resource)
            else:
                logger.warning(f"No tracking info found for resource: {resource_arn}")

        # Process each sys_id group
        for sys_id, resources in sys_id_groups.items():
            logger.info(f"Updating ServiceNow change with sys_id {sys_id} for {len(resources)} resources")

            # Create update payload using get_payload with is_update=True
            # Note: We don't have an identifier here, but it's not needed for updates
            update_payload = get_payload(event_body, "", resources, is_update=True)

            # Update the change request
            response = update_change(sys_id, headers, update_payload)


logger.info('Lambda function initialized')
