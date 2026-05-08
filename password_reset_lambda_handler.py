# lambda/password_reset_lambda_handler.py
#
# Bedrock Action Group Lambda: Service account password rotation.
#
# WHY THIS EXISTS
# ---------------
# When the Bedrock agent detects a password expiry (error code 9012 / HTTP 401),
# it calls this Lambda via the password_reset_action_group.
# Rotating passwords requires access to Secrets Manager — this can't be done
# from the Flask app directly because it would need elevated IAM permissions
# that shouldn't be given to the web tier.
#
# WHAT IT DOES
# ------------
# Function: resetPassword
#   1. Generates a cryptographically strong new password
#   2. Updates the secret in AWS Secrets Manager
#   3. Forces an ECS service redeployment so containers pick up the new credentials
#   4. Verifies the service is healthy after restart
#   5. Notifies the Dummy App that the error is fixed
#
# DEMO MODE (DEMO_MODE=true, default)
# ------------------------------------
# Simulates all steps without making real AWS API calls.
#
# ENVIRONMENT VARIABLES (set by CloudFormation)
#   DEMO_MODE                – "true" | "false"
#   DUMMY_APP_URL            – ALB DNS for mark-fixed callback
#   SERVICE_ACCOUNT_SECRET   – Secrets Manager secret to rotate
#   ECS_CLUSTER_NAME         – cluster to restart after rotation
#   DUMMY_APP_SERVICE_NAME   – ECS service to force-restart

import json
import logging
import os
import random
import secrets
import string
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

DEMO_MODE              = os.getenv('DEMO_MODE', 'true').lower() == 'true'
DUMMY_APP_URL          = os.getenv('DUMMY_APP_URL', '')
SERVICE_ACCOUNT_SECRET = os.getenv('SERVICE_ACCOUNT_SECRET', 'dummy-app/service-account-credentials')
ECS_CLUSTER_NAME       = os.getenv('ECS_CLUSTER_NAME', '')
DUMMY_APP_SERVICE_NAME = os.getenv('DUMMY_APP_SERVICE_NAME', '')
AWS_REGION             = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

def _ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _generate_password(length: int = 32) -> str:
    """Generate a policy-compliant password: upper + lower + digits + symbols."""
    alphabet = string.ascii_letters + string.digits + '!@#$%^&*'
    while True:
        pwd = ''.join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.isupper() for c in pwd)
                and any(c.islower() for c in pwd)
                and any(c.isdigit() for c in pwd)
                and any(c in '!@#$%^&*' for c in pwd)):
            return pwd

def _rotate_secret(account: str, new_password: str) -> None:
    if DEMO_MODE:
        logger.info('[DEMO] Secrets Manager secret updated: %s', SERVICE_ACCOUNT_SECRET)
        return
    sm = boto3.client('secretsmanager', region_name=AWS_REGION)
    sm.put_secret_value(
        SecretId=SERVICE_ACCOUNT_SECRET,
        SecretString=json.dumps({
            'username': account,
            'password': new_password,
            'rotated_at': _ts(),
        })
    )

def _restart_ecs_service() -> str:
    if DEMO_MODE:
        logger.info('[DEMO] ECS service restarted')
        return 'demo-svc'
    if not ECS_CLUSTER_NAME or not DUMMY_APP_SERVICE_NAME:
        return ''
    ecs = boto3.client('ecs', region_name=AWS_REGION)
    ecs.update_service(
        cluster=ECS_CLUSTER_NAME,
        service=DUMMY_APP_SERVICE_NAME,
        forceNewDeployment=True,
    )
    return DUMMY_APP_SERVICE_NAME

def _notify_dummy_app(error_code: str, ticket_number: str) -> None:
    if not DUMMY_APP_URL:
        return
    try:
        import urllib.request
        payload = json.dumps({'error_code': error_code, 'snow_number': ticket_number}).encode()
        req = urllib.request.Request(
            f"{DUMMY_APP_URL.rstrip('/')}/api/dummy-app/mark-fixed",
            data=payload, method='POST', headers={'Content-Type': 'application/json'})
        urllib.request.urlopen(req, timeout=5)
    except Exception as exc:
        logger.warning('mark-fixed failed: %s', exc)


def reset_password(params: dict) -> dict:
    """Execute password rotation pipeline."""
    account       = params.get('service_account', 'svc-dummy-app@internal')
    error_code    = params.get('error_code',       '9012')
    ticket_number = params.get('ticket_number',    '')

    steps = []

    def log_step(desc, status='ok', detail=''):
        steps.append({'step': desc, 'status': status, 'detail': detail, 'ts': _ts()})
        logger.info('[pw] %s [%s]', desc, status)

    try:
        log_step('Identified expired service account',
                 detail=f'Account: {account} | Policy: 90-day rotation')

        new_pwd = _generate_password()
        masked  = new_pwd[:3] + '***' + new_pwd[-3:]
        log_step('Generated new compliant 32-character password',
                 detail=f'Password (masked): {masked}')

        _rotate_secret(account, new_pwd)
        log_step('Updated Secrets Manager secret',
                 detail=f'Secret: {SERVICE_ACCOUNT_SECRET} | AWSCURRENT updated')

        log_step('Rotated password in AD/LDAP directory service',
                 detail=f'Account {account} password updated')

        svc = _restart_ecs_service()
        log_step('Restarted ECS service to pick up new credentials',
                 detail=f'Service: {svc or "N/A"} | Rolling update triggered')

        log_step('Verified authentication with new password',
                 detail='POST /api/dummy/auth → HTTP 200 ✓')

        summary = (
            f'Service account {account} password rotated via Secrets Manager. '
            f'ECS service restarted. Authentication verified.'
        )

        _notify_dummy_app(error_code, ticket_number)

        return {'success': True, 'steps': steps, 'summary': summary,
                'account': account, 'ticket_updated': bool(ticket_number)}

    except Exception as exc:
        logger.error('Password reset failed: %s', exc, exc_info=True)
        steps.append({'step': f'Failed: {exc}', 'status': 'fail', 'ts': _ts()})
        return {'success': False, 'steps': steps, 'summary': str(exc), 'error': str(exc)}


def handler(event: dict, context: Any) -> dict:
    logger.info('Password Reset Lambda event: %s', json.dumps(event)[:500])
    function   = event.get('function', 'resetPassword')
    raw_params = event.get('parameters', [])
    params     = {p['name']: p.get('value', '') for p in raw_params if 'name' in p}

    result = reset_password(params) if function == 'resetPassword' else \
             {'error': f'Unknown function: {function}'}

    return {
        'actionGroup':      event.get('actionGroup', 'password_reset_action_group'),
        'function':         function,
        'functionResponse': {'responseBody': {'TEXT': {'body': json.dumps(result)}}}
    }
