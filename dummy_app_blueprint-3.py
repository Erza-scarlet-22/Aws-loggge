# DummyApp/dummy_app_blueprint.py
#
# Flask blueprint for the Toil Management dummy application.
#
# Routes:
#   GET  /dummy-app                         — HTML control panel
#   POST /api/dummy-app/trigger-error       — Trigger a named error + auto-ship
#   POST /api/dummy-app/trigger-resolution  — Mark a named error resolved
#   POST /api/dummy-app/generate            — Write N random events
#   POST /api/dummy-app/ship                — Force conversion immediately
#   GET  /api/dummy-app/logs                — Last N relevant log lines
#   GET  /api/dummy-app/stats               — Error/success counts
#   GET  /api/dummy-app/scenario-states     — Current state of all named scenarios
#   POST /api/dummy-app/mark-fixed          — Called by dashboard fix pipeline to sync fix status

import logging
import os
import random
import threading
from datetime import datetime, timezone
from typing import Dict

from flask import Blueprint, jsonify, render_template, request

_log = logging.getLogger(__name__)

SOURCE_TAG = 'dummy_app'

# ── Named scenario metadata (mirrors ErrorSimulator.ERROR_DEFINITIONS) ─────────
NAMED_SCENARIOS: Dict[str, dict] = {
    'ssl_expired': {
        'label':    'SSL Certificate Expired',
        'desc':     'SSL certificate expired for domain api.dummy-app.internal',
        'http':     495,
        'code':     9010,
        'api':      'GET /api/dummy/status',
        'severity': 'high',
        'fix_hint': 'Renew certificate via ACM and update ALB listener',
    },
    'ssl_expiring': {
        'label':    'SSL Certificate Expiring Soon (7 days)',
        'desc':     'SSL certificate expires in 7 days — proactive renewal required',
        'http':     495,
        'code':     9011,
        'api':      'GET /api/dummy/status',
        'severity': 'medium',
        'fix_hint': 'Rotate certificate proactively via ACM before it expires',
    },
    'password_expired': {
        'label':    'Service Account Password Expired',
        'desc':     'Service account password expired, authentication failed',
        'http':     401,
        'code':     9012,
        'api':      'POST /api/dummy/auth',
        'severity': 'high',
        'fix_hint': 'Rotate password in Secrets Manager and restart ECS task',
    },
    'db_storage': {
        'label':    'DB Storage Critical (92%)',
        'desc':     'Database storage at 92% capacity, writes may fail',
        'http':     507,
        'code':     9013,
        'api':      'POST /api/dummy/db-write',
        'severity': 'high',
        'fix_hint': 'Increase RDS allocated storage and enable autoscaling',
    },
    'db_connection': {
        'label':    'DB Connection Pool Exhausted',
        'desc':     'RDS connection pool exhausted, timeout after 30s',
        'http':     504,
        'code':     9014,
        'api':      'GET /api/dummy/db-read',
        'severity': 'critical',
        'fix_hint': 'Kill stale connections, redeploy ECS task, upgrade RDS instance',
    },
    'compute_overload': {
        'label':    'Compute Overload (CPU 95%)',
        'desc':     'CPU at 95%, memory at 88%, dropping requests',
        'http':     503,
        'code':     9015,
        'api':      'POST /api/dummy/process',
        'severity': 'critical',
        'fix_hint': 'Scale out ECS desired count and update autoscaling policy',
    },
}

# ── 2000-series random scenarios ───────────────────────────────────────────────
_ERROR_SCENARIOS = [
    (503, 2001, 'Toilet sensor gateway did not respond within SLA threshold',      'GET /api/dummy_app/sensors'),
    (500, 2002, 'Database connection pool exhausted — all connections in use',      'POST /api/dummy_app/flush'),
    (404, 2003, 'Toilet unit not found in registry',                                'GET /api/dummy_app/unit/{id}'),
    (422, 2004, 'Maintenance schedule payload missing required field: unit_id',     'POST /api/dummy_app/maintenance'),
    (401, 2005, 'Authentication token expired for maintenance crew dashboard',      'POST /api/dummy_app/auth'),
    (409, 2006, 'Flush command rejected — unit TLT-009 is already in flush cycle',  'POST /api/dummy_app/flush'),
    (502, 2007, 'Received malformed response from downstream sensor aggregator',    'GET /api/dummy_app/sensors'),
    (504, 2008, 'Upstream alert engine timed out after 3000 ms',                   'GET /api/dummy_app/alerts'),
    (429, 2009, 'Rate limit exceeded for sensor heartbeat endpoint',               'POST /api/dummy_app/heartbeat'),
    (500, 2010, 'NullPointerException in pressure reading parser',                  'GET /api/dummy_app/sensors'),
]

