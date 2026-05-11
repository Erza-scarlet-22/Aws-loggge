# Dashboard Flask blueprint for the Log Aggregator.
#
# Routes:
#   GET  /dashboard                    — SPA shell
#   GET  /api/dashboard-data           — JSON payload (accepts ?source=dummy_app)
#   GET  /api/dashboard-report.pdf     — PDF export
#   POST /api/chat-insights            — Bedrock agent proxy
#   POST /api/snow/create              — Create ServiceNow incident
#   GET  /api/snow/status/<sys_id>     — Poll incident state
#   POST /api/snow/fix                 — Resolve incident
#   POST /api/snow/update              — Add notes / change state
#   GET  /api/snow/tickets             — In-memory ticket store (for page-refresh restore)

from datetime import date, datetime, timezone
from flask import Blueprint, jsonify, render_template, request, send_file

try:
    from bedrock_chat_service import generate_error_insight  # type: ignore[reportMissingImports]
    BEDROCK_CHAT_AVAILABLE = True
except Exception:
    BEDROCK_CHAT_AVAILABLE = False

try:
    from dashboard_pdf_service import build_dashboard_pdf, REPORTLAB_AVAILABLE  # type: ignore[reportMissingImports]
except Exception:
    REPORTLAB_AVAILABLE = False
    def build_dashboard_pdf(_): ...

from dashboard_data_service import build_dashboard_payload  # type: ignore[reportMissingImports]

# ── In-memory ticket store ─────────────────────────────────────────────────────
# key: "StatusCode|ErrorCode|API"  →  ticket dict
_ticket_store: dict = {}


def _row_key(row: dict) -> str:
    return f"{row.get('Status Code','')}|{row.get('Error Code','')}|{row.get('API','')}"


# ── Lazy ServiceNow loader ─────────────────────────────────────────────────────
# Import at call-time rather than module-load-time so it works regardless of
# when sys.path is populated (avoids the "not found" error on cold import).
def _snow():
    """Return servicenow_client module or raise ImportError with a clear message."""
    try:
        import servicenow_client as _sc  # type: ignore[reportMissingImports]
        return _sc
    except ImportError:
        raise ImportError(
            "servicenow_client.py could not be imported. "
            "Make sure it is at the project root and that PROJECT_ROOT is on sys.path "
            "(check Application/app.py)."
        )


def _snow_configured() -> bool:
    try:
        return _snow().is_configured()
    except ImportError:
        return False


def _state_label(state) -> str:
    labels = {
        '1': 'New', '2': 'In Progress', '3': 'On Hold',
        '4': 'Awaiting User Info', '5': 'Awaiting Problem',
        '6': 'Resolved', '7': 'Closed', '8': 'Cancelled',
    }
    return labels.get(str(state), f'Unknown ({state})')


# ── Blueprint factory ──────────────────────────────────────────────────────────

