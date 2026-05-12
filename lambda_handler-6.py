# lambda/lambda_handler.py
#
# S3-triggered Lambda: reads raw log from S3, merges new rows into the
# existing processed CSV + unique_errors.json, then writes both back.
#
# TRIGGER: s3:ObjectCreated:* on prefix raw-logs/
#
# ENVIRONMENT VARIABLES (CloudFormation injects these):
#   PROCESSED_BUCKET  — destination bucket for CSV + JSON
#   RAW_LOGS_BUCKET   — source bucket (for merge read-back)
#   PROCESSED_PREFIX  — destination prefix (default: processed/)
#   LOG_RETENTION_DAYS — number of days to retain (default: 90)
#
# KEY DESIGN NOTES
# ----------------
# - Every file uploaded to raw-logs/ triggers this Lambda independently.
# - We READ the existing processed/converted_application_logs.csv first,
#   MERGE the new rows in (deduplicating by Timestamp+API), then WRITE back.
# - Same approach for unique_errors.json.
# - This means the processed files accumulate history across all uploads.
# - All field names use "Status Code" (capital C) consistently.

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
logger.setLevel(logging.INFO)

PROCESSED_BUCKET   = os.environ['PROCESSED_BUCKET']
PROCESSED_PREFIX   = os.getenv('PROCESSED_PREFIX', 'processed/')
RAW_LOGS_BUCKET    = os.environ['RAW_LOGS_BUCKET']
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '90'))

s3 = boto3.client('s3')

# ── Field names — single source of truth ─────────────────────────────────────
CSV_FIELDS = ['Timestamp', 'Date', 'Status Code', 'Error Code', 'Description', 'API']

# ── Retention ─────────────────────────────────────────────────────────────────
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
    m = _TS_RE.search(line); return m.group(1) if m else ''

def _dt(line):
    ts = _ts(line); return ts[:10] if ts else ''

def _err(line):
    ec   = _ERR_CODE_RE.search(line)
    code = ec.group(1) if ec else ''
    desc = ''
    bi   = line.rfind('] ')
    if bi != -1:
        raw = re.sub(r"\s*\{.*\}\s*$", '', line[bi+2:]).strip()
        if raw: desc = raw
    return code, desc

def parse_log(text: str) -> List[Dict]:
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
            api_st = m_st.group(1)
            sc     = m_st.group(2)
            rows.append({
                'Timestamp':   cur_ts   or _ts(line),
                'Date':        cur_date or _dt(line),
                'Status Code': sc,
                'Error Code':  cur_code,
                'Description': cur_desc or ('Success' if sc.startswith('2') else 'No description'),
                'API':         cur_api  or api_st,
            })
            cur_api = cur_code = cur_desc = cur_date = cur_ts = ''

    logger.info('parse_log: %d lines input → %d rows parsed', len(text.splitlines()), len(rows))

    if LOG_RETENTION_DAYS > 0:
        before = len(rows)
        cutoff = _cutoff_date()
        rows   = [r for r in rows if _row_date(r) >= cutoff]
        logger.info('Retention filter: kept %d/%d rows (cutoff=%s)', len(rows), before, cutoff)

    return rows

# ── S3 read helpers ───────────────────────────────────────────────────────────
def _read_existing_csv() -> List[Dict]:
    key = f"{PROCESSED_PREFIX}converted_application_logs.csv"
    try:
        resp = s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)
        text = resp['Body'].read().decode('utf-8', errors='replace')
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            # Normalise old 'Status code' (lowercase c) from previous runs
            if 'Status code' in row and 'Status Code' not in row:
                row['Status Code'] = row.pop('Status code')
            rows.append(row)
        logger.info('Read %d existing CSV rows from processed bucket', len(rows))
        return rows
    except s3.exceptions.NoSuchKey:
        logger.info('No existing CSV — starting fresh')
        return []
    except Exception as exc:
        logger.warning('Could not read existing CSV: %s — starting fresh', exc)
        return []

def _read_existing_json() -> List[Dict]:
    key = f"{PROCESSED_PREFIX}unique_errors.json"
    try:
        resp = s3.get_object(Bucket=PROCESSED_BUCKET, Key=key)
        data = json.loads(resp['Body'].read().decode('utf-8', errors='replace'))
        if not isinstance(data, list):
            return []
        # Normalise capitalisation
        normalised = []
        for e in data:
            if 'Status code' in e and 'Status Code' not in e:
                e = dict(e)
                e['Status Code'] = e.pop('Status code')
            normalised.append(e)
        logger.info('Read %d existing JSON entries from processed bucket', len(normalised))
        return normalised
    except s3.exceptions.NoSuchKey:
        logger.info('No existing unique_errors.json — starting fresh')
        return []
    except Exception as exc:
        logger.warning('Could not read existing JSON: %s — starting fresh', exc)
        return []

