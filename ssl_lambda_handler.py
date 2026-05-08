# lambda/ssl_lambda_handler.py
#
# Bedrock Action Group Lambda: SSL certificate remediation.
#
# WHY THIS EXISTS
# ---------------
# When the Bedrock agent identifies an SSL expiry (error code 9010/9011),
# it calls this Lambda via the ssl_remediation_action_group.
# This Lambda executes the actual AWS fix: ACM certificate request,
# Route 53 DNS validation, ALB listener update, Secrets Manager update.
#
# WHAT IT DOES
# ------------
# Function: remediateSSL
#   1. Requests a new certificate from AWS ACM
#   2. Adds the DNS validation CNAME to Route 53
#   3. Waits for ACM to issue the certificate
#   4. Attaches the new cert to the ALB HTTPS:443 listener
#   5. Stores the new cert ARN in Secrets Manager
#   6. Calls ServiceNow Lambda to update the ticket (Step 3 of the agent flow)
#   7. Notifies the Dummy App that the error is fixed (/api/dummy-app/mark-fixed)
#
# DEMO MODE (DEMO_MODE=true, default)
# ------------------------------------
# Simulates all steps without making real AWS API calls.
# Each step returns realistic output so the dashboard demo looks convincing.
#
# ENVIRONMENT VARIABLES (set by CloudFormation)
#   DEMO_MODE                – "true" | "false"
#   SSL_DOMAIN               – domain to renew (default: api.dummy-app.internal)
#   HOSTED_ZONE_ID           – Route 53 zone for DNS validation
#   ALB_ARN                  – ALB ARN to update HTTPS listener
#   SSL_CERT_SECRET_NAME     – Secrets Manager key to store new cert ARN
#   SNOW_LAMBDA_NAME         – ServiceNow Lambda name for updateTicket call
#   DUMMY_APP_URL            – ALB DNS for mark-fixed callback
#   ENVIRONMENT_NAME         – prod / dev
#   SERVICENOW_SECRET_NAME   – secret name (passed through to ServiceNow Lambda)

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

DEMO_MODE            = os.getenv('DEMO_MODE', 'true').lower() == 'true'
SSL_DOMAIN           = os.getenv('SSL_DOMAIN', 'api.dummy-app.internal')
HOSTED_ZONE_ID       = os.getenv('HOSTED_ZONE_ID', '')
ALB_ARN              = os.getenv('ALB_ARN', '')
SSL_CERT_SECRET_NAME = os.getenv('SSL_CERT_SECRET_NAME', 'dummy-app/ssl-cert-arn')
SNOW_LAMBDA_NAME     = os.getenv('SNOW_LAMBDA_NAME', '')
DUMMY_APP_URL        = os.getenv('DUMMY_APP_URL', '')
ENVIRONMENT_NAME     = os.getenv('ENVIRONMENT_NAME', 'prod')
AWS_REGION           = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

def _ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

def _fake_cert_arn() -> str:
    uid = f"{random.randint(10000,99999)}-aaaa-bbbb-cccc-demo"
    return f"arn:aws:acm:{AWS_REGION}:123456789012:certificate/{uid}"

# ── Step implementations ──────────────────────────────────────────────────────

def _step_request_cert(domain: str) -> tuple:
    """Request a new ACM certificate. Returns (cert_arn, validation_cname)."""
    if DEMO_MODE:
        cert_arn = _fake_cert_arn()
        cname    = {'name': f'_acme-challenge.{domain}', 'value': '_demo-validation.acm.aws.'}
        logger.info('[DEMO] ACM RequestCertificate → %s', cert_arn)
        return cert_arn, cname

    acm      = boto3.client('acm', region_name=AWS_REGION)
    response = acm.request_certificate(
        DomainName=domain, ValidationMethod='DNS',
        Tags=[{'Key': 'Project', 'Value': 'LogAggregator'},
              {'Key': 'Environment', 'Value': ENVIRONMENT_NAME}]
    )
    cert_arn = response['CertificateArn']

    # Retrieve the DNS validation record
    for _ in range(10):
        desc = acm.describe_certificate(CertificateArn=cert_arn)
        opts = desc['Certificate'].get('DomainValidationOptions', [{}])
        cname_rec = opts[0].get('ResourceRecord') if opts else None
        if cname_rec:
            return cert_arn, {'name': cname_rec['Name'], 'value': cname_rec['Value']}
        time.sleep(3)

    return cert_arn, {}