_SUCCESS_SCENARIOS = [
    (200, 'GET /api/dummy_app/status'),
    (200, 'GET /api/dummy_app/sensors'),
    (201, 'POST /api/dummy_app/maintenance'),
    (200, 'GET /api/dummy_app/alerts'),
    (200, 'POST /api/dummy_app/heartbeat'),
    (200, 'GET /api/dummy_app/unit/{id}'),
]

# ── Server-side scenario state store ──────────────────────────────────────────
# Persists across requests for the lifetime of the Flask process.
# state: 'idle' | 'triggered' | 'resolved' | 'fixed'
_scenario_states: Dict[str, dict] = {
    t: {
        'state':          'idle',
        'triggered_at':   None,
        'resolved_at':    None,
        'fixed_at':       None,
        'trigger_count':  0,
        'http_status':    m['http'],
        'error_code':     m['code'],
        'snow_ticket':    None,   # filled by mark-fixed
    }
    for t, m in NAMED_SCENARIOS.items()
}

_state_lock = threading.Lock()
_write_lock = threading.Lock()


def _fake_ip() -> str:
    return f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


# AWS mode config — read dynamically so ECS env vars are always picked up
def _get_raw_bucket():  return os.environ.get('RAW_LOGS_BUCKET', '')
def _get_raw_prefix():  return os.environ.get('RAW_LOGS_PREFIX', 'raw-logs/')
def _get_aws_region():  return os.environ.get('AWS_DEFAULT_REGION',
                               os.environ.get('AWS_REGION', 'us-east-1'))
def _is_aws():          return bool(os.environ.get('RAW_LOGS_BUCKET', ''))

# Backward-compat aliases — call as functions throughout the module
# (Using functions, not property(), since we're at module level not class level)


def _s3_put_log_lines(lines: list):
    """Write triggered error lines directly to S3 raw bucket so Lambda picks them up."""
    raw_bucket = _get_raw_bucket()
    raw_prefix = _get_raw_prefix()
    aws_region = _get_aws_region()

    if not raw_bucket:
        _log.error('[dummy_app] RAW_LOGS_BUCKET env var is not set — cannot write to S3')
        return

    _log.info('[dummy_app] Writing %d lines to s3://%s  region=%s',
              len(lines), raw_bucket, aws_region)

    try:
        import boto3
        from datetime import datetime, timezone
        ts   = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
        dt   = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        key  = f"{raw_prefix}{dt}/dummy-app-{ts}.log"
        body = '\n'.join(lines) + '\n'
        s3c  = boto3.client('s3', region_name=aws_region)
        s3c.put_object(
            Bucket=raw_bucket, Key=key,
            Body=body.encode('utf-8'), ContentType='text/plain')
        _log.info('[dummy_app] S3 upload OK → s3://%s/%s (%d bytes)',
                  raw_bucket, key, len(body))
    except Exception as exc:
        _log.error('[dummy_app] S3 upload FAILED: %s — '
                   'Check IAM role has s3:PutObject on %s', exc, raw_bucket)


def _append_lines(log_path: str, lines: list):
    if _is_aws():
        # AWS mode: write to S3 raw bucket (Lambda triggers automatically)
        _s3_put_log_lines(lines)
        return
    # Local mode: write to file as before
    try:
        dir_path = os.path.dirname(os.path.abspath(log_path))
        os.makedirs(dir_path, exist_ok=True)
        with _write_lock:
            with open(log_path, 'a', encoding='utf-8') as fh:
                for line in lines:
                    fh.write(line + '\n')
                fh.flush()
                os.fsync(fh.fileno())
        _log.info('[dummy_app] wrote %d lines to %s', len(lines), log_path)
    except Exception as exc:
        _log.error('[dummy_app] WRITE FAILED to %s: %s', log_path, exc)
        raise


