# Dashboard/dashboard_data_service.py
#
# AWS mode  : reads unique_errors.json + CSV from S3 processed bucket.
# Local mode: reads from Conversion/ directory (unchanged behaviour).
#
# The switch is entirely driven by the PROCESSED_BUCKET env var.

import csv, io, json, logging, os
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
import boto3

_logger = logging.getLogger(__name__)

STATUS_CODE_KEY = 'Status Code'
ERROR_CODE_KEY  = 'Error Code'
DESCRIPTION_KEY = 'Description'
API_KEY         = 'API'
COUNT_KEY       = 'Count'
LAST_SEEN_KEY   = 'Last Seen'
UNIQUE_ERRORS_JSON_FILENAME = 'unique_errors.json'
LEGACY_UNIQUE_ERRORS_JSON_FILENAME = 'unique errors.json'

_PROCESSED_BUCKET = os.getenv('PROCESSED_BUCKET', '')
_PROCESSED_PREFIX = os.getenv('PROCESSED_LOG_PREFIX', 'processed/')
_AWS_REGION       = os.getenv('AWS_DEFAULT_REGION', os.getenv('AWS_REGION', 'us-east-1'))
_IS_AWS           = bool(_PROCESSED_BUCKET)

# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3():
    return boto3.client('s3', region_name=_AWS_REGION)

def _s3_get_text(key: str) -> Optional[str]:
    """Download an S3 object and return its content as a string, or None on error."""
    try:
        resp = _s3().get_object(Bucket=_PROCESSED_BUCKET, Key=key)
        return resp['Body'].read().decode('utf-8', errors='replace')
    except Exception as exc:
        _logger.warning('[dashboard_data] S3 GET %s failed: %s', key, exc)
        return None

def _s3_json_key() -> str:
    return f"{_PROCESSED_PREFIX}{UNIQUE_ERRORS_JSON_FILENAME}"

def _s3_csv_key() -> str:
    return f"{_PROCESSED_PREFIX}converted_application_logs.csv"

# ── Readers ───────────────────────────────────────────────────────────────────

