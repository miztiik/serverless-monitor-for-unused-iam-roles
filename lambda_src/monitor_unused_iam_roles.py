import boto3
from botocore.exceptions import ClientError
from botocore.config import Config
import datetime
import fnmatch
import json
import os
import re
import logging


logger = logging.getLogger()
logging.basicConfig(
    format="[%(asctime)s] %(levelname)s [%(module)s.%(funcName)s:%(lineno)d] %(message)s", datefmt="%H:%M:%S"
)
logger.setLevel(os.getenv('log_level', logging.INFO))

# Configure boto retries
BOTO_CONFIG = Config(retries=dict(max_attempts=5))

# Define the default resource to report to Config Rules
DEFAULT_RESOURCE_TYPE = 'AWS::IAM::Role'

CONFIG_ROLE_TIMEOUT_SECONDS = 60

# Set to True to get the lambda to assume the Role attached on the Config service (useful for cross-account).
ASSUME_ROLE_MODE = False

# Evaluation strings for Config evaluations
COMPLIANT = 'COMPLIANT'
NON_COMPLIANT = 'NON_COMPLIANT'

# MAX UNUSED DAYS
MAX_UNUSED_DAYS = 30


# This gets the client after assuming the Config service role either in the same AWS account or cross-account.
def get_client(service, execution_role_arn):
    if not ASSUME_ROLE_MODE:
        return boto3.client(service)
    credentials = get_assume_role_credentials(execution_role_arn)
    return boto3.client(service, aws_access_key_id=credentials['AccessKeyId'],
                        aws_secret_access_key=credentials['SecretAccessKey'],
                        aws_session_token=credentials['SessionToken'],
                        config=BOTO_CONFIG
                        )


def get_assume_role_credentials(execution_role_arn):
    sts_client = boto3.client('sts')
    try:
        assume_role_response = sts_client.assume_role(RoleArn=execution_role_arn,
                                                      RoleSessionName="configLambdaExecution",
                                                      DurationSeconds=CONFIG_ROLE_TIMEOUT_SECONDS)
        return assume_role_response['Credentials']
    except ClientError as ex:
        if 'AccessDenied' in ex.response['Error']['Code']:
            ex.response['Error']['Message'] = "AWS Config does not have permission to assume the IAM role."
        else:
            ex.response['Error']['Message'] = "InternalError"
            ex.response['Error']['Code'] = "InternalError"
        raise ex


# Validates role pathname whitelist as passed via AWS Config parameters and returns a list of comma separated patterns.
def validate_whitelist(unvalidated_role_pattern_whitelist):
    # Names of users, groups, roles must be alphanumeric, including the following common
    # characters: plus (+), equal (=), comma (,), period (.), at (@), underscore (_), and hyphen (-).

    if not unvalidated_role_pattern_whitelist:
        return None

    regex = re.compile('^[-a-zA-Z0-9+=,.@_/|*]+')
    if regex.search(unvalidated_role_pattern_whitelist):
        raise ValueError("[Error] Provided whitelist has invalid characters")

    return unvalidated_role_pattern_whitelist.split('|')


# This uses Unix filename pattern matching (as opposed to regular expressions), as documented here:
# https://docs.python.org/3.7/library/fnmatch.html.  Please note that if using a wildcard, e.g. "*", you should use
# it sparingly/appropriately.
# If the rolename matches the pattern, then it is whitelisted
def is_whitelisted_role(role_pathname, pattern_list):
    if not pattern_list:
        return False

    # If role_pathname matches pattern, then return True, else False
    # eg. /service-role/aws-codestar-service-role matches pattern /service-role/*
    # https://docs.python.org/3.7/library/fnmatch.html
    for pattern in pattern_list:
        if fnmatch.fnmatch(role_pathname, pattern):
            # whitelisted
            return True

    # not whitelisted
    return False


# Form an evaluation as a dictionary. Suited to report on scheduled rules.  More info here:
#   https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/config.html#ConfigService.Client.put_evaluations
def build_evaluation(resource_id, compliance_type, notification_creation_time, resource_type=DEFAULT_RESOURCE_TYPE, annotation=None):
    evaluation = {}
    if annotation:
        evaluation['Annotation'] = annotation
    evaluation['ComplianceResourceType'] = resource_type
    evaluation['ComplianceResourceId'] = resource_id
    evaluation['ComplianceType'] = compliance_type
    evaluation['OrderingTimestamp'] = notification_creation_time
    return evaluation


