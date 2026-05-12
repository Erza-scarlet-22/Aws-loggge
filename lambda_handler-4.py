# lambda/lambda_handler.py
#
# S3-triggered Lambda: reads raw log from S3, converts to CSV + unique_errors.json,
# writes both to the processed bucket.
#
# KEY FIX NOTES
# -------------
# 1. All field names use CONSISTENT capitalisation throughout:
#    CSV fields:  Timestamp, Date, Status Code, Error Code, Description, API
#    JSON fields: Status Code, Error Code, Description, API, Count, Last Seen, Dates
#    (Status Code always has capital C — was 'Status code' in the old version)
#
# 2. MERGE BEHAVIOUR: Lambda reads the existing CSV/JSON from the processed
#    bucket FIRST, merges the new rows, then writes back. This means every
#    S3 trigger adds to the history rather than replacing it.
#
# 3. ALL rows are included in the CSV (not just errors).
#    Only rows with Error Code >= 400 appear in unique_errors.json.
#
# 4. Retention is enforced at write time — rows older than LOG_RETENTION_DAYS
#    are dropped before writing.

import csv
import io
import json
import logging
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

PROCESSED_BUCKET   = os.environ['PROCESSED_BUCKET']
PROCESSED_PREFIX   = os.getenv('PROCESSED_PREFIX', 'processed/')
RAW_LOGS_BUCKET    = os.environ['RAW_LOGS_BUCKET']
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '90'))

s3 = boto3.client('s3')

# ── CSV field names — single source of truth ─────────────────────────────────
CSV_FIELDS = ['Timestamp', 'Date', 'Status Code', 'Error Code', 'Description', 'API']

def _cutoff_date() -> date:
    if LOG_RETENTION_DAYS <= 0:
        return date.min
    return date.today() - timedelta(days=LOG_RETENTION_DAYS)

def _row_date(row: Dict) -> date:
    raw = row.get('Date', '')
    try:
        return date.fromisoformat(raw[:10]) if raw else date.min
    except ValueError:
        return date.min

# ── Log parser ────────────────────────────────────────────────────────────────
_TS_RE       = re.compile(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]')
_API_IP_RE   = re.compile(r'\[(?:INFO|WARNING|ERROR|DEBUG)\]\s+([A-Z]+\s+\S+)\s+IP:')
_STATUS_RE   = re.compile(r'\[(?:INFO|WARNING|ERROR|DEBUG)\]\s+([A-Z]+\s+\S+)\s+Status Code:\s*(\d{3})')
_ERR_CODE_RE = re.compile(r"'error_code'\s*:\s*(\d+)")

def _ts(line):
    m = _TS_RE.search(line)
    return m.group(1) if m else ''

def _dt(line):
    ts = _ts(line)
    return ts[:10] if ts else ''

def _err(line):
    ec = _ERR_CODE_RE.search(line)
    error_code = ec.group(1) if ec else ''
    desc = ''
    bi = line.rfind('] ')
    if bi != -1:
        raw = re.sub(r"\s*\{.*\}\s*$", '', line[bi+2:]).strip()
        if raw:
            desc = raw
    return error_code, desc

def parse_log(text: str) -> List[Dict[str, str]]:
    """
    Parse the 3-line log format into a list of row dicts.
    Every request that produces a Status Code line becomes a row.
    All field names use consistent capitalisation (Status Code with capital C).
    """
    rows = []
    cur_api = cur_code = cur_desc = cur_date = cur_ts = ''

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m_ip = _API_IP_RE.search(line)
        if m_ip:
            cur_api  = m_ip.group(1)
            cur_code = cur_desc = ''
            cur_ts   = _ts(line)
            cur_date = _dt(line)
            continue

        if '[ERROR]' in line or '[WARNING]' in line:
            ec, desc = _err(line)
            if ec:   cur_code = ec
            if desc: cur_desc = desc
            continue

        m_st = _STATUS_RE.search(line)
        if m_st:
            api_st      = m_st.group(1)
            status_code = m_st.group(2)
            rows.append({
                'Timestamp':   cur_ts   or _ts(line),
                'Date':        cur_date or _dt(line),
                'Status Code': status_code,        # ← capital C, consistent
                'Error Code':  cur_code,
                'Description': cur_desc or (
                    'Success' if status_code.startswith('2') else 'No description'
                ),
                'API':         cur_api or api_st,
            })
            cur_api = cur_code = cur_desc = cur_date = cur_ts = ''

    # Apply retention filter
    if LOG_RETENTION_DAYS > 0:
        cutoff = _cutoff_date()
        rows   = [r for r in rows if _row_date(r) >= cutoff]

    return rows

# ── S3 merge helpers ──────────────────────────────────────────────────────────

def _read_existing_csv(key: str) -> List[Dict]:
    """Read existing CSV from processed bucket. Returns [] if not found."""
    try:
        resp = s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)
        text = resp['Body'].read().decode('utf-8', errors='replace')
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            # Normalise: handle both 'Status code' and 'Status Code'
            if 'Status code' in row and 'Status Code' not in row:
                row['Status Code'] = row.pop('Status code')
            rows.append(row)
        return rows
    except Exception:
        return []

