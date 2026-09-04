"""
Need to add a module description
"""

import json
import logging
import http.client
import ssl
import os
import base64
from urllib.parse import urlparse
from typing import Dict, Any, List, Tuple
from datetime import datetime
import boto3
from botocore.exceptions import ClientError
from dateutil.relativedelta import relativedelta

# Setup Logger
logger = logging.getLogger()
logger.setLevel(logging.INFO)

class JiraProjectTable:
    """Handles DynamoDB operations based on deployment model"""

    def __init__(self):
        """Initialize with configuration based on deployment model"""
        self.dynamodb = boto3.client('dynamodb')
        self.table_name = os.environ.get('JIRA_DYNAMODB_TABLE')
        self.deploy_model = os.environ.get('DEPLOY_MODEL')

        if not self.table_name:
            raise ValueError("JIRA_DYNAMODB_TABLE environment variable is not set")
        if not self.deploy_model:
            raise ValueError("DEPLOY_MODEL environment variable is not set")

        # Model-specific configurations
        self.config = {
            'Account': {
                'primary_key': 'Account',
                'project_key': 'ACJiraProjectKey',
                'issue_type_id': 'ACJiraIssueTypeID',
                'index_name': 'ACkeyIndex'
            },
            'Service': {
                'primary_key': 'Service',
                'project_key': 'SJiraProjectKey',
                'issue_type_id': 'SJiraIssueTypeID',
                'index_name': 'SkeyIndex'
            },
            'Tag': {
                'primary_key': 'HostTag',
                'project_key': 'HTJiraProjectKey',
                'issue_type_id': 'HTJiraIssueTypeID',
                'index_name': 'HTkeyIndex'
            }
        }

        if self.deploy_model not in self.config:
            raise ValueError(f"Invalid deploy model: {self.deploy_model}")

        self.model_config = self.config[self.deploy_model]

#
def store_event_tracking(event_arn: str, start_time: str, ticket_id: str, resource_arn: str, response_key: str = None) -> bool:
    """
    Store event tracking information in DynamoDB

    Parameters:
    event_arn (str): The ARN of the event
    start_time (str): PLE end time
    ticket_id (str): The Jira ticket ID
    resource_arn (str): resource arn
    response_key (str, optional): Response key from the API call

    Returns:
    bool: True if successful, False if failed
    """
    dynamodb = boto3.resource('dynamodb')
    table_name = os.environ.get('DYNAMODB_TRACK_TABLE')
    table = dynamodb.Table(table_name)
    start_time_format = datetime.strptime(start_time, "%a, %d %b %Y %H:%M:%S %Z")
    expiration_time = int((start_time_format + relativedelta(years=2)).timestamp())

    try:
        # Create item to store in DynamoDB
        item = {
            'eventArn': event_arn,
            'ticketId': ticket_id,
            'resourceArn': resource_arn,
            'expirationTime': expiration_time
        }

        # Add response key if provided
        if response_key:
            item['ticketkey'] = response_key

        # Put item in the table
        table.put_item(Item=item)

        logger.info(f"Successfully stored event tracking: {event_arn}")
        return True

    except ClientError as e:
        logger.error(f"Error storing event tracking: {str(e)}")
        return False

