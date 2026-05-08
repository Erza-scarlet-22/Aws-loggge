# DummyApp/ssl_remediation_agent.py
#
# Invokes the AWS Bedrock Agent to produce an SSL certificate remediation plan,
# then runs the actual (simulated) AWS fix steps.
#
# Works in two modes:
#   LIVE   — BEDROCK_AGENT_ID + BEDROCK_AGENT_ALIAS_ID are set → real Bedrock call
#   DEMO   — env vars missing or boto3 unavailable → rich scripted plan returned
#
# The demo mode is indistinguishable from a real response in the UI —
# it returns the same dict structure with realistic step-by-step output.

import logging
import os
import random
import time
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Bedrock invocation ────────────────────────────────────────────────────────

def _call_bedrock_agent(prompt: str, session_id: str = '') -> str:
    """
    Call the Bedrock agent with a structured SSL remediation prompt.
    Returns the agent's plain-text response, or raises RuntimeError.
    """
    try:
        import boto3
    except ImportError:
        raise RuntimeError('boto3 not installed')

    region         = os.environ.get('AWS_REGION', 'us-east-1')
    agent_id       = os.environ.get('BEDROCK_AGENT_ID', '').strip()
    agent_alias_id = os.environ.get('BEDROCK_AGENT_ALIAS_ID', '').strip()

    if not agent_id or not agent_alias_id:
        raise RuntimeError('BEDROCK_AGENT_ID / BEDROCK_AGENT_ALIAS_ID not configured')

    # ECS Fargate: IAM task role provides credentials automatically.
    # Local: boto3 uses ~/.aws/credentials or environment variables.
    client = boto3.client('bedrock-agent-runtime', region_name=region)
    sid    = (session_id or '').strip() or str(uuid4())

    logger.info('[ssl_remediation] Invoking Bedrock agent %s (session=%s)', agent_id, sid)

    response = client.invoke_agent(
        agentId       = agent_id,
        agentAliasId  = agent_alias_id,
        sessionId     = sid,
        inputText     = prompt,
    )

    parts = []
    for event in response.get('completion', []):
        chunk = event.get('chunk') if isinstance(event, dict) else None
        if chunk and chunk.get('bytes'):
            data = chunk['bytes']
            parts.append(data.decode('utf-8', errors='replace') if isinstance(data, bytes) else str(data))

    text = ''.join(parts).strip()
    return text or 'No response returned by Bedrock agent.'


def _ssl_fix_prompt(domain: str, cert_serial: str, error_code: str) -> str:
    return (
        'You are an AWS cloud engineer. An SSL certificate has expired and caused a service outage.\n\n'
        f'Domain       : {domain}\n'
        f'Error Code   : {error_code}\n'
        f'Cert Serial  : {cert_serial}\n'
        f'Detected at  : {datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}\n\n'
        'Provide a concise step-by-step remediation plan covering:\n'
        '1. What caused this (root cause)\n'
        '2. Immediate fix steps using AWS ACM, ALB, and Secrets Manager\n'
        '3. How to verify the fix\n'
        '4. How to prevent recurrence (auto-rotation)\n\n'
        'Be specific with AWS service names and API calls. Plain text only, no JSON.'
    )


# ── Demo fallback plan ────────────────────────────────────────────────────────

_DEMO_BEDROCK_PLAN = """\
ROOT CAUSE
The SSL/TLS certificate for {domain} reached its expiry date and was not \
auto-rotated. The ALB listener continued serving the expired certificate, \
causing all HTTPS clients to reject the connection with a certificate \
validation error (error code {error_code}).

IMMEDIATE FIX STEPS

Step 1 — Request a new certificate from AWS ACM
  aws acm request-certificate \\
    --domain-name {domain} \\
    --validation-method DNS \\
    --region {region}
  → ACM returns a new CertificateArn

Step 2 — Add DNS validation CNAME record
  ACM provides a CNAME name/value pair.
  Add it to your Route 53 hosted zone:
  aws route53 change-resource-record-sets \\
    --hosted-zone-id Z1234567890 \\
    --change-batch file://cname.json
  Wait ~2 minutes for ACM status → ISSUED

Step 3 — Attach new certificate to ALB HTTPS listener
  aws elbv2 modify-listener \\
    --listener-arn arn:aws:elasticloadbalancing:{region}:123456789012:listener/... \\
    --certificates CertificateArn=arn:aws:acm:{region}:123456789012:certificate/{cert_id}
  → Old expired cert is detached automatically

Step 4 — Store new cert ARN in Secrets Manager
  aws secretsmanager put-secret-value \\
    --secret-id /dummy-app/ssl/cert-arn \\
    --secret-string '{{"arn":"{cert_arn}","domain":"{domain}","expires":"2027-04-29"}}'

Step 5 — Verify TLS handshake
  curl -vI https://{domain}/api/dummy/status 2>&1 | grep -E "SSL|subject|expire"
  → Expected: TLS 1.3, certificate valid until 2027-04-29

PREVENTION — Enable ACM auto-renewal monitoring
  1. Set CloudWatch alarm on ACM certificate DaysToExpiry < 30
  2. Add EventBridge rule for ACM CertificateApproachingExpiry events
  3. Configure SNS notification to on-call channel
  This ensures future renewals are triggered before expiry.\
"""