def _read_unique_errors_data(conversion_dir: str) -> List[dict]:
    if _IS_AWS:
        text = _s3_get_text(_s3_json_key())
        if text is None:
            return []
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except Exception:
            return []

    for fname in (UNIQUE_ERRORS_JSON_FILENAME, LEGACY_UNIQUE_ERRORS_JSON_FILENAME):
        path = os.path.join(conversion_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception:
            continue
    return []

# ── Date filter helpers (unchanged logic) ─────────────────────────────────────

# ── Retention configuration (Story 4968 / 4970) ──────────────────────────────
# Reads LOG_RETENTION_DAYS from environment — same var used by log_to_csv_service.
# The dashboard enforces this: dates beyond the retention window are blocked.
import os as _os
_RETENTION_DAYS = int(_os.environ.get("LOG_RETENTION_DAYS", "90"))


def _retention_cutoff() -> date:
    """Earliest date visible in the dashboard (today - LOG_RETENTION_DAYS)."""
    if _RETENTION_DAYS <= 0:
        return date.min
    return date.today() - timedelta(days=_RETENTION_DAYS)


def _resolve_date_filters(request_args):
    date_from_str = request_args.get('from')
    date_to_str   = request_args.get('to')
    preset        = request_args.get('preset')
    today         = date.today()
    cutoff        = _retention_cutoff()

    if preset == 'today':
        return max(today, cutoff), today, 'Today'
    if preset == 'week':
        return max(today - timedelta(days=6), cutoff), today, 'Last 7 Days'
    if preset == 'month':
        return max(today - timedelta(days=29), cutoff), today, 'Last 30 Days'
    if preset == 'quarter':
        # Story 4970: cap at retention boundary (e.g. 90 days max)
        return cutoff, today, f'Last {_RETENTION_DAYS} Days (max retention)'

    try:
        date_from = date.fromisoformat(date_from_str) if date_from_str else None
        date_to   = date.fromisoformat(date_to_str)   if date_to_str   else None
    except ValueError:
        date_from = date_to = None

    # Story 4970 AC3: block dates beyond retention window
    if date_from and cutoff != date.min:
        date_from = max(date_from, cutoff)

    if date_from or date_to:
        fl = date_from.isoformat() if date_from else '...'
        tl = date_to.isoformat()   if date_to   else '...'
        return date_from, date_to, f'{fl} to {tl}'

    # Default: show within retention window only
    if cutoff != date.min:
        return cutoff, today, f'Last {_RETENTION_DAYS} Days'
    return None, None, 'All Time'

def _row_is_in_range(row_date, date_from, date_to):
    if date_from and row_date < date_from: return False
    if date_to   and row_date > date_to:   return False
    return True

def _update_aggregated_error(aggregated, row, row_date_str, row_timestamp_str):
    key = (row['Status code'], row[ERROR_CODE_KEY], row[DESCRIPTION_KEY], row[API_KEY])
    if key not in aggregated:
        aggregated[key] = {'count': 0, 'dates': set(), 'last_seen': ''}
    aggregated[key]['count'] += 1
    if row_date_str:
        # Only add date if within the retention window
        cutoff_s = _retention_cutoff().isoformat()
        if _RETENTION_DAYS <= 0 or row_date_str >= cutoff_s:
            aggregated[key]['dates'].add(row_date_str)
    if row_timestamp_str and row_timestamp_str > aggregated[key]['last_seen']:
        aggregated[key]['last_seen'] = row_timestamp_str
    elif row_date_str and row_date_str > aggregated[key]['last_seen']:
        aggregated[key]['last_seen'] = row_date_str

def _serialize_aggregated_errors(aggregated):
    return [
        {STATUS_CODE_KEY: k[0], ERROR_CODE_KEY: k[1], DESCRIPTION_KEY: k[2],
         API_KEY: k[3], COUNT_KEY: m['count'], LAST_SEEN_KEY: m['last_seen'],
         'Dates': sorted(m['dates'])}
        for k, m in sorted(aggregated.items())
        if k[0].isdigit() and int(k[0]) >= 400 and k[1]
    ]

def _collect_unique_errors(conversion_dir, date_from, date_to):
    if not (date_from or date_to):
        # Even without an explicit date filter, apply retention cutoff
        # so stale entries don't appear if JSON was written before purge ran
        raw = _read_unique_errors_data(conversion_dir)
        if _RETENTION_DAYS <= 0:
            return raw
        cutoff    = _retention_cutoff()
        cutoff_s  = cutoff.isoformat()
        filtered  = []
        for entry in raw:
            # Filter dates list to only within retention window
            old_dates = entry.get('Dates', [])
            new_dates = [d for d in old_dates if d >= cutoff_s]
            last_seen = entry.get(LAST_SEEN_KEY, '')
            # Skip entries whose last occurrence is before the cutoff
            if last_seen and last_seen[:10] < cutoff_s:
                continue
            entry = dict(entry)  # don't mutate the original
            entry['Dates'] = new_dates
            filtered.append(entry)
        return filtered

    # Date-filtered path: read CSV
    if _IS_AWS:
        csv_text = _s3_get_text(_s3_csv_key())
        if csv_text is None:
            return _read_unique_errors_data(conversion_dir)
        reader = csv.DictReader(io.StringIO(csv_text))
    else:
        csv_path = os.path.join(conversion_dir, 'converted_application_logs.csv')
        if not os.path.exists(csv_path):
            return _read_unique_errors_data(conversion_dir)
        reader = csv.DictReader(open(csv_path, 'r', encoding='utf-8'))

    aggregated = {}
    for row in reader:
        row_date_str      = row.get('Date', '')
        row_timestamp_str = row.get('Timestamp', '')
        try:
            row_date = date.fromisoformat(row_date_str)
        except ValueError:
            continue
        if not _row_is_in_range(row_date, date_from, date_to):
            continue
        _update_aggregated_error(aggregated, row, row_date_str, row_timestamp_str)

    return _serialize_aggregated_errors(aggregated)

# ── Public API ────────────────────────────────────────────────────────────────

def build_dashboard_payload(conversion_dir, run_conversion_outputs, request_args):
    """
    AWS mode  : does NOT call run_conversion_outputs (Lambda handles it).
                Reads directly from S3 processed bucket.
    Local mode: triggers conversion first, then reads from Conversion/.
    """
    if not _IS_AWS:
        run_conversion_outputs()

    date_from, date_to, filter_label = _resolve_date_filters(request_args)
    unique_errors = _collect_unique_errors(conversion_dir, date_from, date_to)

    total_errors = sum(item.get(COUNT_KEY, 0) for item in unique_errors)
    by_status, by_api = {}, {}
    for item in unique_errors:
        sc  = str(item.get(STATUS_CODE_KEY, 'Unknown'))
        api = str(item.get(API_KEY, 'Unknown'))
        cnt = int(item.get(COUNT_KEY, 0))
        by_status[sc]  = by_status.get(sc, 0)  + cnt
        by_api[api]    = by_api.get(api, 0)     + cnt

    return {
        'summary': {
            'uniqueErrorTypes': len(unique_errors),
            'totalErrorEvents': total_errors,
            'statusCodeCount':  len(by_status),
            'apiCount':         len(by_api),
            'source':           'aws-s3' if _IS_AWS else 'local',
        },
        'byStatus': by_status,
        'byApi':    by_api,
        'rows':     unique_errors,
        'filter': {
            'from':           date_from.isoformat() if date_from else None,
            'to':             date_to.isoformat()   if date_to   else None,
            'label':          filter_label,
            'retentionDays':  _RETENTION_DAYS,
            'cutoffDate':     _retention_cutoff().isoformat() if _RETENTION_DAYS > 0 else None,
        },
    }