def create_dashboard_blueprint(conversion_dir: str, run_conversion_outputs):
    dashboard_bp = Blueprint('dashboard', __name__, template_folder='templates')

    # ── Existing routes — unchanged ────────────────────────────────────────────

    @dashboard_bp.route('/dashboard', methods=['GET'])
    def dashboard_page():
        return render_template('dashboard.html')

    @dashboard_bp.route('/api/dashboard-data', methods=['GET'])
    def dashboard_data():
        payload = build_dashboard_payload(conversion_dir, run_conversion_outputs, request.args)

        # Optional ?source=dummy_app filter — matches all dummy app API paths:
        #   /api/dummy_app/...  (2000-series random events)
        #   /api/dummy/...      (9000-series named ErrorSimulator events)
        source_filter = request.args.get('source', '').strip().lower()
        if source_filter:
            if source_filter == 'dummy_app':
                # Matches both dummy app path styles:
                #   /api/dummy/...      (9000-series: ssl_expired, password_expired, etc.)
                #   /api/dummy_app/...  (2000-series: random events)
                filter_terms = ['/api/dummy/', 'dummy_app']
            else:
                filter_terms = [source_filter]

            payload['rows'] = [
                r for r in payload['rows']
                if any(term in (r.get('API') or '').lower() for term in filter_terms)
            ]

            # Recompute all summary stats for the filtered set
            filtered_rows = payload['rows']
            total_events  = sum(int(r.get('Count', 0)) for r in filtered_rows)
            by_status     = {}
            by_api        = {}
            for r in filtered_rows:
                sc  = str(r.get('Status Code', 'Unknown'))
                api = str(r.get('API', 'Unknown'))
                cnt = int(r.get('Count', 0))
                by_status[sc]  = by_status.get(sc, 0)  + cnt
                by_api[api]    = by_api.get(api, 0)    + cnt

            payload['summary']['uniqueErrorTypes'] = len(filtered_rows)
            payload['summary']['totalErrorEvents'] = total_events
            payload['byStatus']     = by_status
            payload['byApi']        = by_api
            payload['sourceFilter'] = source_filter
            # Preserve filter metadata including retention info for the banner
            # (source filtering must not strip retentionDays / cutoffDate)
            if 'filter' not in payload:
                payload['filter'] = {}
            payload['filter']['sourceFilter'] = source_filter

        return jsonify(payload), 200

    @dashboard_bp.route('/api/dashboard-report.pdf', methods=['GET'])
    def dashboard_report_pdf():
        if not REPORTLAB_AVAILABLE:
            return jsonify({'error': 'PDF export unavailable — install reportlab.'}), 503
        payload    = build_dashboard_payload(conversion_dir, run_conversion_outputs, request.args)
        pdf_buffer = build_dashboard_pdf(payload)
        filename   = f"error-dashboard-report-{date.today().isoformat()}.pdf"
        return send_file(pdf_buffer, mimetype='application/pdf', as_attachment=True, download_name=filename)

    @dashboard_bp.route('/api/chat-insights', methods=['POST'])
    def chat_insights():
        payload       = request.get_json(silent=True) or {}
        error_context = payload.get('error') or {}
        user_message  = (payload.get('message') or '').strip()
        history       = payload.get('history') or []
        session_id    = (payload.get('sessionId') or '').strip()

        if not isinstance(error_context, dict):
            return jsonify({'error': 'Invalid payload: error context must be an object'}), 400
        if not isinstance(history, list):
            return jsonify({'error': 'Invalid payload: history must be a list'}), 400
        if not user_message:
            user_message = 'Provide insights and remediation steps for this selected error.'
        if not BEDROCK_CHAT_AVAILABLE:
            return jsonify({'error': 'AWS Bedrock not available. Set BEDROCK_AGENT_ID and BEDROCK_AGENT_ALIAS_ID.'}), 503

        try:
            reply_text, metadata = generate_error_insight(error_context, user_message, history, session_id)
            return jsonify({
                'reply':     reply_text,
                'provider':  'aws-bedrock-agent',
                'modelId':   metadata.get('model_id', ''),
                'region':    metadata.get('region', ''),
                'sessionId': metadata.get('session_id', session_id),
            }), 200
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    # ── ServiceNow routes ──────────────────────────────────────────────────────

    # ── Retention info endpoint (Story 4968/4970) ─────────────────────────────
    @dashboard_bp.route('/api/retention-info', methods=['GET'])
    def retention_info():
        """
        Returns current log retention configuration.
        Used by the dashboard banner and can be used in demo presentations.

        Response:
            {
              "retentionDays": 90,
              "cutoffDate":    "2026-02-10",
              "description":   "Data older than 2026-02-10 is automatically purged",
              "configuredBy":  "LOG_RETENTION_DAYS environment variable"
            }
        """
        import os as _os
        from datetime import date as _date, timedelta as _td

        days   = int(_os.environ.get('LOG_RETENTION_DAYS', '90'))
        cutoff = (_date.today() - _td(days=days)).isoformat() if days > 0 else None
        return jsonify({
            'retentionDays': days,
            'cutoffDate':    cutoff,
            'description':   (
                f'Data older than {cutoff} is automatically purged'
                if cutoff else 'No retention limit configured'
            ),
            'configuredBy': 'LOG_RETENTION_DAYS environment variable',
        }), 200

    @dashboard_bp.route('/api/snow/create', methods=['POST'])
    def snow_create():
        # Lazy import — works even if sys.path was not set when the module loaded
        try:
            sc = _snow()
        except ImportError as exc:
            return jsonify({'error': str(exc)}), 503

        if not sc.is_configured():
            return jsonify({
                'error': 'ServiceNow not configured. '
                         'Set SERVICENOW_INSTANCE, SERVICENOW_USERNAME, SERVICENOW_PASSWORD in .env'
            }), 503

        row = request.get_json(silent=True) or {}
        if not row.get('Error Code') and not row.get('Description'):
            return jsonify({'error': 'Payload must include Error Code or Description'}), 400

        key = _row_key(row)
        if key in _ticket_store:
            return jsonify({'ticket': _ticket_store[key], 'existing': True}), 200

        try:
            incident = sc.create_incident_from_row(row)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

        ticket = {
            'sys_id':      incident.get('sys_id'),
            'number':      incident.get('number'),
            'state':       incident.get('state', '1'),
            'state_label': _state_label(incident.get('state', '1')),
            'created_at':  datetime.now(timezone.utc).isoformat(),
            'fixed_at':    None,
            'fix_status':  'open',
        }
        _ticket_store[key] = ticket
        return jsonify({'ticket': ticket, 'existing': False}), 201

    @dashboard_bp.route('/api/snow/status/<sys_id>', methods=['GET'])
    def snow_status(sys_id):
        try:
            sc = _snow()
        except ImportError as exc:
            return jsonify({'error': str(exc)}), 503
        if not sc.is_configured():
            return jsonify({'error': 'ServiceNow not configured'}), 503
        try:
            inc = sc.get_incident(sys_id)
            return jsonify({
                'sys_id':      sys_id,
                'number':      inc.get('number'),
                'state':       inc.get('state'),
                'state_label': _state_label(inc.get('state', '')),
            }), 200
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

    @dashboard_bp.route('/api/snow/fix', methods=['POST'])
    def snow_fix():
        try:
            sc = _snow()
        except ImportError as exc:
            return jsonify({'error': str(exc)}), 503
        if not sc.is_configured():
            return jsonify({'error': 'ServiceNow not configured'}), 503

        body   = request.get_json(silent=True) or {}
        sys_id = body.get('sys_id', '').strip()
        notes  = body.get('close_notes', 'Resolved via Log Aggregator dashboard Fix button.')
        if not sys_id:
            return jsonify({'error': 'sys_id is required'}), 400

        try:
            result = sc.resolve_incident(sys_id, close_notes=notes)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

        fixed_at = datetime.now(timezone.utc).isoformat()
        # Mark as resolved locally regardless of whether ServiceNow state change succeeded
        # (state change may have been silently skipped if user lacks incident_manager role)
        resolved_state = result.get('state') or '6'
        for ticket in _ticket_store.values():
            if ticket.get('sys_id') == sys_id:
                ticket['state']       = resolved_state
                ticket['state_label'] = _state_label(resolved_state)
                ticket['fix_status']  = 'resolved'
                ticket['fixed_at']    = fixed_at
                break

        return jsonify({
            'sys_id':      sys_id,
            'number':      result.get('number') or '',
            'state':       resolved_state,
            'state_label': _state_label(resolved_state),
            'fixed_at':    fixed_at,
        }), 200

    @dashboard_bp.route('/api/snow/update', methods=['POST'])
    def snow_update():
        try:
            sc = _snow()
        except ImportError as exc:
            return jsonify({'error': str(exc)}), 503
        if not sc.is_configured():
            return jsonify({'error': 'ServiceNow not configured'}), 503

        body   = request.get_json(silent=True) or {}
        sys_id = body.get('sys_id', '').strip()
        if not sys_id:
            return jsonify({'error': 'sys_id is required'}), 400

        update_fields = {}
        if body.get('work_notes'):
            update_fields['work_notes'] = body['work_notes']
        if body.get('state'):
            update_fields['state'] = str(body['state'])
        if body.get('short_description'):
            update_fields['short_description'] = body['short_description']
        if body.get('close_notes'):
            update_fields['close_notes'] = body['close_notes']
        # Auto-add close_notes when resolving (state=6) if not already provided
        if update_fields.get('state') == '6' and not update_fields.get('close_notes'):
            update_fields['close_notes'] = body.get('work_notes', 'Resolved from Log Aggregator dashboard.')
        if not update_fields:
            return jsonify({'error': 'No update fields provided'}), 400

        try:
            result = sc.update_incident(sys_id, update_fields)
        except Exception as exc:
            return jsonify({'error': str(exc)}), 502

        # Sync in-memory ticket store — always use the intended state locally
        # even if ServiceNow state change was silently skipped due to 403
        new_state = update_fields.get('state', '')
        effective_state = result.get('state') or new_state
        for ticket in _ticket_store.values():
            if ticket.get('sys_id') == sys_id:
                if effective_state:
                    ticket['state']       = effective_state
                    ticket['state_label'] = _state_label(effective_state)
                    if effective_state in ('6', '7'):
                        ticket['fix_status'] = 'resolved'
                        ticket['fixed_at']   = _ts_now()
                break

        return jsonify({
            'sys_id':      sys_id,
            'number':      result.get('number') or '',
            'state':       effective_state,
            'state_label': _state_label(effective_state),
            'updated':     True,
        }), 200

    @dashboard_bp.route('/api/snow/tickets', methods=['GET'])
    def snow_tickets():
        return jsonify({'tickets': _ticket_store}), 200


    # ════════════════════════════════════════════════════════════════════════════
    # FIX THIS ERROR — Bedrock diagnosis + remediation engine + ServiceNow update
    # ════════════════════════════════════════════════════════════════════════════

    @dashboard_bp.route('/api/fix-error', methods=['POST'])
    def fix_error():
        """Orchestrate: Bedrock diagnosis → remediation steps → ServiceNow update.

        POST body: { "error": {row dict}, "sys_id": "...", "session_id": "..." }
        Returns:   { success, bedrock_plan, steps, summary, ticket }
        """
        import os as _os, sys as _sys

        body        = request.get_json(silent=True) or {}
        error_row   = body.get('error') or {}
        sys_id      = (body.get('sys_id')     or '').strip()
        session_id  = (body.get('session_id') or '').strip()
        error_code  = str(error_row.get('Error Code',  ''))
        description = str(error_row.get('Description', ''))

        # ── Step 1: Bedrock diagnosis (non-blocking — failure is OK) ──────────
        bedrock_plan = ''
        if BEDROCK_CHAT_AVAILABLE:
            try:
                plan_prompt = (
                    f'Error code {error_code} has been escalated for automated remediation. '
                    f'Description: {description}. '
                    f'List the exact automated fix steps being executed and what success looks like. '
                    f'Be specific and concise.'
                )
                bedrock_plan, _ = generate_error_insight(
                    error_row, plan_prompt, [], session_id
                )
            except Exception as exc:
                bedrock_plan = f'(Bedrock unavailable: {exc})'

        # ── Step 2: Run demo remediation engine ───────────────────────────────
        remediation = _run_remediation_safe(error_code, description, session_id)

        # ── Step 3: Trigger conversion so RESOLVED line appears on dashboard ──
        try:
            run_conversion_outputs()
        except Exception:
            pass

        # ── Step 4: Update or create ServiceNow ticket ────────────────────────
        ticket_result = None
        if _snow_configured():
            try:
                sc        = _snow()
                work_note = _build_work_note(remediation, bedrock_plan)

                if sys_id:
                    # Update existing ticket
                    new_state = '6' if remediation['success'] else '2'
                    extra = {}
                    if remediation['success']:
                        extra = {'close_notes': remediation['summary'],
                                 'work_notes':  work_note}
                    updated = sc.update_incident(sys_id, {
                        'work_notes': work_note,
                        'state':      new_state,
                        **extra
                    })
                    fix_status   = 'resolved' if remediation['success'] else 'in_progress'
                    _state_skipped = updated.get('_state_skipped', False)
                    # If state change was skipped (403), use the intended state locally
                    _real_state  = updated.get('state') or new_state
                    # Build a clear state label — don't expose 403 errors to the user
                    if _state_skipped and remediation['success']:
                        _state_label_str = 'Resolved (work note added — state change requires incident_manager role in ServiceNow)'
                    else:
                        _state_label_str = _state_label(_real_state)
                    ticket_result = {
                        'sys_id':      sys_id,
                        'number':      updated.get('number') or '',
                        'state':       new_state,           # use intended state locally
                        'state_label': _state_label_str,
                        'action':      'updated',
                        'fix_status':  fix_status,
                    }
                    # Sync in-memory store
                    for t in _ticket_store.values():
                        if t.get('sys_id') == sys_id:
                            t['state']       = updated.get('state', new_state)
                            t['state_label'] = _state_label(updated.get('state', new_state))
                            t['fix_status']  = fix_status
                            break
                else:
                    # Look up existing ticket by row key first
                    key = _row_key(error_row)
                    if key in _ticket_store:
                        existing_sys_id = _ticket_store[key]['sys_id']
                        ns = '6' if remediation['success'] else '2'
                        updated = sc.update_incident(existing_sys_id, {
                            'work_notes': work_note, 'state': ns
                        })
                        _ticket_store[key]['fix_status']  = 'resolved' if remediation['success'] else 'in_progress'
                        _ticket_store[key]['state']       = ns
                        _ticket_store[key]['state_label'] = _state_label(ns)
                        ticket_result = {**_ticket_store[key], 'action': 'updated'}
                    else:
                        # Create a fresh ticket with fix notes
                        incident = sc.create_incident_from_row(error_row)
                        sc.update_incident(incident['sys_id'], {'work_notes': work_note})
                        fix_status = 'resolved' if remediation['success'] else 'in_progress'
                        ticket = {
                            'sys_id':      incident.get('sys_id'),
                            'number':      incident.get('number'),
                            'state':       '6' if remediation['success'] else '2',
                            'state_label': 'Resolved' if remediation['success'] else 'In Progress',
                            'fix_status':  fix_status,
                            'created_at':  _ts_now(),
                        }
                        _ticket_store[key] = ticket
                        ticket_result = {'action': 'created', **ticket}
            except Exception as exc:
                ticket_result = {'error': str(exc)}

        # ── Step 5: Notify dummy app to update its scenario state ─────────────
        try:
            import requests as _req
            _port = _os.environ.get('APP_PORT', '5000')
            _req.post(
                f'http://127.0.0.1:{_port}/api/dummy-app/mark-fixed',
                json={
                    'error_code':  error_code,
                    'snow_number': (ticket_result.get('number', '')
                                    if ticket_result and not ticket_result.get('error') else ''),
                    'snow_sys_id': (ticket_result.get('sys_id',  '')
                                    if ticket_result and not ticket_result.get('error') else ''),
                },
                timeout=3,
            )
        except Exception:
            pass

        # If remediation_engine fetched its own Bedrock plan, prefer it
        if remediation.get('bedrock_plan'):
            bedrock_plan = remediation['bedrock_plan']

        return jsonify({
            'success':      remediation['success'],
            'bedrock_plan': bedrock_plan,
            'steps':        remediation['steps'],
            'summary':      remediation['summary'],
            'ticket':       ticket_result,
        }), 200

    return dashboard_bp