def get_bedrock_ssl_plan(
    domain: str = 'api.dummy-app.internal',
    cert_serial: str = 'A1:B2:C3:D4:E5',
    error_code: str  = '9010',
    session_id: str  = '',
    region: str      = 'us-east-1',
) -> dict:
    """
    Returns:
        {
          'plan':    str,        # full remediation plan text
          'source':  'bedrock'|'demo',
          'session': str,
        }
    """
    cert_id  = f'{random.randint(10000,99999)}-aaaa-bbbb-cccc-demo'
    cert_arn = f'arn:aws:acm:{region}:123456789012:certificate/{cert_id}'

    try:
        prompt = _ssl_fix_prompt(domain, cert_serial, error_code)
        plan   = _call_bedrock_agent(prompt, session_id)
        source = 'bedrock'
        logger.info('[ssl_remediation] Bedrock response: %d chars', len(plan))
    except Exception as exc:
        logger.warning('[ssl_remediation] Bedrock unavailable (%s) — using demo plan', exc)
        plan = _DEMO_BEDROCK_PLAN.format(
            domain     = domain,
            error_code = error_code,
            region     = region,
            cert_id    = cert_id,
            cert_arn   = cert_arn,
        )
        source = 'demo'

    return {'plan': plan, 'source': source, 'session': session_id,
            'cert_arn': cert_arn, 'domain': domain}


# ── Full SSL fix execution ────────────────────────────────────────────────────

def run_ssl_expired_fix(log_path: str = '', session_id: str = '') -> dict:
    """
    Full fix pipeline for ssl_expired (error code 9010):
      1. Ask Bedrock agent for the remediation plan
      2. Execute each simulated AWS step with realistic timing
      3. Write RESOLVED line to log

    Returns the standard remediation dict:
        { success, steps, summary, new_state, bedrock_plan, bedrock_source }
    """
    domain      = 'api.dummy-app.internal'
    cert_serial = 'A1:B2:C3:D4:E5'
    error_code  = '9010'
    region      = os.environ.get('AWS_REGION', 'us-east-1')
    cert_id     = f'{random.randint(10000,99999)}-aaaa-bbbb-cccc-demo'
    cert_arn    = f'arn:aws:acm:{region}:123456789012:certificate/{cert_id}'

    # ── Phase 1: Get Bedrock plan ─────────────────────────────────────────────
    bedrock = get_bedrock_ssl_plan(
        domain      = domain,
        cert_serial = cert_serial,
        error_code  = error_code,
        session_id  = session_id,
        region      = region,
    )

    steps = []

    def step(description: str, status: str = 'ok', detail: str = '', delay: float = 0.3):
        time.sleep(delay)
        s = {
            'step':   description,
            'status': status,
            'detail': detail,
            'ts':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S'),
        }
        steps.append(s)
        logger.info('[ssl_remediation] %s [%s] %s', status.upper(), description, detail)
        return s

    # ── Phase 2: Execute fix steps ────────────────────────────────────────────
    step(
        'Detected expired SSL certificate',
        detail=f'Domain: {domain} | Serial: {cert_serial} | Expired 3 days ago',
        delay=0.2,
    )

    bedrock_label = '(Bedrock Agent)' if bedrock['source'] == 'bedrock' else '(Demo Plan)'
    step(
        f'Received remediation plan from AWS Bedrock {bedrock_label}',
        detail=f'Plan length: {len(bedrock["plan"])} chars | Session: {session_id or "new"}',
        delay=0.4,
    )

    step(
        'Requested new TLS certificate from AWS ACM',
        detail=f'aws acm request-certificate --domain-name {domain} --validation-method DNS',
        delay=0.5,
    )

    step(
        'DNS validation CNAME record added to Route 53',
        detail=f'_acme-challenge.{domain} → validated | Status: ISSUED',
        delay=0.8,
    )

    step(
        f'New cert ARN: {cert_arn}',
        detail='Certificate valid for 365 days (expires 2027-04-29)',
        delay=0.3,
    )

    step(
        'Attached new certificate to ALB HTTPS:443 listener',
        detail=f'aws elbv2 modify-listener --certificates CertificateArn={cert_arn[-40:]}',
        delay=0.6,
    )

    step(
        'Old expired certificate detached from ALB',
        detail='Previous cert (serial A1:B2:C3:D4:E5) removed from listener',
        delay=0.3,
    )

    step(
        'Stored new cert ARN in Secrets Manager',
        detail=f'Secret: /dummy-app/ssl/cert-arn | Version: AWSCURRENT updated',
        delay=0.4,
    )

    step(
        'Configured CloudWatch alarm: DaysToExpiry < 30',
        detail='EventBridge rule → SNS → on-call Slack channel (prevents recurrence)',
        delay=0.3,
    )

    step(
        f'Verified HTTPS handshake: curl -vI https://{domain}/api/dummy/status',
        detail='TLS 1.3 ✔ | HTTP 200 | Certificate valid until 2027-04-29',
        delay=0.5,
    )

    # ── Phase 3: Write RESOLVED line to application.log ───────────────────────
    if log_path:
        try:
            ts   = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
            line = (
                f'[{ts}] [INFO] RESOLVED: ssl_expired — '
                f'SSL certificate renewed for {domain}. '
                f'New cert ARN: {cert_arn[-40:]}. Valid 365 days.\n'
            )
            os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
            with open(log_path, 'a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception as exc:
            logger.warning('[ssl_remediation] Could not write RESOLVED log: %s', exc)

    summary = (
        f'SSL certificate for {domain} renewed via ACM. '
        f'New cert ARN: ...{cert_arn[-30:]}. '
        f'Valid 365 days. ALB updated. CloudWatch alarm configured.'
    )

    return {
        'success':        True,
        'steps':          steps,
        'summary':        summary,
        'new_state':      'resolved',
        'bedrock_plan':   bedrock['plan'],
        'bedrock_source': bedrock['source'],
    }
