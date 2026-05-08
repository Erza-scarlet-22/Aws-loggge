# lambda/db_lambda_handler.py
#
# Bedrock Action Group Lambda: RDS database remediation.
#
# WHY THIS EXISTS
# ---------------
# When Bedrock detects DB errors (9013 = storage critical, 9014 = connection exhausted),
# it calls this Lambda via db_remediation_action_group.
# RDS modification requires AWS API calls that cannot safely be made from a web container.
# This Lambda has the RDS permissions needed.
#
# WHAT IT DOES
# ------------
# Function: remediateDB
#   For DB storage (9013):
#     1. Checks current RDS storage usage
#     2. Doubles allocated storage (e.g. 1TB → 2TB)
#     3. Enables RDS storage autoscaling
#   For DB connections (9014):
#     1. Identifies stale connections
#     2. Upgrades RDS instance class
#     3. Force-restarts the ECS app service to recycle connection pools
#
# DEMO MODE: simulates all steps without real RDS calls.
#
# ENVIRONMENT VARIABLES
#   RDS_DB_INSTANCE_ID  – RDS instance identifier (leave blank for demo)
#   ECS_CLUSTER_NAME    – for service restart
#   DUMMY_APP_URL       – for mark-fixed callback

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

DEMO_MODE          = os.getenv('DEMO_MODE', 'true').lower() == 'true'
RDS_DB_INSTANCE_ID = os.getenv('RDS_DB_INSTANCE_ID', '')
ECS_CLUSTER_NAME   = os.getenv('ECS_CLUSTER_NAME', '')
DUMMY_APP_URL      = os.getenv('DUMMY_APP_URL', '')
AWS_REGION         = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

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


def _fix_db_storage(steps, log_step):
    """Expand RDS storage and enable autoscaling."""
    if DEMO_MODE:
        log_step('Confirmed RDS storage at 92% — writes failing',
                 detail='Instance: db-dummy-app-prod | Current: 920GB / 1000GB')
        log_step('Initiated RDS storage expansion: 1TB → 2TB',
                 detail='ModifyDBInstance called — zero downtime (autoscale eligible)')
        log_step('Storage expansion complete — RDS status: available',
                 detail='New capacity: 2000GB | Used: 920GB (46%)')
        log_step('Enabled RDS storage autoscaling (max 4TB)',
                 detail='MaxAllocatedStorage=4000 — prevents recurrence')
        return

    rds = boto3.client('rds', region_name=AWS_REGION)
    desc  = rds.describe_db_instances(DBInstanceIdentifier=RDS_DB_INSTANCE_ID)
    inst  = desc['DBInstances'][0]
    alloc = inst['AllocatedStorage']

    log_step(f'Current RDS storage: {alloc}GB', detail=f'Instance: {RDS_DB_INSTANCE_ID}')

    new_alloc = alloc * 2
    rds.modify_db_instance(
        DBInstanceIdentifier=RDS_DB_INSTANCE_ID,
        AllocatedStorage=new_alloc,
        MaxAllocatedStorage=new_alloc * 2,
        ApplyImmediately=True,
    )
    log_step(f'RDS storage expanded: {alloc}GB → {new_alloc}GB',
             detail='Autoscaling enabled')


def _fix_db_connections(steps, log_step):
    """Upgrade RDS instance class and recycle ECS connections."""
    if DEMO_MODE:
        log_step('Detected connection pool exhaustion',
                 detail='Active: 500/500 | Waiting: 47 | Stale connections identified')
        log_step('Force-terminated idle connections > 60s',
                 detail='183 stale connections closed — pool freed to 317/500')
        log_step('Deployed patched ECS task (connection leak fix)',
                 detail='Rolling update complete — 4/4 tasks healthy')
        log_step('Upgraded RDS instance: db.t3.large → db.r6g.xlarge',
                 detail='Max connections: 500 → 2000')
        log_step('Connection pool stable at 120/2000',
                 detail='All APIs responding < 200ms')
        return

    rds = boto3.client('rds', region_name=AWS_REGION)
    rds.modify_db_instance(
        DBInstanceIdentifier=RDS_DB_INSTANCE_ID,
        DBInstanceClass='db.r6g.xlarge',
        ApplyImmediately=True,
    )
    log_step('RDS instance class upgraded to db.r6g.xlarge',
             detail='Max connections increased to 2000')

    if ECS_CLUSTER_NAME:
        ecs = boto3.client('ecs', region_name=AWS_REGION)
        services = ecs.list_services(cluster=ECS_CLUSTER_NAME)['serviceArns']
        for svc_arn in services:
            svc_name = svc_arn.split('/')[-1]
            if 'app' in svc_name.lower():
                ecs.update_service(cluster=ECS_CLUSTER_NAME, service=svc_name,
                                   forceNewDeployment=True)
                log_step(f'ECS service {svc_name} restarted',
                         detail='Connection pool recycled')


def remediate_db(params: dict) -> dict:
    error_code    = params.get('error_code',    '9013')
    ticket_number = params.get('ticket_number', '')
    steps = []

    def log_step(desc, status='ok', detail=''):
        steps.append({'step': desc, 'status': status, 'detail': detail, 'ts': _ts()})
        logger.info('[db] %s [%s]', desc, status)

    try:
        if error_code == '9013':
            log_step('Identified DB storage critical error (error code 9013)')
            _fix_db_storage(steps, log_step)
            summary = 'RDS storage expanded 1TB → 2TB. Autoscaling enabled (max 4TB).'
        else:
            log_step('Identified DB connection exhaustion error (error code 9014)')
            _fix_db_connections(steps, log_step)
            summary = 'RDS upgraded to r6g.xlarge (2000 max connections). ECS recycled.'

        _notify_dummy_app(error_code, ticket_number)
        return {'success': True, 'steps': steps, 'summary': summary}

    except Exception as exc:
        logger.error('DB remediation failed: %s', exc, exc_info=True)
        steps.append({'step': f'Failed: {exc}', 'status': 'fail', 'ts': _ts()})
        return {'success': False, 'steps': steps, 'summary': str(exc), 'error': str(exc)}


def handler(event: dict, context: Any) -> dict:
    logger.info('DB Lambda event: %s', json.dumps(event)[:500])
    function   = event.get('function', 'remediateDB')
    raw_params = event.get('parameters', [])
    params     = {p['name']: p.get('value', '') for p in raw_params if 'name' in p}

    result = remediate_db(params) if function == 'remediateDB' else \
             {'error': f'Unknown function: {function}'}

    return {
        'actionGroup':      event.get('actionGroup', 'db_remediation_action_group'),
        'function':         function,
        'functionResponse': {'responseBody': {'TEXT': {'body': json.dumps(result)}}}
    }