# Determine if any roles were used to make an AWS request
def determine_last_used(role_name, role_last_used, max_age_in_days, notification_creation_time):

    last_used_date = role_last_used.get('LastUsedDate', None)
    used_region = role_last_used.get('Region', None)

    if not last_used_date:
        compliance_result = NON_COMPLIANT
        reason = "No record of usage"
        logger.info(f"NON_COMPLIANT: {role_name} has never been used")
        return build_evaluation(role_name, compliance_result, notification_creation_time, resource_type=DEFAULT_RESOURCE_TYPE, annotation=reason)


    days_unused = (datetime.datetime.now() - last_used_date.replace(tzinfo=None)).days

    if days_unused > max_age_in_days:
        compliance_result = NON_COMPLIANT
        reason = f"Was used {days_unused} days ago in {used_region}"
        logger.info(f"NON_COMPLIANT: {role_name} has not been used for {days_unused} days, last use in {used_region}")
        return build_evaluation(role_name, compliance_result, notification_creation_time, resource_type=DEFAULT_RESOURCE_TYPE, annotation=reason)

    compliance_result = COMPLIANT
    reason = f"Was used {days_unused} days ago in {used_region}"
    logger.info(f"COMPLIANT: {role_name} used {days_unused} days ago in {used_region}")
    return build_evaluation(role_name, compliance_result, notification_creation_time, resource_type=DEFAULT_RESOURCE_TYPE, annotation=reason)


# Returns a list of docts, each of which has authorization details of each role.  More info here:
#   https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/iam.html#IAM.Client.get_account_authorization_details
def get_role_authorization_details(iam_client):

    roles_authorization_details = []
    roles_list = iam_client.get_account_authorization_details(Filter=['Role'])

    while True:
        roles_authorization_details += roles_list['RoleDetailList']
        if 'Marker' in roles_list:
            roles_list = iam_client.get_account_authorization_details(Filter=['Role'], MaxItems=100, Marker=roles_list['Marker'])
        else:
            break

    return roles_authorization_details


# Check the compliance of each role by determining if role last used is > than max_days_for_last_used
def lambda_handler(event, context):

    # Initialize our AWS clients
    iam_client = get_client('iam', event["executionRoleArn"])
    config_client = get_client('config', event["executionRoleArn"])

    # List of resource evaluations to return back to AWS Config
    evaluations = []

    # List of dicts of each role's authorization details as returned by boto3
    all_roles = get_role_authorization_details(iam_client)

    # Timestamp of when AWS Config triggered this evaluation
    notification_creation_time = str(json.loads(event['invokingEvent'])['notificationCreationTime'])

    # ruleParameters is received from AWS Config's user-defined parameters
    rule_parameters = json.loads(event["ruleParameters"])

    # Maximum allowed days that a role can be unused, or has been last used for an AWS request
    max_days_for_last_used = int(os.environ.get('max_days_for_last_used', MAX_UNUSED_DAYS))
    if 'max_days_for_last_used' in rule_parameters:
        max_days_for_last_used = int(rule_parameters['max_days_for_last_used'])

    whitelisted_role_pattern_list = []
    if 'role_whitelist' in rule_parameters:
        whitelisted_role_pattern_list = validate_whitelist(rule_parameters['role_whitelist'])

    # Iterate over all our roles.  If the creation date of a role is <= max_days_for_last_used, it is compliant
    for role in all_roles:
        logger.info(f"WHATISTHIS:{role}, LASTUSED:{role.get('RoleLastUsed')}")
        role_name = role['RoleName']
        role_path = role['Path']
        role_creation_date = role['CreateDate']
        role_age_in_days = (datetime.datetime.now() - role_creation_date.replace(tzinfo=None)).days
        # For Roles created and never used "RoleLastUsed" will be None,
        # IT is same as the role was last used on the date it was created.
        if 'RoleLastUsed' in role:
            role_last_used = role.get('RoleLastUsed')
        else:
            role_last_used = {}
            role_last_used['LastUsedDate'] = role['CreateDate']
        

        if is_whitelisted_role(role_path + role_name, whitelisted_role_pattern_list):
            compliance_result = COMPLIANT
            reason = "Role is whitelisted"
            evaluations.append(
                build_evaluation(role_name, compliance_result, notification_creation_time, resource_type=DEFAULT_RESOURCE_TYPE, annotation=reason))
            logger.info(f"COMPLIANT: {role} is whitelisted")
            continue

        if role_age_in_days <= max_days_for_last_used:
            compliance_result = COMPLIANT
            reason = f"Role age is {role_age_in_days} days"
            evaluations.append(
                build_evaluation(role_name, compliance_result, notification_creation_time, resource_type=DEFAULT_RESOURCE_TYPE, annotation=reason))
            logger.info(f"COMPLIANT: {role_name} - {role_age_in_days} is newer or equal to {max_days_for_last_used} days")
            continue

        evaluation_result = determine_last_used(role_name, role_last_used, max_days_for_last_used, notification_creation_time)
        evaluations.append(evaluation_result)

    # Iterate over our evaluations 100 at a time, as put_evaluations only accepts a max of 100 evals.
    evaluations_copy = evaluations[:]
    while evaluations_copy:
        config_client.put_evaluations(Evaluations=evaluations_copy[:100], ResultToken=event['resultToken'])
        del evaluations_copy[:100]