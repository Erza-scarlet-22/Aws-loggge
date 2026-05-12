# Application/app.py  –  Log Aggregator entry point (AWS + local mode)
#
# AWS mode  : RAW_LOGS_BUCKET env var set by ECS task definition.
#   - logger writes to /app/Application/logs/application.log (local temp file)
#   - After each modifying request, new lines are uploaded to S3 raw bucket
#   - POST /api/ingest  writes directly to S3 so curl → Lambda is instant
#   - Dashboard reads unique_errors.json from S3 processed bucket (Lambda wrote it)
#
# Local mode: RAW_LOGS_BUCKET absent.
#   - Behaves exactly as before (file-based conversion, dashboard reads local JSON)

from flask import Flask, jsonify, request
from logger import info, error, warn
import os, sys, time, threading

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT   = os.path.abspath(os.path.join(BASE_DIR, '..'))
CONVERSION_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', 'Conversion'))
DASHBOARD_DIR  = os.path.abspath(os.path.join(BASE_DIR, '..', 'Dashboard'))
DUMMY_APP_DIR  = os.path.abspath(os.path.join(BASE_DIR, '..', 'DummyApp'))

# .env loaded by logger.py (local mode only)

# ── Config ────────────────────────────────────────────────────────────────────
APP_PORT         = int(os.getenv('APP_PORT',    '5000'))
APP_HOST         =     os.getenv('APP_HOST',    '0.0.0.0')
FLASK_DEBUG      =     os.getenv('FLASK_DEBUG', 'false').lower() in ('true','1','yes')
APP_LOG_FILENAME =     os.getenv('LOG_FILENAME','application.log')

RAW_LOGS_BUCKET  = os.getenv('RAW_LOGS_BUCKET',  '')
RAW_LOGS_PREFIX  = os.getenv('RAW_LOGS_PREFIX',  'raw-logs/')
PROCESSED_BUCKET = os.getenv('PROCESSED_BUCKET', '')
AWS_REGION       = os.getenv('AWS_DEFAULT_REGION', os.getenv('AWS_REGION', 'us-east-1'))
IS_AWS           = bool(RAW_LOGS_BUCKET)

for _d in (PROJECT_ROOT, CONVERSION_DIR, DASHBOARD_DIR, DUMMY_APP_DIR):
    if _d not in sys.path:
        sys.path.append(_d)

# ── S3 uploader ───────────────────────────────────────────────────────────────
# Track how many bytes we have already uploaded to S3 so each flush
# only sends NEW lines written since the last flush.
_s3_flush_offset = 0
_s3_flush_lock   = threading.Lock()

def _flush_log_to_s3():
    """
    Upload only NEW lines (since last flush) to S3 as a uniquely-named object.
    Each flush creates a new S3 object → new ObjectCreated event → Lambda runs.
    Using unique keys (timestamped) means Lambda processes a small delta each time
    instead of re-processing the entire growing application.log.
    """
    global _s3_flush_offset
    if not IS_AWS:
        return
    log_path = os.path.join(BASE_DIR, 'logs', APP_LOG_FILENAME)
    try:
        if not os.path.exists(log_path):
            return
        with _s3_flush_lock:
            with open(log_path, 'rb') as fh:
                fh.seek(_s3_flush_offset)
                new_bytes = fh.read()
            if not new_bytes.strip():
                return  # nothing new to upload
            # Build a unique key so every upload fires a fresh S3 ObjectCreated
            from datetime import datetime, timezone as _tz
            now   = datetime.now(_tz.utc)
            dt    = now.strftime('%Y-%m-%d')
            ts    = now.strftime('%Y%m%d%H%M%S%f')
            key   = f"{RAW_LOGS_PREFIX}{dt}/application-{ts}.log"
            import boto3
            region = os.environ.get('AWS_DEFAULT_REGION',
                       os.environ.get('AWS_REGION', 'us-east-1'))
            boto3.client('s3', region_name=region).put_object(
                Bucket=RAW_LOGS_BUCKET,
                Key=key,
                Body=new_bytes,
                ContentType='text/plain',
            )
            _s3_flush_offset += len(new_bytes)
            print(f"[app] S3 flush OK → s3://{RAW_LOGS_BUCKET}/{key} ({len(new_bytes)} bytes)")
    except Exception as exc:
        print(f"[app] S3 flush error: {exc}")

def _bg_flush():
    threading.Thread(target=_flush_log_to_s3, daemon=True).start()

# ── Optional modules ──────────────────────────────────────────────────────────
try:
    from log_to_csv_service import convert_log_to_rows, write_rows_to_csv, write_unique_errors_json
    CONVERTER_AVAILABLE = True