#
def post_to_jira(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post issue to JIRA
    
    Args:
        payload: JIRA ticket payload
        
    Returns:
        dict: JIRA API response
    """
    try:
        host, path = get_jira_url()
        secret_dict = get_jira_credentials()

       # auth = base64.b64encode(f"{email}:{api_token}".encode()).decode()
        auth_string = f"{secret_dict['jira_user_email']}:{secret_dict['jira_api_token']}"
        auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}"
        }

        conn = http.client.HTTPSConnection(host)
        conn.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers=headers
        )

        response = conn.getresponse()
        response_data = json.loads(response.read().decode())

        if response.status not in (200, 201):
            raise Exception(f"Failed to create JIRA ticket: {response_data}")

        return response_data

    except Exception as e:
        logger.error(f"Error posting to JIRA: {str(e)}")
        raise
    finally:
        conn.close()

#
def create_jira_payload(
    event: Dict[str, Any],
    identifier: str,
    resources: List[Dict[str, str]],
    jiraprojectid: str,
    issuetypeid: str
) -> Dict[str, Any]:
    """
    Creates a JIRA ticket payload
    
    Args:
        event: AWS Health event
        identifier: Resource identifier
        resources: List of affected resources
        jiraprojectid: JIRA project key
        issuetypeid: JIRA issue type ID
        
    Returns:
        dict: JIRA ticket payload
    """
    table = JiraProjectTable()
    logger.info(f"Processing resources: {resources}")
    service = event["detail"]["service"]
    event_category = event["detail"]["eventTypeCategory"]
    event_type = event["detail"]["eventTypeCode"]
    deploy_model = table.deploy_model

    resource_details = []
    for resource in resources:
        resource_detail = f"ARN: {resource['arn']}\n"
        if deploy_model == 'Tag' and 'host_tag' in resource:
            resource_detail += f"\n  Host Tag: {resource['host_tag']}"
        resource_details.append(resource_detail)

    # Format identifier display based on deployment model
    if deploy_model == 'Tag':
        display_title = f"Host Tag: {identifier}"
    else:
        display_title = f"{table.model_config['primary_key']}: {identifier}"
    logger.info(f"Display Title: {display_title}")
    description = f"""
*Event Details*
• Service: {service}
• Category: {event_category}
• Event Type: {event_type}
• Start Time: {event['detail']['startTime']}
• Affected Account: {event['detail']['account']}
• Region: {event['detail']['region']}
• Event ARN: {event['detail']['eventArn']}

*Affected Resources for {display_title}*
{chr(10).join(resource_details)}

*Description*
{event['detail']['eventDescription']}

*Additional Information*
• Event ID: {event['detail']['id']}
• Event Time: {event['detail']['time']}
"""

    # Create appropriate summary based on deployment model
    if deploy_model == 'Tag':
        summary = f"{service} {event_category}: {len(resources)} resources affected | Host: {identifier}"
    else:
        summary = f"{service} {event_category}: {len(resources)} resources affected | {table.model_config['primary_key']}: {identifier}"
    logger.info(f"Summary: {summary}")
    return {
        "fields": {
            "project": {
                "key": jiraprojectid
            },
            "summary": summary,
            "description": description,
            "issuetype": {
                "id": issuetypeid
            },
            "labels": [
                f"{service}",
                f"{event_category}",
                f"{event_type}",
                f"{deploy_model}-{identifier}"
            ]
        }
    }

#
def get_default_jira_project() -> Dict[str, str]:
    """
    Get default JIRA project details from DynamoDB

    Returns:
        dict: Project key and issue type ID
    """
    table = JiraProjectTable()
    try:
        response = table.dynamodb.get_item(
            TableName=table.table_name,
            Key={
                table.model_config['primary_key']: {'S': 'DefaultProjectCode'}
            }
        )

        if 'Item' not in response:
            raise Exception("Default JIRA project configuration not found in DynamoDB")

        return {
            'projectKey': response['Item'][table.model_config['project_key']]['S'],
            'issueTypeId': response['Item'][table.model_config['issue_type_id']]['N']
        }
    except Exception as e:
        logger.error(
            "Unexpected error fetching default JIRA project",
            extra={
                'error_type': type(e).__name__,
                'error_message': str(e)
            },
            exc_info=True
        )
        raise

#
def query_jira_project(instance_identifier: str) -> Dict[str, Any]:
    """
    Query DynamoDB table based on deployment model

    Args:
        instance_identifier: Identifier based on deployment model

    Returns:
        dict: DynamoDB response
    """
    table = JiraProjectTable()
    logger.info(instance_identifier)
    logger.info(table.table_name)
    try:
        response = table.dynamodb.query(
            TableName=table.table_name,
            KeyConditionExpression=f"{table.model_config['primary_key']} = :identifier",
            ExpressionAttributeValues={
                ':identifier': {'S': instance_identifier}
            }
        )
        return response
    except Exception as e:
        logger.error(f"Error querying DynamoDB: {str(e)}")
        raise

#
def get_secret_value(secret_name: str) -> str:
    """
    Retrieve a secret from AWS Secrets Manager

    Args:
        secret_name: Name of the secret to retrieve

    Returns:
        str: The secret value

    Raises:
        ValueError: If secret_name is None or empty
        RuntimeError: If there are AWS service errors
    """
    if not secret_name:
        raise ValueError("Secret name cannot be None or empty")

    try:
        session = boto3.session.Session()
        client = session.client(
            service_name='secretsmanager',
            region_name=os.environ.get('AWS_REGION', 'us-east-1')
        )

        response = client.get_secret_value(SecretId=secret_name)
        # if it's just a string we can return it
        if 'SecretString' in response:
            return response['SecretString']

        # otherwise we need to decode it before returning
        return base64.b64decode(response['SecretBinary'])

    except Exception as e:
        logger.error(
            "Unexpected error retrieving secret",
            extra={
                'secret_name': secret_name,
                'error_type': type(e).__name__,
                'error_message': str(e)
            },
            exc_info=True
        )
        raise

#
def get_jira_credentials():
    """
    Retrieve and parse JIRA credentials from Secrets Manager

    Returns:
        dict: Dictionary containing JIRA credentials

    Raises:
        ValueError: If secret is not valid JSON or missing required fields
        RuntimeError: If there are AWS service errors
    """
    # Get and validate SECRET_NAME environment variable
    secret_name = os.environ.get('JIRA_SECRET_NAME')
    if not secret_name:
        error_msg = "SECRET_NAME environment variable is not set"
        logger.error(error_msg)
        raise ValueError(error_msg)

    try:
        # Get secret value from Secrets Manager
        secret_string = get_secret_value(secret_name)

        # Parse JSON string to dictionary
        secret = json.loads(secret_string)

        # Validate required fields
        required_fields = ['jira_user_email', 'jira_api_token']
        missing_fields = [field for field in required_fields if field not in secret]

        if missing_fields:
            raise ValueError(f"Missing required fields in secret: {', '.join(missing_fields)}")

        return {
            'jira_user_email': secret['jira_user_email'],
            'jira_api_token': secret['jira_api_token']
        }

    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse secret as JSON: {str(e)}")
        raise ValueError("Secret value is not valid JSON") from e

    except Exception as e:
        logger.error(f"Error retrieving JIRA credentials: {str(e)}")
        raise RuntimeError(f"Failed to get JIRA credentials: {str(e)}") from e

#
def get_jira_url() -> Tuple[str, str]:
    """
    Get Jira URL from environment variable and parse it

    Returns:
        tuple: (host, path)
    """
    jira_base_url = os.environ.get('JIRA_BASE_URL')
    if not jira_base_url:
        raise ValueError("JIRA_BASE_URL environment variable is not set")

    parsed_url = urlparse(jira_base_url)
    host = parsed_url.netloc
    path = f"{parsed_url.path.rstrip('/')}/rest/api/2/issue/"

    return (host, path)

#
def update_jira_ticket(ticket_id: str, resources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Update an existing Jira ticket with new information

    Args:
        ticket_id (str): The Jira ticket ID to update
        event (dict): The AWS Health event containing all details

    Returns:
        dict: Response from Jira API
    """
    try:
        # Create update payload with comment about the update
        #create list of affected resource updates
        logger.info(f"update_jira_ticket: Updating ticket {ticket_id}")
        resource_payload = "*Affected Resources update:*\n"
        for resource in resources:
            resource_payload += (
                f"*Entity Value:* {resource['resource_arn']}\n"
                f"*Status:* {resource['status']}\n"
                f"*Last Updated:* {resource['last_updated_time']}\n\n"
            )
        #logger.info(f"update_jira_ticket: Affected resource payload {resource_payload}")
        update_payload = {
            "update": {
                "comment": [{
                    "add": {
                        "body": f"""


{resource_payload}


"""
                    }
                }]
            }
        }

        # Get Jira credentials and URL
        host, base_path = get_jira_url()
        credentials = get_jira_credentials()

        # Create auth header
        auth_string = f"{credentials['jira_user_email']}:{credentials['jira_api_token']}"
        auth = base64.b64encode(auth_string.encode('utf-8')).decode('utf-8')

        # Set up HTTP connection
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}"
        }

        # Construct the full URL path for the update
        update_path = f"{base_path.rstrip('/')}/{ticket_id}"

        #  Create a secure HTTPS connection with proper SSL context
        ssl_context = ssl.create_default_context()
        ssl_context.verify_mode = ssl.CERT_REQUIRED
        ssl_context.check_hostname = True
        conn = http.client.HTTPSConnection(
            host=host,
            context=ssl_context,
            timeout=30  # Add timeout for safety
        )

        try:
            # Send PUT request to update the ticket
            conn.request(
                "PUT",
                update_path,
                body=json.dumps(update_payload),
                headers=headers
            )

            # Get response
            response = conn.getresponse()
            response_data = response.read().decode()

            # Check response status
            if response.status not in (200, 201, 204):
                raise Exception(f"update_jira_ticket: Failed to update Jira ticket: {response_data}")

            logger.info(f"update_jira_ticket: Successfully updated Jira ticket {ticket_id}")

            return {
                'status': 'success',
                'ticket_id': ticket_id,
                'response': response_data if response_data else 'Update successful'
            }

        finally:
            conn.close()

    except Exception as e:
        error_msg = f"update_jira_ticket: Error updating Jira ticket {ticket_id}: {str(e)}"
        logger.info(error_msg)
        raise Exception(error_msg) from e