def _run_remediation_safe(error_code: str, description: str, session_id: str = '') -> dict:
    """Import and run remediation_engine.run_remediation. Always returns a valid dict."""
    import os as _os, sys as _sys

    # Add DummyApp/ to sys.path so remediation_engine and ssl_remediation_agent are importable
    _dummy_dir = _os.path.abspath(
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', 'DummyApp')
    )
    if _dummy_dir not in _sys.path:
        _sys.path.insert(0, _dummy_dir)

    # Resolve log path for writing RESOLVED lines
    _app_dir  = _os.path.abspath(
        _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..', 'Application')
    )
    _log_path = _os.path.join(_app_dir, 'logs', _os.environ.get('LOG_FILENAME', 'application.log'))

    # Force reload of remediation_engine to bypass stale __pycache__
    try:
        import importlib
        import remediation_engine as _re_mod  # type: ignore
        importlib.reload(_re_mod)
        run_remediation = _re_mod.run_remediation
    except Exception as import_exc:
        return {
            'success':   False,
            'steps':     [{'step': f'Could not import remediation_engine: {import_exc}',
                           'status': 'fail', 'detail': str(import_exc), 'ts': _ts_now()}],
            'summary':   f'Remediation engine import failed: {import_exc}',
            'new_state': 'in_progress',
        }

    # Try calling with session_id first, fall back without if old signature cached
    try:
        return run_remediation(
            error_code, description,
            log_path=_log_path,
            session_id=session_id,
        )
    except TypeError:
        # Old cached version without session_id parameter — call without it
        try:
            return run_remediation(error_code, description, log_path=_log_path)
        except Exception as exc:
            return {
                'success':   False,
                'steps':     [{'step': f'Remediation failed: {exc}',
                               'status': 'fail', 'detail': str(exc), 'ts': _ts_now()}],
                'summary':   f'Automated remediation error: {exc}',
                'new_state': 'in_progress',
            }
    except Exception as exc:
        return {
            'success':   False,
            'steps':     [{'step': f'Remediation failed: {exc}',
                           'status': 'fail', 'detail': str(exc), 'ts': _ts_now()}],
            'summary':   f'Automated remediation error: {exc}',
            'new_state': 'in_progress',
        }



def _build_work_note(remediation: dict, bedrock_plan: str) -> str:
    lines = [f'[Auto-remediation via Log Aggregator — {_ts_now()}]',
             f'Result: {remediation["summary"]}']
    if remediation.get('steps'):
        lines.append('\nSteps taken:')
        for s in remediation['steps'][:8]:
            icon = {'ok': '✓', 'warn': '⚠', 'fail': '✗'}.get(s.get('status', ''), '•')
            lines.append(f'  {icon} {s.get("step", "")}')
            if s.get('detail'):
                lines.append(f'    → {s["detail"]}')
    if bedrock_plan and 'unavailable' not in bedrock_plan:
        lines.append(f'\nBedrock diagnosis:\n{bedrock_plan[:600]}')
    return '\n'.join(lines)


def _ts_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')