except Exception:
    CONVERTER_AVAILABLE = False

try:
    from dashboard_blueprint import create_dashboard_blueprint
    DASHBOARD_AVAILABLE = True
except Exception:
    DASHBOARD_AVAILABLE = False

try:
    from dummy_app_blueprint import create_dummy_app_blueprint
    DUMMY_APP_AVAILABLE = True
except Exception as _e:
    DUMMY_APP_AVAILABLE = False
    print(f"[app] DummyApp not loaded: {_e}")

# ── Conversion callback ───────────────────────────────────────────────────────
_conv_lock = threading.Lock()
_last_conv = 0.0
_DEBOUNCE  = 5   # seconds

def run_conversion_outputs():
    global _last_conv
    now = time.time()
    with _conv_lock:
        if now - _last_conv < _DEBOUNCE:
            return
        _last_conv = now

    if IS_AWS:
        _bg_flush()   # upload log → Lambda auto-triggers → processed bucket
        return

    if not CONVERTER_AVAILABLE:
        return

    def _do():
        try:
            lp = os.path.join(BASE_DIR, 'logs', APP_LOG_FILENAME)
            cp = os.path.join(CONVERSION_DIR, 'converted_application_logs.csv')
            jp = os.path.join(CONVERSION_DIR, 'unique_errors.json')
            if not os.path.exists(lp):
                return
            # Story 4970/4968: purge log lines older than LOG_RETENTION_DAYS
            # before converting so the CSV and JSON only contain retained data
            try:
                from log_to_csv_service import purge_log_file  # type: ignore
                removed = purge_log_file(lp)
                if removed:
                    print(f"[app] Purged {removed} log lines beyond retention window")
            except Exception as _pe:
                print(f"[app] Purge skipped: {_pe}")
            rows = convert_log_to_rows(lp)
            write_rows_to_csv(rows, cp)
            write_unique_errors_json(rows, jp)
        except Exception as exc:
            print(f"[app] Conversion error: {exc}")

    threading.Thread(target=_do, daemon=True).start()

# ── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)

from routes.core           import core_bp
from routes.payments       import payments_bp
from routes.auth           import auth_bp
from routes.orders         import orders_bp
from routes.users          import users_bp
from routes.infrastructure import infrastructure_bp
from routes.simulator      import create_simulator_blueprint

app.register_blueprint(core_bp)
app.register_blueprint(payments_bp)
app.register_blueprint(auth_bp)
app.register_blueprint(orders_bp)
app.register_blueprint(users_bp)
app.register_blueprint(infrastructure_bp)
app.register_blueprint(
    create_simulator_blueprint(BASE_DIR, APP_LOG_FILENAME, run_conversion_outputs))

if DASHBOARD_AVAILABLE:
    app.register_blueprint(
        create_dashboard_blueprint(CONVERSION_DIR, run_conversion_outputs))

if DUMMY_APP_AVAILABLE:
    app.register_blueprint(
        create_dummy_app_blueprint(BASE_DIR, APP_LOG_FILENAME, run_conversion_outputs))

# ── POST /api/ingest  (curl → S3 → Lambda pipeline entry point) ───────────────
@app.route('/api/ingest', methods=['POST'])
def ingest_logs():
    """
    Receive raw log lines via curl and persist them.

    AWS mode  → writes directly to S3 raw bucket as a timestamped object.
                Lambda triggers on ObjectCreated, converts, writes processed bucket.
    Local mode → appends to application.log, triggers conversion synchronously.

    Example:
        # Ingest a single error event
        curl -X POST http://<alb>/api/ingest \\
             -H "Content-Type: text/plain" \\
             --data "[2026-05-08T10:00:00] [ERROR] SSL cert expired {'error_code': 9010}"

        # Ingest an entire log file
        curl -X POST http://<alb>/api/ingest \\
             -H "Content-Type: text/plain" \\
             --data-binary @/path/to/application.log
    """
    body = request.get_data(as_text=True)
    if not body or not body.strip():
        return jsonify({'error': 'Empty body — send log lines as text/plain'}), 400

    if IS_AWS:
        try:
            import boto3
            from datetime import datetime, timezone
            ts  = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
            dt  = datetime.now(timezone.utc).strftime('%Y-%m-%d')
            key = f"{RAW_LOGS_PREFIX}{dt}/ingest-{ts}.log"
            boto3.client('s3', region_name=AWS_REGION).put_object(
                Bucket=RAW_LOGS_BUCKET, Key=key,
                Body=body.encode('utf-8'), ContentType='text/plain')
            return jsonify({'success': True, 'bucket': RAW_LOGS_BUCKET,
                            'key': key, 'note': 'Lambda processing started'}), 200
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500
    else:
        log_path = os.path.join(BASE_DIR, 'logs', APP_LOG_FILENAME)
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, 'a', encoding='utf-8') as fh:
            fh.write(body if body.endswith('\n') else body + '\n')
        run_conversion_outputs()
        return jsonify({'success': True, 'mode': 'local',
                        'appended_bytes': len(body)}), 200

