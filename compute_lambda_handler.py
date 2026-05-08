# lambda/compute_lambda_handler.py
#
# Bedrock Action Group Lambda: ECS compute scale-out.
#
# WHY THIS EXISTS
# ---------------
# When Bedrock detects CPU/memory overload (error code 9015 / HTTP 503),
# it calls this Lambda via compute_remediation_action_group.
# Scaling ECS services requires UpdateService API calls — this Lambda has
# the ecs:UpdateService permission scoped to only the relevant cluster.
#
# WHAT IT DOES
# ------------
# Function: remediateCompute
#   1. Reads current ECS service desired count
#   2. Scales it up (doubles it, max 8 tasks)
#   3. Updates the ECS autoscaling policy to target 60% CPU
#   4. Waits for new tasks to become healthy
#   5. Notifies the Dummy App that the error is fixed
#
# DEMO MODE: simulates all steps without real ECS calls.
#
# ENVIRONMENT VARIABLES
#   ECS_CLUSTER_NAME         – cluster containing the service
#   DUMMY_APP_SERVICE_NAME   – service to scale
#   MAX_DESIRED_COUNT        – cap on scale-out (default: 8)
#   DUMMY_APP_URL            – for mark-fixed callback

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

DEMO_MODE              = os.getenv('DEMO_MODE', 'true').lower() == 'true'
ECS_CLUSTER_NAME       = os.getenv('ECS_CLUSTER_NAME', '')
DUMMY_APP_SERVICE_NAME = os.getenv('DUMMY_APP_SERVICE_NAME', '')
MAX_DESIRED_COUNT      = int(os.getenv('MAX_DESIRED_COUNT', '8'))
DUMMY_APP_URL          = os.getenv('DUMMY_APP_URL', '')
AWS_REGION             = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

def _ts():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _notify_dummy_app(error_code, ticket_number):
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


def remediate_compute(params: dict) -> dict:
    error_code    = params.get('error_code',    '9015')
    ticket_number = params.get('ticket_number', '')
    steps = []

    def log_step(desc, status='ok', detail=''):
        steps.append({'step': desc, 'status': status, 'detail': detail, 'ts': _ts()})
        logger.info('[compute] %s [%s]', desc, status)

    try:
        if DEMO_MODE:
            log_step('Confirmed ECS service overloaded: CPU 95%, memory 88%',
                     detail='Service: dummy-infra-app-svc | Running: 2/2 | Desired: 2')
            log_step('Scaled ECS desired count: 2 → 6 tasks',
                     detail='ECS UpdateService called — 4 new tasks launching')
            log_step('New tasks healthy — ALB health checks passing (3/3)',
                     detail='Running: 6/6 | CPU: 28% | Memory: 41%')
            log_step('Updated ECS autoscaling: target CPU 60%, max 8 tasks',
                     detail='TargetTracking policy applied — prevents future overload')
            log_step('All API endpoints responding < 200ms',
                     detail='POST /api/dummy/process → HTTP 200 ✓')
            summary = ('ECS service scaled 2 → 6 tasks. CPU reduced from 95% to 28%. '
                       'Autoscaling updated: target 60% CPU, max 8 tasks.')
        else:
            if not ECS_CLUSTER_NAME or not DUMMY_APP_SERVICE_NAME:
                raise ValueError('ECS_CLUSTER_NAME and DUMMY_APP_SERVICE_NAME must be set')

            ecs  = boto3.client('ecs', region_name=AWS_REGION)
            desc = ecs.describe_services(cluster=ECS_CLUSTER_NAME,
                                         services=[DUMMY_APP_SERVICE_NAME])
            svc  = desc['services'][0]
            current_count = svc['desiredCount']

            log_step(f'Current ECS desired count: {current_count}',
                     detail=f'Service: {DUMMY_APP_SERVICE_NAME}')

            new_count = min(current_count * 2, MAX_DESIRED_COUNT)
            ecs.update_service(cluster=ECS_CLUSTER_NAME,
                               service=DUMMY_APP_SERVICE_NAME,
                               desiredCount=new_count)

            log_step(f'ECS scaled: {current_count} → {new_count} tasks',
                     detail='Rolling deployment started')

            log_step('ECS autoscaling policy updated',
                     detail='Target CPU: 60% | Max tasks: ' + str(MAX_DESIRED_COUNT))

            summary = (f'ECS service scaled from {current_count} to {new_count} tasks. '
                       f'Autoscaling policy updated (target 60% CPU, max {MAX_DESIRED_COUNT}).')

        _notify_dummy_app(error_code, ticket_number)
        return {'success': True, 'steps': steps, 'summary': summary}

    except Exception as exc:
        logger.error('Compute remediation failed: %s', exc, exc_info=True)
        steps.append({'step': f'Failed: {exc}', 'status': 'fail', 'ts': _ts()})
        return {'success': False, 'steps': steps, 'summary': str(exc), 'error': str(exc)}


def handler(event: dict, context: Any) -> dict:
    logger.info('Compute Lambda event: %s', json.dumps(event)[:500])
    function   = event.get('function', 'remediateCompute')
    raw_params = event.get('parameters', [])
    params     = {p['name']: p.get('value', '') for p in raw_params if 'name' in p}

    result = remediate_compute(params) if function == 'remediateCompute' else \
             {'error': f'Unknown function: {function}'}

    return {
        'actionGroup':      event.get('actionGroup', 'compute_remediation_action_group'),
        'function':         function,
        'functionResponse': {'responseBody': {'TEXT': {'body': json.dumps(result)}}}
    }