def _write_error_event(log_path, http_status, error_code, description, api):
    ts = _ts()
    ip = _fake_ip()
    _append_lines(log_path, [
        f"[{ts}] [INFO] {api} IP: {ip}",
        f"[{ts}] [ERROR] {description} {{'error_code': {error_code}}}",
        f"[{ts}] [INFO] {api} Status Code: {http_status}",
    ])


def _write_success_event(log_path, http_status, api):
    ts = _ts()
    ip = _fake_ip()
    _append_lines(log_path, [
        f"[{ts}] [INFO] {api} IP: {ip}",
        f"[{ts}] [INFO] {api} Status Code: {http_status}",
    ])


def _generate_random_events(log_path, count, error_pct):
    errors = successes = 0
    for _ in range(count):
        if random.randint(1, 100) <= error_pct:
            s = random.choice(_ERROR_SCENARIOS)
            _write_error_event(log_path, *s)
            errors += 1
        else:
            status, api = random.choice(_SUCCESS_SCENARIOS)
            _write_success_event(log_path, status, api)
            successes += 1
    return {'errors': errors, 'successes': successes, 'total': count}


def _tail_logs(log_path, events_log_path, n=100):
    """Return last n dummy-related lines. In AWS mode reads from S3 processed CSV."""
    if _is_aws():
        # AWS mode: read from processed S3 bucket CSV
        try:
            import boto3, csv, io
            processed_bucket = os.environ.get('PROCESSED_BUCKET', '')
            processed_prefix = os.environ.get('PROCESSED_LOG_PREFIX', 'processed/')
            aws_region       = _get_aws_region()
            if not processed_bucket:
                return []
            s3   = boto3.client('s3', region_name=aws_region)
            resp = s3.get_object(
                Bucket=processed_bucket,
                Key=f"{processed_prefix}converted_application_logs.csv"
            )
            text    = resp['Body'].read().decode('utf-8', errors='replace')
            reader  = csv.DictReader(io.StringIO(text))
            lines   = []
            for row in reader:
                api = (row.get('API') or '').lower()
                if 'dummy' in api:
                    sc   = row.get('Status Code', row.get('Status code', ''))
                    ec   = row.get('Error Code', '')
                    desc = row.get('Description', '')
                    ts   = row.get('Timestamp', '')
                    lines.append(
                        f"[{ts}] {'[ERROR]' if ec else '[INFO]'} {row.get('API','')} "                        f"Status Code: {sc}" + (f" error_code={ec}" if ec else '')
                    )
            return list(reversed(lines[-n:]))
        except Exception as exc:
            _log.warning('[dummy_app] _tail_logs S3 read failed: %s', exc)
            return []

    # Local mode: read from file
    lines = []
    for path in [log_path, events_log_path]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    l = line.rstrip()
                    if not l:
                        continue
                    low = l.lower()
                    if 'dummy' in low or 'resolved' in low or 'ssl_event' in low:
                        lines.append(l)
        except Exception:
            pass
    return list(reversed(lines[-n:]))



def _count_stats(log_path):
    """Count ERROR events and success lines for dummy app. In AWS mode reads from S3."""
    if _is_aws():
        try:
            import boto3, csv, io
            processed_bucket = os.environ.get('PROCESSED_BUCKET', '')
            processed_prefix = os.environ.get('PROCESSED_LOG_PREFIX', 'processed/')
            aws_region       = _get_aws_region()
            if not processed_bucket:
                return {'errors': 0, 'info': 0, 'total': 0}
            s3   = boto3.client('s3', region_name=aws_region)
            resp = s3.get_object(
                Bucket=processed_bucket,
                Key=f"{processed_prefix}converted_application_logs.csv"
            )
            text   = resp['Body'].read().decode('utf-8', errors='replace')
            reader = csv.DictReader(io.StringIO(text))
            errors = successes = 0
            for row in reader:
                api = (row.get('API') or '').lower()
                if 'dummy' not in api:
                    continue
                sc = row.get('Status Code', row.get('Status code', ''))
                if sc.isdigit() and int(sc) >= 400:
                    errors += 1
                else:
                    successes += 1
            return {'errors': errors, 'info': successes, 'total': errors + successes}
        except Exception as exc:
            _log.warning('[dummy_app] _count_stats S3 read failed: %s', exc)
            return {'errors': 0, 'info': 0, 'total': 0}

    # Local mode
    errors = successes = 0
    if not os.path.exists(log_path):
        return {'errors': 0, 'info': 0, 'total': 0}
    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as fh:
            for line in fh:
                low = line.lower()
                if 'dummy' not in low:
                    continue
                if '[error]' in low:
                    errors += 1
                elif '[info]' in low and 'status code:' in low:
                    successes += 1
    except Exception:
        pass
    return {'errors': errors, 'info': successes, 'total': errors + successes}