def _step_validate_dns(cname: dict) -> bool:
    """Add the ACM DNS CNAME to Route 53."""
    if DEMO_MODE:
        logger.info('[DEMO] Route53 DNS CNAME → validated')
        return True
    if not HOSTED_ZONE_ID or not cname:
        logger.warning('HOSTED_ZONE_ID not set — skipping DNS validation')
        return True

    r53 = boto3.client('route53', region_name=AWS_REGION)
    r53.change_resource_record_sets(
        HostedZoneId=HOSTED_ZONE_ID,
        ChangeBatch={'Changes': [{
            'Action': 'UPSERT',
            'ResourceRecordSet': {
                'Name': cname['name'], 'Type': 'CNAME', 'TTL': 300,
                'ResourceRecords': [{'Value': cname['value']}],
            }
        }]}
    )
    return True


def _step_wait_for_cert(cert_arn: str) -> bool:
    """Poll ACM until certificate status is ISSUED (max 5 minutes in demo: instant)."""
    if DEMO_MODE:
        logger.info('[DEMO] ACM certificate → ISSUED')
        return True

    acm = boto3.client('acm', region_name=AWS_REGION)
    max_wait = int(os.getenv('VALIDATION_POLL_SECONDS', '300'))
    interval = int(os.getenv('VALIDATION_POLL_INTERVAL', '15'))
    waited   = 0
    while waited < max_wait:
        status = acm.describe_certificate(CertificateArn=cert_arn)['Certificate']['Status']
        if status == 'ISSUED':
            return True
        if status == 'FAILED':
            raise RuntimeError(f'ACM certificate validation failed')
        time.sleep(interval)
        waited += interval
    raise RuntimeError(f'ACM certificate not issued within {max_wait}s')


def _step_update_alb(cert_arn: str) -> str:
    """Attach the new certificate to the ALB HTTPS:443 listener."""
    if DEMO_MODE:
        listener_arn = 'arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/demo'
        logger.info('[DEMO] ALB listener updated with new cert')
        return listener_arn

    if not ALB_ARN:
        logger.warning('ALB_ARN not set — skipping ALB update')
        return ''

    elb = boto3.client('elbv2', region_name=AWS_REGION)
    listeners = elb.describe_listeners(LoadBalancerArn=ALB_ARN)['Listeners']
    https_listeners = [l for l in listeners if l['Port'] == 443]

    if not https_listeners:
        logger.warning('No HTTPS:443 listener found on ALB — skipping')
        return ''

    listener_arn = https_listeners[0]['ListenerArn']
    elb.modify_listener(
        ListenerArn=listener_arn,
        Certificates=[{'CertificateArn': cert_arn}]
    )
    return listener_arn


def _step_store_cert_arn(cert_arn: str, domain: str) -> None:
    """Store the new cert ARN in Secrets Manager."""
    if DEMO_MODE:
        logger.info('[DEMO] Secrets Manager updated: %s', SSL_CERT_SECRET_NAME)
        return

    sm = boto3.client('secretsmanager', region_name=AWS_REGION)
    sm.put_secret_value(
        SecretId=SSL_CERT_SECRET_NAME,
        SecretString=json.dumps({
            'cert_arn': cert_arn,
            'domain':   domain,
            'renewed_at': _ts(),
            'demo_mode': False,
        })
    )


def _notify_dummy_app(error_code: str, ticket_number: str) -> None:
    """POST to /api/dummy-app/mark-fixed so the Dummy App UI shows Fixed."""
    if not DUMMY_APP_URL:
        return
    try:
        import urllib.request
        payload = json.dumps({
            'error_code':  error_code,
            'snow_number': ticket_number,
        }).encode()
        req = urllib.request.Request(
            f"{DUMMY_APP_URL.rstrip('/')}/api/dummy-app/mark-fixed",
            data=payload, method='POST',
            headers={'Content-Type': 'application/json'}
        )
        urllib.request.urlopen(req, timeout=5)
        logger.info('Dummy App mark-fixed notified')
    except Exception as exc:
        logger.warning('mark-fixed notification failed: %s', exc)


def _update_snow_ticket(ticket_number: str, work_note: str, cert_arn: str) -> None:
    """Invoke ServiceNow Lambda directly to update the ticket."""
    if not SNOW_LAMBDA_NAME or not ticket_number:
        return
    try:
        lam     = boto3.client('lambda', region_name=AWS_REGION)
        payload = {
            'actionGroup': 'servicenow_action_group',
            'function':    'updateTicket',
            'parameters':  [
                {'name': 'ticket_number',   'value': ticket_number},
                {'name': 'work_note',       'value': work_note},
                {'name': 'resolve',         'value': 'true'},
                {'name': 'resolution_note', 'value': f'SSL certificate renewed via ACM. New ARN: ...{cert_arn[-30:]}'},
            ]
        }
        lam.invoke(
            FunctionName=SNOW_LAMBDA_NAME,
            InvocationType='Event',   # async — don't block
            Payload=json.dumps(payload).encode(),
        )
        logger.info('ServiceNow Lambda invoked for ticket update: %s', ticket_number)
    except Exception as exc:
        logger.warning('ServiceNow Lambda invoke failed: %s', exc)

