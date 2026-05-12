# Dashboard/dashboard_data_service.py
#
# Reads processed log data from S3 (AWS mode) or local Conversion/ dir (local mode).
# AWS mode  : s3://PROCESSED_BUCKET/processed/unique_errors.json
# Local mode: Conversion/unique_errors.json
#
# ALL env vars are read at CALL TIME using os.environ.get() inside functions.
# Nothing is cached at module import time — this is intentional so ECS env vars
# are always picked up correctly.

import csv
import io
import json
import logging
import os
from datetime import date, timedelta
from typing import Dict, List, Optional

import boto3

_logger = logging.getLogger(__name__)

# ── Field name constants ──────────────────────────────────────────────────────
STATUS_CODE_KEY  = 'Status Code'
ERROR_CODE_KEY   = 'Error Code'
DESCRIPTION_KEY  = 'Description'
API_KEY          = 'API'
COUNT_KEY        = 'Count'
LAST_SEEN_KEY    = 'Last Seen'
JSON_FILENAME    = 'unique_errors.json'
CSV_FILENAME     = 'converted_application_logs.csv'


# ── Environment — ALL read dynamically, never cached at import ────────────────

def _bucket() -> str:
    return os.environ.get('PROCESSED_BUCKET', '')

def _prefix() -> str:
    # Support both env var names
    return os.environ.get('PROCESSED_LOG_PREFIX',
           os.environ.get('PROCESSED_PREFIX', 'processed/'))

def _region() -> str:
    return os.environ.get('AWS_DEFAULT_REGION',
           os.environ.get('AWS_REGION', 'us-east-1'))

def _is_aws() -> bool:
    return bool(os.environ.get('PROCESSED_BUCKET', ''))

def _retention_days() -> int:
    try:
        return int(os.environ.get('LOG_RETENTION_DAYS', '90'))
    except (ValueError, TypeError):
        return 90

def _cutoff() -> Optional[date]:
    days = _retention_days()
    if days <= 0:
        return None
    return date.today() - timedelta(days=days)


# ── S3 helpers ────────────────────────────────────────────────────────────────

def _s3_client():
    return boto3.client('s3', region_name=_region())


def _s3_read(key: str) -> Optional[str]:
    """Read a text object from the processed S3 bucket. Returns None on any error."""
    bucket = _bucket()
    if not bucket:
        _logger.error(
            '[dashboard] PROCESSED_BUCKET is not set. '
            'Env vars with BUCKET/PROCESSED: %s',
            {k: v for k, v in os.environ.items()
             if 'BUCKET' in k.upper() or 'PROCESSED' in k.upper()}
        )
        return None
    full_key = key
    _logger.info('[dashboard] Reading s3://%s/%s', bucket, full_key)
    try:
        resp    = _s3_client().get_object(Bucket=bucket, Key=full_key)
        content = resp['Body'].read().decode('utf-8', errors='replace')
        _logger.info('[dashboard] s3://%s/%s — %d bytes read OK', bucket, full_key, len(content))
        return content
    except Exception as exc:
        msg = str(exc)
        if 'NoSuchKey' in msg or '404' in msg:
            _logger.warning('[dashboard] Not found: s3://%s/%s', bucket, full_key)
        else:
            _logger.error('[dashboard] S3 read failed s3://%s/%s: %s', bucket, full_key, exc)
        return None


def _json_key() -> str:
    return f"{_prefix()}{JSON_FILENAME}"

def _csv_key() -> str:
    return f"{_prefix()}{CSV_FILENAME}"


# ── Read unique_errors.json ───────────────────────────────────────────────────

def _read_json(conversion_dir: str) -> List[dict]:
    """
    Read unique_errors.json from S3 (AWS) or local file (local).
    Returns a list of error entry dicts. Empty list on any failure.
    """
    _logger.info('[dashboard] _read_json — is_aws=%s bucket=%s prefix=%s',
                 _is_aws(), _bucket(), _prefix())

    if _is_aws():
        text = _s3_read(_json_key())
        if text is None:
            _logger.warning('[dashboard] unique_errors.json not found in S3 — '
                            'ensure Lambda has processed at least one log file')
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            _logger.error('[dashboard] Failed to parse unique_errors.json: %s', exc)
            return []
        if not isinstance(data, list):
            _logger.error('[dashboard] unique_errors.json is not a list (got %s)', type(data))
            return []

        # Normalise: older Lambda versions wrote 'Status code' (lowercase c)
        result = []
        for entry in data:
            if isinstance(entry, dict):
                if 'Status code' in entry and STATUS_CODE_KEY not in entry:
                    entry = dict(entry)
                    entry[STATUS_CODE_KEY] = entry.pop('Status code')
                result.append(entry)

        _logger.info('[dashboard] Loaded %d error entries from S3 JSON', len(result))
        return result

    # Local mode — read from Conversion/ directory
    for fname in (JSON_FILENAME, 'unique errors.json'):
        path = os.path.join(conversion_dir, fname)
        if not os.path.exists(path):
            continue
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                _logger.info('[dashboard] Local JSON loaded: %d entries from %s', len(data), path)
                return data
        except Exception as exc:
            _logger.warning('[dashboard] Failed to read %s: %s', path, exc)
    _logger.warning('[dashboard] No unique_errors.json found in %s', conversion_dir)
    return []