# ── Middleware ────────────────────────────────────────────────────────────────
@app.before_request
def _before():
    request._start_time = time.time()

@app.after_request
def _after(response):
    dur = time.time() - getattr(request, '_start_time', time.time())
    if request.path not in ('/api/status', '/health', '/favicon.ico'):
        info(f"{request.method} {request.path} {response.status_code} ({dur*1000:.0f}ms)")
    if IS_AWS and request.method in ('POST','PUT','DELETE','PATCH'):
        _bg_flush()
    return response

@app.errorhandler(404)
def _404(e):
    return jsonify({'error': 'Not found', 'path': request.path}), 404

@app.errorhandler(500)
def _500(e):
    error(f"500: {e}")
    return jsonify({'error': 'Internal server error'}), 500

@app.route('/api/status')
@app.route('/health')
def _status():
    return jsonify({'status': 'ok', 'mode': 'aws' if IS_AWS else 'local',
                    'raw_bucket': RAW_LOGS_BUCKET or None,
                    'processed_bucket': PROCESSED_BUCKET or None}), 200

if __name__ == '__main__':
    mode = 'AWS' if IS_AWS else 'LOCAL'
    print(f"[app] Starting Log Aggregator  mode={mode}  port={APP_PORT}")
    print(f"[app] Dashboard  : {'enabled' if DASHBOARD_AVAILABLE else 'disabled'}")
    print(f"[app] DummyApp   : {'enabled' if DUMMY_APP_AVAILABLE else 'disabled'}")
    if IS_AWS:
        print(f"[app] Raw bucket       : {RAW_LOGS_BUCKET}")
        print(f"[app] Processed bucket : {PROCESSED_BUCKET}")
    print("[app] Routes registered:")
    print("  Core API:")
    print("    GET  /")
    print("    GET  /api/status   (health check)")
    print("    GET  /health       (ALB health check)")
    print("    POST /api/logs     (write a log entry)")
    print("    GET  /api/logs     (retrieve log lines)")
    print("  Ingest:")
    print("    POST /api/ingest   (curl log lines → S3 in AWS, local file otherwise)")
    print("  Simulator:")
    print("    POST /api/simulate-traffic  (seed 30 days of demo errors)")
    print("    POST /api/validate          (payload validation with error logging)")
    print("  Payments:")
    print("    POST /api/payments/charge")
    print("    POST /api/payments/refund")
    print("  Auth:")
    print("    POST /api/auth/token")
    print("    POST /api/auth/refresh")
    print("    POST /api/auth/login")
    print("  Orders:")
    print("    POST   /api/orders")
    print("    GET    /api/orders/<order_id>")
    print("    DELETE /api/orders/<order_id>")
    print("  Users:")
    print("    POST /api/users/register")
    print("    PUT  /api/users/profile")
    print("  Infrastructure:")
    print("    POST /api/notifications/email")
    print("    GET  /api/recommendations")
    print("    POST /api/inventory/sync")
    print("    POST /api/fulfillment/dispatch")
    if DASHBOARD_AVAILABLE:
        print("  Dashboard:")
        print("    GET  /dashboard")
        print("    GET  /api/dashboard-data")
        print("    POST /api/snow/create")
        print("    GET  /api/snow/status/<sys_id>")
        print("    POST /api/snow/fix")
        print("    POST /api/snow/update")
        print("    GET  /api/snow/tickets")
        print("    POST /api/fix-error")
        print("    GET  /api/chat")
    if DUMMY_APP_AVAILABLE:
        print("  Dummy App:")
        print("    GET  /dummy-app")
        print("    POST /api/dummy-app/trigger-error")
        print("    POST /api/dummy-app/trigger-resolution")
        print("    POST /api/dummy-app/generate")
        print("    POST /api/dummy-app/ship")
        print("    GET  /api/dummy-app/logs")
        print("    GET  /api/dummy-app/stats")
        print("    GET  /api/dummy-app/scenario-states")
        print("    POST /api/dummy-app/mark-fixed")
        print("    GET  /api/dummy-app/debug")
    app.run(host=APP_HOST, port=APP_PORT, debug=FLASK_DEBUG)