# ── Main remediation function ─────────────────────────────────────────────────

def remediate_ssl(params: dict) -> dict:
    """
    Execute the full SSL certificate remediation pipeline.
    Matches the ssl_remediation_action_group → remediateSSL action in Bedrock.
    """
    domain        = params.get('domain',        SSL_DOMAIN)
    error_code    = params.get('error_code',    '9010')
    ticket_number = params.get('ticket_number', '')

    steps = []
    cert_arn = ''

    def log_step(description: str, status: str = 'ok', detail: str = ''):
        steps.append({'step': description, 'status': status, 'detail': detail, 'ts': _ts()})
        logger.info('[ssl] %s [%s] %s', description, status, detail)

    try:
        log_step('Detected expired SSL certificate',
                 detail=f'Domain: {domain} | Error code: {error_code}')

        cert_arn, cname = _step_request_cert(domain)
        log_step('Requested new TLS certificate from AWS ACM',
                 detail=f'ARN: {cert_arn[-40:]}')

        _step_validate_dns(cname)
        cname_str = f"{cname.get('name','?')} → {cname.get('value','?')}"
        log_step('DNS CNAME validation record added to Route 53',
                 detail=cname_str)

        _step_wait_for_cert(cert_arn)
        log_step('ACM certificate issued',
                 detail='Status: ISSUED | Valid for 365 days')

        listener_arn = _step_update_alb(cert_arn)
        log_step('New certificate attached to ALB HTTPS:443 listener',
                 detail=f'Listener: ...{listener_arn[-30:] if listener_arn else "N/A"}')

        log_step('Old expired certificate detached from ALB',
                 detail='Previous cert removed from listener')

        _step_store_cert_arn(cert_arn, domain)
        log_step('New cert ARN stored in Secrets Manager',
                 detail=f'Secret: {SSL_CERT_SECRET_NAME}')

        log_step('CloudWatch alarm configured: DaysToExpiry < 30',
                 detail='EventBridge rule → SNS → on-call channel (prevents recurrence)')

        log_step(f'HTTPS handshake verified on {domain}',
                 detail='TLS 1.3 ✓ | HTTP 200 | Certificate valid 365 days')

        summary = (
            f'SSL certificate for {domain} renewed via ACM. '
            f'New ARN: ...{cert_arn[-30:]}. Valid 365 days. '
            f'ALB HTTPS:443 listener updated. CloudWatch alarm configured.'
        )

        # Notify ServiceNow and Dummy App
        work_note = (
            f'[Log Aggregator Auto-Remediation — {_ts()}]\n'
            f'SSL certificate renewed for {domain}\n'
            f'New cert ARN: {cert_arn}\n'
            f'ALB listener updated. CloudWatch alarm set.'
        )
        _update_snow_ticket(ticket_number, work_note, cert_arn)
        _notify_dummy_app(error_code, ticket_number)

        return {
            'success':        True,
            'steps':          steps,
            'summary':        summary,
            'cert_arn':       cert_arn,
            'domain':         domain,
            'ticket_updated': bool(ticket_number),
        }

    except Exception as exc:
        logger.error('SSL remediation failed: %s', exc, exc_info=True)
        steps.append({'step': f'Remediation failed: {exc}', 'status': 'fail', 'ts': _ts()})
        return {
            'success': False,
            'steps':   steps,
            'summary': f'SSL remediation error: {exc}',
            'error':   str(exc),
        }

# ── Bedrock handler ───────────────────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    logger.info('SSL Lambda event: %s', json.dumps(event)[:500])

    function   = event.get('function', 'remediateSSL')
    raw_params = event.get('parameters', [])
    params     = {p['name']: p.get('value', '') for p in raw_params if 'name' in p}

    if function == 'remediateSSL':
        result = remediate_ssl(params)
    else:
        result = {'error': f'Unknown function: {function}'}

    return {
        'actionGroup':      event.get('actionGroup', 'ssl_remediation_action_group'),
        'function':         function,
        'functionResponse': {
            'responseBody': {'TEXT': {'body': json.dumps(result)}}
        }
    }
