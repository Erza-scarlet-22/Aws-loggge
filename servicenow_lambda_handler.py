# lambda/servicenow_lambda_handler.py
#
# Bedrock Action Group Lambda: ServiceNow ticket management.
#
# WHY THIS EXISTS
# ---------------
# The Bedrock orchestrator agent cannot call ServiceNow directly.
# This Lambda acts as the bridge — Bedrock invokes it via an action group
# and it creates/updates/queries incidents in ServiceNow on the agent's behalf.
#
# WHAT IT DOES
# ------------
# Exposes 3 functions to Bedrock (matching your orchestrator_agent_prompt.txt):
#   createIncident    → POST   /api/now/table/incident
#   getTicketStatus   → GET    /api/now/table/incident/<sys_id>
#   updateTicket      → PATCH  /api/now/table/incident/<sys_id>
#
# DEMO MODE (DEMO_MODE=true, default)
# ------------------------------------
# Returns realistic fake ticket numbers without calling ServiceNow.
# Set DEMO_MODE=false and populate the ServiceNow secret for live operation.
#
# ENVIRONMENT VARIABLES (set by CloudFormation)
#   SERVICENOW_SECRET_NAME  – Secrets Manager secret name (default: servicenow/credential)
#   DEMO_MODE               – "true" | "false"
#   AWS_DEFAULT_REGION      – region for boto3

import json
import logging
import os
import random
import string
from datetime import datetime, timezone
from typing import Any, Dict

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

SERVICENOW_SECRET_NAME = os.getenv('SERVICENOW_SECRET_NAME', 'servicenow/credential')
DEMO_MODE              = os.getenv('DEMO_MODE', 'true').lower() == 'true'
AWS_REGION             = os.getenv('AWS_DEFAULT_REGION', 'us-east-1')

# ── ServiceNow credential loader ──────────────────────────────────────────────

_snow_creds: Dict[str, str] = {}

def _load_snow_creds() -> Dict[str, str]:
    global _snow_creds
    if _snow_creds:
        return _snow_creds
    try:
        import boto3
        sm  = boto3.client('secretsmanager', region_name=AWS_REGION)
        val = sm.get_secret_value(SecretId=SERVICENOW_SECRET_NAME)
        _snow_creds = json.loads(val['SecretString'])
        logger.info('ServiceNow credentials loaded from Secrets Manager')
    except Exception as exc:
        logger.warning('Could not load ServiceNow creds: %s — using demo mode', exc)
    return _snow_creds


