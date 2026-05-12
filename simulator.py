# Traffic simulator and data validation routes.
#
# Routes:
#   POST /api/simulate-traffic — Seeds the application log with a weighted, backdated
#                                distribution of realistic error events across the last
#                                30 days (used to populate the dashboard for demos).
#   POST /api/validate         — Validates a JSON payload (name, email, age) and logs
#                                any validation failures for aggregation testing.

import os
import random
import re
import threading
from datetime import datetime, timedelta
from typing import Callable
from flask import Blueprint, jsonify, request
from logger import error, info  # type: ignore[reportMissingImports]

# Reused across the scenarios table and the /api/validate handler.
_VALIDATE_API = 'POST /api/validate'
_MSG_BODY_EMPTY = 'Request body is empty'

# Each tuple: (http_status, error_code, description, api_path, occurrence_count).
# The occurrence count controls how many backdated log entries are seeded per scenario,
# giving the dashboard a realistic weighted distribution of error types.
_SCENARIOS = [
    (503, 5001, "Payment gateway did not respond within SLA threshold",                "POST /api/payments/charge",        8),
    (402, 5002, "Card declined by issuer — insufficient funds",                        "POST /api/payments/charge",       15),
    (422, 5003, "Refund window expired — transaction is older than 90 days",           "POST /api/payments/refund",        6),
    (401, 6001, "Authentication failed — invalid credentials provided",                "POST /api/auth/token",            22),
    (401, 6002, "JWT refresh token has expired",                                       "POST /api/auth/refresh",          18),
    (401, 6003, "MFA verification required but not provided",                          "POST /api/auth/token",             9),
    (429, 6004, "Account temporarily locked after 5 consecutive failed login attempts","POST /api/auth/login",            11),
    (409, 7001, "Requested quantity exceeds available stock level",                    "POST /api/orders",                13),
    (404, 7002, "Order not found or belongs to a different account",                   "GET /api/orders/{id}",            19),
    (409, 7003, "Cannot cancel order — shipment already dispatched",                   "DELETE /api/orders/{id}",          7),
    (409, 8001, "Registration failed — email address already registered",              "POST /api/users/register",        14),
    (422, 8002, "Phone number format is invalid for the specified region",             "PUT /api/users/profile",           8),
    (503, 9001, "Email delivery service is unreachable — circuit breaker open",        "POST /api/notifications/email",   10),
    (504, 9002, "Upstream recommendation engine timed out after 3000ms",               "GET /api/recommendations",        12),
    (503, 9003, "Inventory microservice is unreachable — health check failed",         "POST /api/inventory/sync",         9),
    (502, 9004, "Received malformed response from downstream fulfillment provider",    "POST /api/fulfillment/dispatch",   5),
    (400, 3001, _MSG_BODY_EMPTY,                                                       _VALIDATE_API,                      6),
    (400, 3002, "Missing required fields: name, email",                                _VALIDATE_API,                      8),
    (400, 3003, "Invalid email format",                                                _VALIDATE_API,                     11),
    (400, 3005, "Age is not a valid integer",                                          _VALIDATE_API,                      4),
]