# ── Read CSV (for date-range queries) ─────────────────────────────────────────

def _read_csv(conversion_dir: str) -> Optional[List[Dict]]:
    """
    Read the CSV. Returns None if not available (caller falls back to JSON).
    """
    if _is_aws():
        text = _s3_read(_csv_key())
        if text is None:
            return None
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            if 'Status code' in row and STATUS_CODE_KEY not in row:
                row[STATUS_CODE_KEY] = row.pop('Status code')
            rows.append(row)
        _logger.info('[dashboard] CSV loaded: %d rows from S3', len(rows))
        return rows
    else:
        path = os.path.join(conversion_dir, CSV_FILENAME)
        if not os.path.exists(path):
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            _logger.info('[dashboard] CSV loaded: %d rows from local file', len(rows))
            return rows
        except Exception as exc:
            _logger.warning('[dashboard] Failed to read local CSV: %s', exc)
            return None


# ── Date filter resolution ────────────────────────────────────────────────────

def _resolve_date_filters(request_args):
    """
    Returns (date_from, date_to, label).
    Priority: From/To calendar > Dropdown date > Preset button > Default (full window).
    date_from is always capped at the retention cutoff.
    """
    cutoff = _cutoff()
    today  = date.today()

    def _clamp_from(df):
        if cutoff and df:
            return max(df, cutoff)
        if cutoff and df is None:
            return cutoff
        return df

    # From/To calendar
    from_s = request_args.get('from', '').strip()
    to_s   = request_args.get('to',   '').strip()
    if from_s or to_s:
        try:
            df = date.fromisoformat(from_s) if from_s else None
            dt = date.fromisoformat(to_s)   if to_s   else today
            df = _clamp_from(df)
            dt = min(dt, today)
            fl = df.strftime('%d %b %Y') if df else '...'
            return df, dt, f"{fl} → {dt.strftime('%d %b %Y')}"
        except ValueError:
            pass

    # Dropdown date
    date_s = request_args.get('date', '').strip()
    if date_s:
        try:
            dt = min(date.fromisoformat(date_s), today)
            df = _clamp_from(None)
            return df, dt, f"Up to {dt.strftime('%d %b %Y')}"
        except ValueError:
            pass

    # Preset buttons
    preset = request_args.get('preset', '').strip()
    if preset == 'today':
        df = _clamp_from(today)
        return df, today, 'Today'
    if preset == 'week':
        df = _clamp_from(today - timedelta(days=6))
        return df, today, 'Last 7 Days'
    if preset == 'month':
        df = _clamp_from(today - timedelta(days=29))
        return df, today, 'Last 30 Days'

    # Default — full retention window
    df = cutoff  # may be None (no limit)
    return df, today, f'Last {_retention_days()} Days' if cutoff else 'All Time'


# ── Date range filtering ──────────────────────────────────────────────────────

def _in_range(entry: dict, date_from: Optional[date], date_to: Optional[date]) -> bool:
    """Check if an error entry's Last Seen date falls within the given range."""
    ls = entry.get(LAST_SEEN_KEY, '')
    if not ls:
        return True  # no date info — include it
    try:
        entry_date = date.fromisoformat(ls[:10])
    except ValueError:
        return True
    if date_from and entry_date < date_from:
        return False
    if date_to   and entry_date > date_to:
        return False
    return True


def _filter_csv_rows(rows: List[Dict],
                     date_from: Optional[date],
                     date_to:   Optional[date]) -> List[Dict]:
    """Filter CSV rows by date range and reaggregate into error entries."""
    aggregated: Dict[tuple, dict] = {}
    for row in rows:
        sc  = row.get(STATUS_CODE_KEY) or row.get('Status code', '')
        ec  = row.get(ERROR_CODE_KEY, '')
        if not sc or not sc.isdigit() or int(sc) < 400 or not ec:
            continue
        rd = row.get('Date', '')
        try:
            row_date = date.fromisoformat(rd[:10]) if rd else None
        except ValueError:
            row_date = None
        if row_date:
            if date_from and row_date < date_from:
                continue
            if date_to   and row_date > date_to:
                continue
        key = (sc, ec, row.get(DESCRIPTION_KEY, ''), row.get(API_KEY, ''))
        if key not in aggregated:
            aggregated[key] = {'count': 0, 'dates': set(), 'last_seen': ''}
        aggregated[key]['count'] += 1
        rt = row.get('Timestamp', '')
        if rd: aggregated[key]['dates'].add(rd)
        if rt and rt > aggregated[key]['last_seen']:
            aggregated[key]['last_seen'] = rt
        elif rd and rd > aggregated[key]['last_seen']:
            aggregated[key]['last_seen'] = rd

    return [
        {STATUS_CODE_KEY: k[0], ERROR_CODE_KEY: k[1],
         DESCRIPTION_KEY: k[2], API_KEY: k[3],
         COUNT_KEY: m['count'], LAST_SEEN_KEY: m['last_seen'],
         'Dates': sorted(m['dates'])}
        for k, m in sorted(aggregated.items())
    ]