# ── Merge helpers ─────────────────────────────────────────────────────────────
def _merge_rows(existing: List[Dict], new_rows: List[Dict]) -> List[Dict]:
    """Merge new rows into existing. Deduplicates by (Timestamp, API)."""
    seen = {(r.get('Timestamp', ''), r.get('API', '')) for r in existing}
    added = 0
    for r in new_rows:
        k = (r.get('Timestamp', ''), r.get('API', ''))
        if k not in seen:
            existing.append(r)
            seen.add(k)
            added += 1
    logger.info('Merge: %d new rows added (dedup removed %d duplicates)',
                added, len(new_rows) - added)
    # Apply retention to merged set
    if LOG_RETENTION_DAYS > 0:
        cutoff   = _cutoff_date()
        existing = [r for r in existing if _row_date(r) >= cutoff]
    return existing

# ── Writers ───────────────────────────────────────────────────────────────────
def _to_csv(rows: List[Dict]) -> bytes:
    buf = io.StringIO(newline='')
    w   = csv.DictWriter(buf, fieldnames=CSV_FIELDS,
                         extrasaction='ignore', lineterminator='\n')
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().encode('utf-8')

def _build_unique_errors(rows: List[Dict]) -> List[Dict]:
    """
    Build the unique_errors list from ALL merged rows.
    Only includes rows with Status Code >= 400 AND non-empty Error Code.
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
    logger.info('_build_unique_errors: %d unique error types from %d rows', len(result), len(rows))
    return result

def _write_s3(key: str, body: bytes, content_type: str) -> bool:
    try:
        s3.put_object(Bucket=PROCESSED_BUCKET, Key=key,
                      Body=body, ContentType=content_type)
        logger.info('Wrote %d bytes → s3://%s/%s', len(body), PROCESSED_BUCKET, key)
        return True
    except Exception as exc:
        logger.error('FAILED to write s3://%s/%s: %s', PROCESSED_BUCKET, key, exc)
        return False

# ── Handler ───────────────────────────────────────────────────────────────────
def handler(event, context):
    logger.info('Lambda triggered with %d record(s)', len(event.get('Records', [])))

    csv_key  = f"{PROCESSED_PREFIX}converted_application_logs.csv"
    json_key = f"{PROCESSED_PREFIX}unique_errors.json"

    # Read existing processed data ONCE at the start
    existing_rows   = _read_existing_csv()
    # (We rebuild JSON from the merged CSV so no need to read existing JSON)

    total_new = 0

    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key    = urllib.parse.unquote_plus(record['s3']['object']['key'])
        logger.info('Processing s3://%s/%s', bucket, key)

        # Read the raw log file
        try:
            obj  = s3.get_object(Bucket=bucket, Key=key)
            text = obj['Body'].read().decode('utf-8', errors='replace')
            logger.info('Read %d bytes from s3://%s/%s', len(text), bucket, key)
        except Exception as exc:
            logger.error('Failed to read s3://%s/%s: %s', bucket, key, exc)
            continue

        # Parse
        new_rows = parse_log(text)
        if not new_rows:
            logger.warning('No rows parsed from %s', key)
            logger.info('First 300 chars: %s', text[:300])
            continue

        # Merge into existing
        existing_rows = _merge_rows(existing_rows, new_rows)
        total_new += len(new_rows)

    if total_new == 0:
        logger.warning('No new rows parsed from any record — nothing written')
        return {'statusCode': 200, 'newRows': 0}

    # Write merged CSV
    csv_ok = _write_s3(csv_key, _to_csv(existing_rows), 'text/csv')

    # Build and write unique_errors.json from the FULL merged set
    unique_errors = _build_unique_errors(existing_rows)
    json_body     = json.dumps(unique_errors, indent=2).encode('utf-8')
    json_ok       = _write_s3(json_key, json_body, 'application/json')

    logger.info('Done — %d total rows, %d unique error types, CSV=%s, JSON=%s',
                len(existing_rows), len(unique_errors),
                'OK' if csv_ok else 'FAILED',
                'OK' if json_ok else 'FAILED')

    return {
        'statusCode':    200,
        'newRows':       total_new,
        'totalRows':     len(existing_rows),
        'uniqueErrors':  len(unique_errors),
        'csvWritten':    csv_ok,
        'jsonWritten':   json_ok,
    }