def _read_existing_json(key: str) -> List[Dict]:
    """Read existing unique_errors.json from processed bucket. Returns [] if not found."""
    try:
        resp = s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)
        data = json.loads(resp['Body'].read().decode('utf-8', errors='replace'))
        return data if isinstance(data, list) else []
    except Exception:
        return []

# ── Writers ───────────────────────────────────────────────────────────────────

def _to_csv(rows: List[Dict]) -> str:
    buf = io.StringIO(newline='')
    w   = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction='ignore',
                         lineterminator='\n')
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()

def _to_unique_errors_json(rows: List[Dict]) -> str:
    """
    Build unique_errors.json from rows.
    Only includes rows with Status Code >= 400 AND a non-empty Error Code.
    Keys use consistent capitalisation matching dashboard_data_service.py.
    """
    agg: Dict[tuple, dict] = {}
    cutoff_s = _cutoff_date().isoformat()

    for row in rows:
        sc = row.get('Status Code', row.get('Status code', ''))
        ec = row.get('Error Code', '')
        if not sc or not sc.isdigit() or int(sc) < 400 or not ec:
            continue

        key = (sc, ec, row.get('Description', ''), row.get('API', ''))
        if key not in agg:
            agg[key] = {'count': 0, 'dates': set(), 'last_seen': ''}

        agg[key]['count'] += 1

        rd = row.get('Date', '')
        rt = row.get('Timestamp', '')
        if rd:
            try:
                if LOG_RETENTION_DAYS <= 0 or rd[:10] >= cutoff_s:
                    agg[key]['dates'].add(rd)
            except Exception:
                agg[key]['dates'].add(rd)

        if rt and rt > agg[key]['last_seen']:
            agg[key]['last_seen'] = rt
        elif rd and rd > agg[key]['last_seen']:
            agg[key]['last_seen'] = rd

    result = [
        {
            'Status Code': k[0],
            'Error Code':  k[1],
            'Description': k[2],
            'API':         k[3],
            'Count':       m['count'],
            'Last Seen':   m['last_seen'],
            'Dates':       sorted(m['dates']),
        }
        for k, m in sorted(agg.items())
    ]
    return json.dumps(result, indent=2)

def _merge_rows(existing: List[Dict], new_rows: List[Dict]) -> List[Dict]:
    """
    Merge new rows into existing rows.
    Deduplicates by (Timestamp, API) — same request won't be double-counted
    if the same log file is reprocessed.
    Applies retention cutoff to the merged result.
    """
    seen = {(r.get('Timestamp',''), r.get('API','')) for r in existing}
    for r in new_rows:
        k = (r.get('Timestamp',''), r.get('API',''))
        if k not in seen:
            existing.append(r)
            seen.add(k)

    # Apply retention to merged set
    if LOG_RETENTION_DAYS > 0:
        cutoff = _cutoff_date()
        existing = [r for r in existing if _row_date(r) >= cutoff]

    return existing

# ── Handler ───────────────────────────────────────────────────────────────────

def handler(event, context):
    processed = []
    csv_key  = f"{PROCESSED_PREFIX}converted_application_logs.csv"
    json_key = f"{PROCESSED_PREFIX}unique_errors.json"

    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key    = urllib.parse.unquote_plus(record['s3']['object']['key'])
        logger.info('Processing s3://%s/%s', bucket, key)

        # Read raw log
        try:
            obj  = s3.get_object(Bucket=bucket, Key=key)
            text = obj['Body'].read().decode('utf-8', errors='replace')
        except Exception as exc:
            logger.error('Failed to read s3://%s/%s: %s', bucket, key, exc)
            continue

        # Parse new rows
        new_rows = parse_log(text)
        logger.info('Parsed %d rows from %s', len(new_rows), key)

        if not new_rows:
            logger.warning('No rows parsed from %s', key)
            logger.info('First 500 chars of log: %s', text[:500])
            continue

        # Read existing processed data and merge
        existing_rows = _read_existing_csv(csv_key)
        logger.info('Existing CSV rows: %d', len(existing_rows))

        all_rows = _merge_rows(existing_rows, new_rows)
        logger.info('Merged total rows: %d', len(all_rows))

        # Write merged CSV
        try:
            s3.put_object(
                Bucket=PROCESSED_BUCKET, Key=csv_key,
                Body=_to_csv(all_rows).encode('utf-8'),
                ContentType='text/csv'
            )
            logger.info('CSV written: %d rows → s3://%s/%s', len(all_rows), PROCESSED_BUCKET, csv_key)
        except Exception as exc:
            logger.error('CSV write failed: %s', exc)

        # Write unique_errors.json from the full merged set
        try:
            s3.put_object(
                Bucket=PROCESSED_BUCKET, Key=json_key,
                Body=_to_unique_errors_json(all_rows).encode('utf-8'),
                ContentType='application/json'
            )
            logger.info('unique_errors.json written → s3://%s/%s', PROCESSED_BUCKET, json_key)
        except Exception as exc:
            logger.error('JSON write failed: %s', exc)

        processed.append({'key': key, 'new_rows': len(new_rows), 'total_rows': len(all_rows)})

    return {'statusCode': 200, 'processed': processed}