# ── Blueprint factory ──────────────────────────────────────────────────────────

def create_dummy_app_blueprint(base_dir: str, log_filename: str, run_conversion_outputs):
    _template_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')
    dummy_bp = Blueprint('dummy_app', __name__, template_folder=_template_dir)

    log_path        = os.path.join(base_dir, 'logs', log_filename)
    events_log_path = os.path.join(base_dir, 'logs', 'ssl_events.log')

    def _get_simulator():
        try:
            from error_simulator import ErrorSimulator  # type: ignore
            return ErrorSimulator(_log, events_log_path)
        except ImportError:
            return None

    # ── Debug endpoint — call this to verify paths and write a test line ─────
    @dummy_bp.route('/api/dummy-app/debug', methods=['GET'])
    def dummy_debug():
        """Returns path info and attempts a test write. Visit this URL if errors
        are not appearing on the dashboard."""
        import platform
        test_written = False
        test_error   = None
        try:
            _write_error_event(
                log_path, 495, 9010,
                'DEBUG TEST: SSL certificate expired for domain api.dummy-app.internal',
                'GET /api/dummy/status',
            )
            test_written = True
            run_conversion_outputs()
        except Exception as e:
            test_error = str(e)

        return jsonify({
            'log_path':        log_path,
            'log_path_exists': os.path.exists(log_path),
            'log_dir_exists':  os.path.isdir(os.path.dirname(os.path.abspath(log_path))),
            'events_log_path': events_log_path,
            'base_dir':        base_dir,
            'cwd':             os.getcwd(),
            'platform':        platform.system(),
            'test_write_ok':   test_written,
            'test_error':      test_error,
        })

    # ── Health check endpoint (ALB target group health check) ────────────────
    @dummy_bp.route('/health', methods=['GET'])
    @dummy_bp.route('/api/status', methods=['GET'])
    def dummy_health():
        return jsonify({
            'status': 'ok',
            'service': 'dummy-app',
            'mode': 'aws' if _is_aws() else 'local',
            'raw_bucket': _get_raw_bucket() or None,
        }), 200

    # ── HTML page ──────────────────────────────────────────────────────────────
    @dummy_bp.route('/dummy-app', methods=['GET'])
    def dummy_app_page():
        stats = _count_stats(log_path)
        with _state_lock:
            states = {k: dict(v) for k, v in _scenario_states.items()}
        return render_template(
            'dummy_app.html',
            stats=stats,
            scenarios=NAMED_SCENARIOS,
            states=states,
        )

    # ── Trigger named error + auto-ship ───────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/trigger-error', methods=['POST'])
    def trigger_error():
        body       = request.get_json(silent=True) or {}
        error_type = body.get('error_type', '').strip()

        if error_type not in NAMED_SCENARIOS:
            return jsonify({'error': f"Unknown error_type '{error_type}'",
                            'valid': list(NAMED_SCENARIOS.keys())}), 400

        meta = NAMED_SCENARIOS[error_type]

        # Write 3-line entry to application.log
        _write_error_event(log_path, meta['http'], meta['code'], meta['desc'], meta['api'])

        # Also write to ssl_events.log via ErrorSimulator if available
        sim = _get_simulator()
        if sim:
            try:
                sim.generate_error(error_type)
            except Exception as e:
                _log.warning('ErrorSimulator failed: %s', e)

        # Update scenario state
        now = _ts()
        with _state_lock:
            s = _scenario_states[error_type]
            s['state']         = 'triggered'
            s['triggered_at']  = now
            s['resolved_at']   = None
            s['fixed_at']      = None
            s['trigger_count'] = s.get('trigger_count', 0) + 1
            s['snow_ticket']   = None

        # Auto-ship: trigger conversion immediately
        try:
            run_conversion_outputs()
        except Exception as e:
            _log.warning('Auto-ship failed: %s', e)

        _log.info('dummy_app triggered + shipped: %s (HTTP %s, code %s)',
                  error_type, meta['http'], meta['code'])

        return jsonify({
            'success':    True,
            'error_type': error_type,
            'error_code': meta['code'],
            'http_status': meta['http'],
            'description': meta['desc'],
            'shipped':     True,
        }), 200

    # ── Resolve a named error ──────────────────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/trigger-resolution', methods=['POST'])
    def trigger_resolution():
        body       = request.get_json(silent=True) or {}
        error_type = body.get('error_type', '').strip()

        if error_type not in NAMED_SCENARIOS:
            return jsonify({'error': f"Unknown error_type '{error_type}'"}), 400

        sim = _get_simulator()
        resolution_line = ''
        if sim:
            try:
                resolution_line = sim.generate_resolution(error_type, {})
            except Exception as e:
                _log.warning('ErrorSimulator.generate_resolution failed: %s', e)

        if not resolution_line:
            resolution_line = f'[{_ts()}] [INFO] RESOLVED: {error_type} resolved manually.'
            _append_lines(events_log_path, [resolution_line])

        with _state_lock:
            s = _scenario_states[error_type]
            s['state']       = 'resolved'
            s['resolved_at'] = _ts()

        return jsonify({'success': True, 'error_type': error_type,
                        'resolution_line': resolution_line}), 200

    # ── Mark as fixed by Bedrock (called by dashboard fix pipeline) ────────────
    @dummy_bp.route('/api/dummy-app/mark-fixed', methods=['POST'])
    def mark_fixed():
        """Called by the dashboard /api/fix-error pipeline after successful remediation."""
        body       = request.get_json(silent=True) or {}
        error_code = str(body.get('error_code', ''))
        snow_num   = body.get('snow_number', '')
        snow_id    = body.get('snow_sys_id', '')

        # Find which scenario matches this error code
        matched = None
        for t, m in NAMED_SCENARIOS.items():
            if str(m['code']) == error_code:
                matched = t
                break

        if not matched:
            return jsonify({'error': f'No scenario for error_code {error_code}'}), 404

        with _state_lock:
            s = _scenario_states[matched]
            s['state']       = 'fixed'
            s['fixed_at']    = _ts()
            s['snow_ticket'] = {'number': snow_num, 'sys_id': snow_id} if snow_num else None

        _log.info('dummy_app marked fixed: %s (code=%s)', matched, error_code)
        return jsonify({'success': True, 'error_type': matched}), 200

    # ── Random event generator ─────────────────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/generate', methods=['POST'])
    def generate():
        body      = request.get_json(silent=True) or {}
        count     = max(1, min(500, int(body.get('count', 50))))
        error_pct = max(0, min(100, int(body.get('error_pct', 25))))
        result    = _generate_random_events(log_path, count, error_pct)
        run_conversion_outputs()
        return jsonify({'success': True, 'generated': result}), 200

    # ── Force conversion ───────────────────────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/ship', methods=['POST'])
    def ship():
        run_conversion_outputs()
        return jsonify({'success': True, 'message': 'Conversion triggered.'}), 200

    # ── Log tail ───────────────────────────────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/logs', methods=['GET'])
    def dummy_logs():
        n     = min(200, max(1, int(request.args.get('n', 100))))
        lines = _tail_logs(log_path, events_log_path, n)
        return jsonify({'lines': lines, 'count': len(lines)}), 200

    # ── Stats ──────────────────────────────────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/stats', methods=['GET'])
    def dummy_stats():
        return jsonify(_count_stats(log_path)), 200

    # ── Scenario states ────────────────────────────────────────────────────────
    @dummy_bp.route('/api/dummy-app/scenario-states', methods=['GET'])
    def scenario_states():
        with _state_lock:
            states = {k: dict(v) for k, v in _scenario_states.items()}
        return jsonify({'states': states}), 200

    return dummy_bp