#
def group_by_ticket_id(tracked_resources):
    """
    Group resources by ticketId
    Args:
        tracked_resources (list): List of resources with ticketId
    Returns:
        dict: Resources grouped by ticketId
    """
    ticket_groups = {}

    for resource in tracked_resources:
        # Note the change from ticket_id to ticketId to match your input
        ticket_id = resource.get('ticket_id')
        if ticket_id:
            if ticket_id not in ticket_groups:
                ticket_groups[ticket_id] = []
            ticket_groups[ticket_id].append(resource)
        else:
            logger.warning(f"Resource missing ticket_id: {resource}")

    return ticket_groups

#
def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Main Lambda handler for processing SQS messages and updating Jira
    """
    logger.info(f"main:Received event: {json.dumps(event)}")
    try:
        # Initialize the dictionary
        ticket_groups = {}
        for record in event['Records']:
            try:
                message = json.loads(record['body'])
                logger.info(message)
                # Process tracked resources (updates)
                tracked_resources = message.get('trackedResources',[])
                if tracked_resources:
                    # Group resources by ticket ID
                    logger.info(f"Processing tracked resources: {json.dumps(tracked_resources)}")
                    ticket_groups = group_by_ticket_id(tracked_resources)
                    logger.info(f"Grouped resources by ticket: {json.dumps(ticket_groups)}")
                    # Process each ticket
                    updated_tickets = []
                    for ticket_id, resources in ticket_groups.items():
                        try:
                            # Collect entity details for each resource in this ticket
                            logger.info(f"main:Processing ticket_id {ticket_id} resources {resources}")

                            # Update Jira ticket with all resource details
                            update_response = update_jira_ticket(
                                ticket_id=ticket_id,
                                resources=resources
                            )
                            updated_tickets.append({
                                'ticket_id': ticket_id,
                                'status': 'updated',
                                'response': update_response
                            })

                        except Exception as e:
                            logger.error(f"Error processing ticket {ticket_id}: {str(e)}")
                            raise

                if message.get('untrackedResources'):
                    logger.info(f"Processing untracked resources: {json.dumps(message['untrackedResources'])}")
                    # Event does not exist, create new Jira ticket
                    # Group untracked resources by identifier
                    resources_by_identifier = message.get('untrackedResources', {})
                    created_tickets = []
                    event_arn = message["detail"]["eventArn"]
                    start_time=message["detail"]["startTime"]
                    for identifier, resources in resources_by_identifier.items():
                        try:
                            # Query DynamoDB for JIRA project mapping
                            logger.info(identifier)
                            response = query_jira_project(identifier)
                            logger.info(response)
                            if response['Items']:
                                item = response['Items'][0]
                                table = JiraProjectTable()
                                jira_project_key = item[table.model_config['project_key']]['S']
                                jira_issue_type_id = item[table.model_config['issue_type_id']]['N']

                            else:
                                # Use default project if no mapping found
                                default_project = get_default_jira_project()
                                jira_project_key = default_project['projectKey']
                                jira_issue_type_id = default_project['issueTypeId']

                            # Create and post JIRA ticket
                            logger.info(message)
                            payload = create_jira_payload(
                                message,
                                identifier,
                                resources,
                                jira_project_key,
                                jira_issue_type_id
                            )
                            response = post_to_jira(payload)
                            ticket_id = response['id']    # Gets the unique ticket ID
                            logger.info(f"Successfully created Jira ticket {ticket_id}")
                            # Store each ticket into dynamodb
                            resource_arns = []
                            for resource in resources:
                                resource_arns.append(resource['arn'])
                            for resource_arn in resource_arns:
                                print(f"Processing ARN: {resource_arn}")
                                # Your processing logic here
                                store_event_tracking(event_arn, start_time, ticket_id, resource_arn, response['key'])

                            created_tickets.append({
                                'identifier': identifier,
                                'ticket_key': response['key'],
                                'ticket_id': response['id']
                            })

                        except Exception as e:
                            logger.error(f"Error processing identifier {identifier}: {str(e)}")
                            raise

                    return {
                        'statusCode': 200,
                        'body': {
                            'message': f"Created {len(created_tickets)} JIRA tickets",
                            'tickets': created_tickets
                        }
                    }
            except Exception as e:
                logger.error(f"Error in lambda handler: {str(e)}")
                raise

    except Exception as e:
        logger.error(f"Error in lambda handler: {str(e)}", exc_info=True)
        # Don't return a response - just raise the exception
        raise
