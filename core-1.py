# Core API routes: health check, log submission, and log retrieval.
#
# Routes registered by this blueprint:
#   GET  /             — Welcome message and API version.
#   GET  /api/status   — Server liveness and uptime.
#   POST /api/logs     — Accept a JSON body and write a log entry at the specified level.
#   GET  /api/logs     — Return all lines from the application log file.

import os
import time
from datetime import datetime
from flask import Blueprint, jsonify, request
from logger import info, error, warn, debug  # type: ignore[reportMissingImports]
core_bp = Blueprint('core', __name__)

# Record the moment the module is loaded so /api/status can report uptime.
_start_time = time.time()

# Get log filename from environment, must match the filename configured in logger.py.
_LOG_FILENAME = os.getenv('LOG_FILENAME', 'application.log')


@core_bp.route('/', methods=['GET'])
def welcome():
    info("Root endpoint accessed")
    return jsonify({
        "message": "Welcome to Log Aggregator API",
        "version": "1.0.0",
    })


@core_bp.route('/api/status', methods=['GET'])
def status():
    info("Status endpoint called")
    uptime = time.time() - _start_time
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "uptime": round(uptime, 2),
    })


@core_bp.route('/api/logs', methods=['POST'])
def create_log():
    """Accept a JSON body with 'message' and optional 'level' and write to the application log."""
    data = request.get_json()

    if not data or 'message' not in data:
        warn("Invalid log request - missing message")
        return jsonify({"error": "Message is required"}), 400

    message = data.get('message')
    if not isinstance(message, str) or len(message) > 10_000:
        warn("Invalid log request - message must be a string under 10000 characters")
        return jsonify({"error": "Message must be a string of at most 10000 characters"}), 400

    level = data.get('level', 'info').lower()
    if level not in ('debug', 'info', 'warn', 'error'):
        level = 'info'

    # Dispatch to the matching log-level function; default to info for unknown levels.
    log_fn = {'error': error, 'warn': warn, 'debug': debug}.get(level, info)
    log_fn(f"Custom log received: {message}")

    return jsonify({
        "success": True,
        "message": "Log entry recorded",
        "timestamp": datetime.now().isoformat(),
    }), 201


@core_bp.route('/api/logs', methods=['GET'])
def get_logs():
    """Return lines from the application log file as a JSON array with optional pagination."""
    # Resolve relative to this file so it works regardless of CWD
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    logs_dir  = os.path.join(_this_dir, '..', os.getenv('LOGS_DIRECTORY', 'logs'))
    log_file  = os.path.join(logs_dir, _LOG_FILENAME)

    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        per_page = min(1000, max(1, int(request.args.get('per_page', 500))))
    except (ValueError, TypeError):
        per_page = 500

    try:
        with open(log_file, 'r') as f:
            # Split on newlines and discard blank lines before returning.
            all_logs = [line for line in f.read().strip().split('\n') if line.strip()]
        total = len(all_logs)
        start = (page - 1) * per_page
        logs = all_logs[start:start + per_page]
        info("Logs retrieved successfully")
        return jsonify({"logs": logs, "count": len(logs), "total": total, "page": page, "per_page": per_page}), 200
    except FileNotFoundError as e:
        error(f"Log file not found: {str(e)}", {"error_code": 1001})
        return jsonify({"error": "Unable to retrieve logs", "error_code": 1001}), 500
    except Exception as e:
        error(f"Error reading logs: {str(e)}", {"error_code": 1000})
        return jsonify({"error": "Unable to retrieve logs", "error_code": 1000}), 500