def _snow_request(method: str, path: str, body: dict = None) -> dict:
    """Make an authenticated REST call to ServiceNow."""
    import urllib.request, base64
    creds = _load_snow_creds()
    base_url = creds.get('instance_url', '').rstrip('/')
    username  = creds.get('username', '')
    password  = creds.get('password', '')

    url     = f"{base_url}{path}"
    payload = json.dumps(body or {}).encode('utf-8')
    token   = base64.b64encode(f"{username}:{password}".encode()).decode()

    req = urllib.request.Request(
        url, data=payload if method != 'GET' else None, method=method,
        headers={
            'Authorization':  f'Basic {token}',
            'Content-Type':   'application/json',
            'Accept':         'application/json',
        }
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

# ── Demo helpers ──────────────────────────────────────────────────────────────

def _demo_inc_number() -> str:
    return 'INC' + ''.join(random.choices(string.digits, k=7))

def _ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# ── Action implementations ────────────────────────────────────────────────────

def create_incident(params: dict) -> dict:
    """Create a ServiceNow incident. Called by Bedrock as Step 1 of every fix."""
    error_type  = params.get('error_type', 'unknown')
    description = params.get('error_description', 'Error detected by Log Aggregator')
    status_code = params.get('status_code', '500')
    count       = params.get('count', 1)
    last_seen   = params.get('last_seen', _ts())

    short_desc = f"[Log Aggregator] {error_type}: {description[:80]}"

    if DEMO_MODE:
        ticket_number = _demo_inc_number()
        logger.info('[DEMO] createIncident → %s', ticket_number)
        return {
            'ticket_number': ticket_number,
            'ticket_url':    f"https://demo.service-now.com/incident?sysparm_query=number={ticket_number}",
            'urgency':       '1' if int(str(status_code).split()[0]) >= 500 else '2',
            'category':      'software',
            'status':        'demo_mode',
            'created_at':    _ts(),
        }

    try:
        body = {
            'short_description': short_desc,
            'description': (
                f"Error type    : {error_type}\n"
                f"HTTP status   : {status_code}\n"
                f"Occurrences   : {count}\n"
                f"Last seen     : {last_seen}\n"
                f"Source        : Log Aggregator auto-remediation"
            ),
            'category':  'software',
            'urgency':   '1' if int(str(status_code).split()[0]) >= 500 else '2',
            'impact':    '2',
            'state':     '1',
        }
        result = _snow_request('POST', '/api/now/table/incident', body)['result']
        return {
            'ticket_number': result['number'],
            'ticket_url':    f"{_load_snow_creds()['instance_url']}/incident?sysparm_query=number={result['number']}",
            'sys_id':        result['sys_id'],
            'urgency':       result.get('urgency', '2'),
            'category':      result.get('category', 'software'),
            'created_at':    _ts(),
        }
    except Exception as exc:
        logger.error('createIncident failed: %s', exc)
        return {'ticket_number': _demo_inc_number(), 'status': 'fallback_demo', 'error': str(exc)}


def get_ticket_status(params: dict) -> dict:
    """Check current state of a ServiceNow incident."""
    ticket_number = params.get('ticket_number', '')

    if DEMO_MODE:
        return {
            'ticket_number': ticket_number,
            'state_label':   'In Progress',
            'priority':      '2 - High',
            'assigned_to':   'aws-log-aggregator',
            'status':        'demo_mode',
        }

    try:
        path   = f"/api/now/table/incident?sysparm_query=number={ticket_number}&sysparm_fields=number,state,priority,assigned_to"
        result = _snow_request('GET', path)['result']
        item   = result[0] if result else {}
        state_map = {'1':'New','2':'In Progress','3':'On Hold','6':'Resolved','7':'Closed'}
        return {
            'ticket_number': ticket_number,
            'state_label':   state_map.get(str(item.get('state','')), 'Unknown'),
            'priority':      item.get('priority', ''),
            'assigned_to':   item.get('assigned_to', {}).get('display_value', ''),
        }
    except Exception as exc:
        return {'ticket_number': ticket_number, 'error': str(exc)}


def update_ticket(params: dict) -> dict:
    """Add work notes or resolve a ServiceNow incident."""
    ticket_number  = params.get('ticket_number', '')
    work_note      = params.get('work_note', '')
    resolve        = str(params.get('resolve', 'false')).lower() == 'true'
    resolution_note = params.get('resolution_note', '')

    if DEMO_MODE:
        logger.info('[DEMO] updateTicket %s resolve=%s', ticket_number, resolve)
        return {'updated': True, 'ticket_number': ticket_number, 'status': 'demo_mode'}

    try:
        # Get sys_id from ticket number
        path   = f"/api/now/table/incident?sysparm_query=number={ticket_number}&sysparm_fields=sys_id,number"
        result = _snow_request('GET', path)['result']
        if not result:
            return {'updated': False, 'error': f'Ticket {ticket_number} not found'}
        sys_id = result[0]['sys_id']

        # Write work note first (always succeeds for itil users)
        if work_note:
            _snow_request('PATCH', f'/api/now/table/incident/{sys_id}',
                          {'work_notes': work_note})

        # State change (may fail if user lacks incident_manager role)
        if resolve:
            try:
                _snow_request('PATCH', f'/api/now/table/incident/{sys_id}', {
                    'state':       '6',
                    'close_notes': resolution_note or work_note,
                })
            except Exception as state_exc:
                logger.warning('State change skipped (403 likely): %s', state_exc)

        return {'updated': True, 'ticket_number': ticket_number}
    except Exception as exc:
        logger.error('updateTicket failed: %s', exc)
        return {'updated': False, 'error': str(exc)}

# ── Bedrock action group handler ──────────────────────────────────────────────

def handler(event: dict, context: Any) -> dict:
    """
    Bedrock action group invocation format:
        event = {
            "actionGroup": "servicenow_action_group",
            "function":    "createIncident",
            "parameters":  [{"name": "error_type", "value": "ssl_expired"}, ...]
        }
    """
    logger.info('ServiceNow Lambda event: %s', json.dumps(event)[:500])

    function   = event.get('function', '')
    raw_params = event.get('parameters', [])

    # Normalise Bedrock parameter format [{"name":..,"value":..}] → dict
    params: dict = {}
    for p in raw_params:
        if isinstance(p, dict) and 'name' in p:
            params[p['name']] = p.get('value', '')

    dispatch = {
        'createIncident':   create_incident,
        'getTicketStatus':  get_ticket_status,
        'updateTicket':     update_ticket,
    }

    if function not in dispatch:
        result = {'error': f"Unknown function: {function}",
                  'available': list(dispatch.keys())}
    else:
        result = dispatch[function](params)

    logger.info('ServiceNow Lambda result: %s', json.dumps(result)[:500])

    return {
        'actionGroup':        event.get('actionGroup', 'servicenow_action_group'),
        'function':           function,
        'functionResponse':   {
            'responseBody': {
                'TEXT': {'body': json.dumps(result)}
            }
        }
    }