def create_simulator_blueprint(base_dir: str, log_filename: str, run_conversion_outputs: Callable):
    """Create and return the simulator blueprint.

    Args:
        base_dir: Absolute path to the Application directory (used to locate the log file).
        log_filename: Name of the application log file (e.g. 'application.log').
        run_conversion_outputs: Callback that regenerates dashboard artefacts after seeding.
    """
    simulator_bp = Blueprint('simulator', __name__)

    # Resolve the log path once at blueprint-creation time.
    log_file_path = os.path.join(base_dir, 'logs', log_filename)
    # Lock prevents concurrent simulation runs from corrupting the log file.
    _write_lock = threading.Lock()

    # AWS / local mode detection
    _RAW_BUCKET = os.environ.get('RAW_LOGS_BUCKET', '')
    _RAW_PREFIX = os.environ.get('RAW_LOGS_PREFIX', 'raw-logs/')
    _AWS_REGION = os.environ.get('AWS_DEFAULT_REGION', os.environ.get('AWS_REGION', 'us-east-1'))
    _IS_AWS_SIM = bool(_RAW_BUCKET)

    @simulator_bp.route('/api/simulate-traffic', methods=['POST'])
    def simulate_traffic():
        """Seed the application log with a weighted, backdated distribution of realistic
        production-like error events spread across the last 30 days.

        AWS mode : writes log lines directly to S3 raw bucket (unique key per call)
                   so Lambda triggers immediately and processes all events at once.
        Local mode: appends to application.log and triggers local conversion.
        """
        total = 0
        now = datetime.now()

        # Build all log lines in memory first
        lines = []
        for http_status, err_code, description, api_path, count in _SCENARIOS:
            for _ in range(count):
                dt = now - timedelta(
                    days=random.randint(0, 29),
                    hours=random.randint(0, 23),
                    minutes=random.randint(0, 59),
                    seconds=random.randint(0, 59),
                )
                ts = dt.strftime('%Y-%m-%dT%H:%M:%S')
                fake_ip = f"10.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
                lines.append(f"[{ts}] [INFO] {api_path} IP: {fake_ip}")
                lines.append(f"[{ts}] [ERROR] {description} {{'error_code': {err_code}}}")
                lines.append(f"[{ts}] [INFO] {api_path} Status Code: {http_status}")
                total += 1

        log_content = "\n".join(lines) + "\n"

        if _IS_AWS_SIM:
            # AWS mode: write to S3 with a UNIQUE key so S3 ObjectCreated fires
            # and Lambda processes all the simulated events at once
            try:
                import boto3
                from datetime import timezone
                ts_key = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
                dt_key = datetime.now(timezone.utc).strftime('%Y-%m-%d')
                key    = f"{_RAW_PREFIX}{dt_key}/simulate-traffic-{ts_key}.log"
                boto3.client('s3', region_name=_AWS_REGION).put_object(
                    Bucket=_RAW_BUCKET,
                    Key=key,
                    Body=log_content.encode('utf-8'),
                    ContentType='text/plain',
                )
                info(f"[simulator] Uploaded {total} events to s3://{_RAW_BUCKET}/{key}")
                return jsonify({
                    "success": True,
                    "events_seeded": total,
                    "mode": "aws",
                    "s3_key": key,
                    "message": f"{total} events uploaded to S3. Lambda processing — dashboard updates in ~5 seconds.",
                }), 200
            except Exception as exc:
                info(f"[simulator] S3 upload failed: {exc} — falling back to local file")

        # Local mode (or S3 fallback): append to log file
        os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
        with _write_lock:
            with open(log_file_path, 'a', encoding='utf-8') as lf:
                lf.write(log_content)

        run_conversion_outputs()
        info(f"Traffic simulation complete — {total} error events seeded across last 30 days")
        return jsonify({
            "success": True,
            "events_seeded": total,
            "mode": "local",
            "message": f"{total} realistic error events written. Dashboard will update on next refresh.",
        }), 200

    @simulator_bp.route('/api/validate', methods=['POST'])
    def validate_data():
        """Validate a JSON payload containing name, email, and age fields."""
        data = request.get_json()

        if not data:
            error(_MSG_BODY_EMPTY, {"error_code": 3001})
            return jsonify({"error": _MSG_BODY_EMPTY, "error_code": 3001}), 400

        # Ensure all required fields are present.
        required_fields = ['name', 'email', 'age']
        missing_fields = [f for f in required_fields if f not in data]
        if missing_fields:
            error(f"Missing required fields: {', '.join(missing_fields)}",
                  {"error_code": 3002, "missing": missing_fields})
            return jsonify({"error": f"Missing fields: {missing_fields}", "error_code": 3002}), 400

        # Basic email format check — must match a minimal RFC-5322 pattern.
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', data.get('email', '')):
            error(f"Invalid email format: {data.get('email')}", {"error_code": 3003, "field": "email"})
            return jsonify({"error": "Invalid email format", "error_code": 3003}), 400

        # Age must be a whole number within the range 0–150.
        try:
            age = int(data.get('age', 0))
            if age < 0 or age > 150:
                error(f"Age out of valid range: {age}",
                      {"error_code": 3004, "field": "age", "min": 0, "max": 150})
                return jsonify({"error": "Age must be between 0 and 150", "error_code": 3004}), 400
        except ValueError:
            error(f"Age is not a valid integer: {data.get('age')}", {"error_code": 3005, "field": "age"})
            return jsonify({"error": "Age must be an integer", "error_code": 3005}), 400

        info(f"Data validation successful for user: {data.get('name')}", {"status_code": 200})
        return jsonify({"success": True, "message": "Data validation successful", "data": data}), 200

    return simulator_bp