# ── Main data collector ───────────────────────────────────────────────────────

def _collect_errors(conversion_dir: str,
                    date_from: Optional[date],
                    date_to:   Optional[date]) -> List[dict]:
    """
    Collect error entries, applying date filtering.

    Strategy:
    1. Always try to read unique_errors.json first (fast, pre-aggregated).
    2. If a specific date range is requested AND the JSON entries don't have
       enough date granularity, fall back to re-aggregating from the CSV.
    3. Apply retention cutoff so no data older than LOG_RETENTION_DAYS appears.
    """
    # Get the data
    entries = _read_json(conversion_dir)

    if not entries:
        _logger.warning('[dashboard] No error entries found — '
                        'check Lambda has processed a log file and written unique_errors.json')

    # Apply date filter to JSON entries
    if date_from or date_to:
        before  = len(entries)
        entries = [e for e in entries if _in_range(e, date_from, date_to)]
        _logger.info('[dashboard] Date filter: kept %d/%d entries (from=%s to=%s)',
                     len(entries), before, date_from, date_to)

        # If JSON filtering removed everything, try CSV for more granular filtering
        if not entries:
            _logger.info('[dashboard] JSON empty after date filter — trying CSV')
            csv_rows = _read_csv(conversion_dir)
            if csv_rows:
                entries = _filter_csv_rows(csv_rows, date_from, date_to)
                _logger.info('[dashboard] CSV fallback: %d entries', len(entries))

    # Apply retention cutoff
    cutoff = _cutoff()
    if cutoff:
        cutoff_s = cutoff.isoformat()
        before   = len(entries)
        retained = []
        for e in entries:
            ls = e.get(LAST_SEEN_KEY, '')
            # Keep entries with no date, or date within retention window
            if not ls or ls[:10] >= cutoff_s:
                retained.append(e)
        if len(retained) < before:
            _logger.info('[dashboard] Retention filter: kept %d/%d entries (cutoff=%s)',
                         len(retained), before, cutoff_s)
        entries = retained

    _logger.info('[dashboard] Final: %d error entries to return', len(entries))
    return entries


# ── Public API ────────────────────────────────────────────────────────────────

def build_dashboard_payload(conversion_dir: str,
                             run_conversion_outputs,
                             request_args) -> dict:
    """
    Build the complete dashboard JSON payload.
    Called by dashboard_blueprint for every /api/dashboard-data request.
    """
    # In local mode, regenerate conversion outputs before reading
    if not _is_aws():
        _logger.info('[dashboard] Local mode — running conversion outputs')
        run_conversion_outputs()

    _logger.info('[dashboard] build_dashboard_payload — is_aws=%s bucket=%s prefix=%s region=%s',
                 _is_aws(), _bucket(), _prefix(), _region())

    date_from, date_to, filter_label = _resolve_date_filters(request_args)
    entries = _collect_errors(conversion_dir, date_from, date_to)

    # Compute summary stats
    total   = sum(int(e.get(COUNT_KEY, 0)) for e in entries)
    by_sc: Dict[str, int] = {}
    by_api: Dict[str, int] = {}
    for e in entries:
        sc  = str(e.get(STATUS_CODE_KEY, 'Unknown'))
        api = str(e.get(API_KEY, 'Unknown'))
        cnt = int(e.get(COUNT_KEY, 0))
        by_sc[sc]   = by_sc.get(sc, 0)   + cnt
        by_api[api] = by_api.get(api, 0) + cnt

    cutoff = _cutoff()
    return {
        'summary': {
            'uniqueErrorTypes': len(entries),
            'totalErrorEvents': total,
            'statusCodeCount':  len(by_sc),
            'apiCount':         len(by_api),
            'source':           'aws-s3' if _is_aws() else 'local',
        },
        'byStatus': by_sc,
        'byApi':    by_api,
        'rows':     entries,
        'filter': {
            'from':          date_from.isoformat() if date_from else None,
            'to':            date_to.isoformat()   if date_to   else None,
            'label':         filter_label,
            'retentionDays': _retention_days(),
            'cutoffDate':    cutoff.isoformat() if cutoff else None,
            'today':         date.today().isoformat(),
        },
    }
