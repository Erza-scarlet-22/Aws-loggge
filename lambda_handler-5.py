# lambda/lambda_handler.py
#
# S3-triggered Lambda: reads raw log from S3, converts to CSV + unique_errors.json,
# writes both to the processed bucket.
#
# Trigger: s3:ObjectCreated:* on raw-logs/ prefix
# Environment variables (set by CloudFormation):
#   RAW_LOGS_BUCKET   – source bucket
#   PROCESSED_BUCKET  – destination bucket
#   PROCESSED_PREFIX  – destination prefix (default: processed/)

import csv
import io
import json
import logging
import os
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import boto3

logger = logging.getLogger()
logger.setLevel(os.getenv('LOG_LEVEL', 'INFO'))

PROCESSED_BUCKET   = os.environ['PROCESSED_BUCKET']
PROCESSED_PREFIX   = os.getenv('PROCESSED_PREFIX', 'processed/')
RAW_LOGS_BUCKET    = os.environ['RAW_LOGS_BUCKET']
# Story 4968/4970: configurable retention — default 90 days
LOG_RETENTION_DAYS = int(os.getenv('LOG_RETENTION_DAYS', '90'))

s3 = boto3.client('s3')


def _cutoff_date() -> date:
    """Return the earliest date rows should be kept (today - LOG_RETENTION_DAYS)."""
    if LOG_RETENTION_DAYS <= 0:
        return date.min
    return date.today() - timedelta(days=LOG_RETENTION_DAYS)

# ── Log parser (inlined so Lambda has zero extra dependencies) ────────────────
_TS_RE       = re.compile(r'\[(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\]')
_API_IP_RE   = re.compile(r'\[(?:INFO|WARNING|ERROR|DEBUG)\]\s+([A-Z]+\s+\S+)\s+IP:')
_STATUS_RE   = re.compile(r'\[(?:INFO|WARNING|ERROR|DEBUG)\]\s+([A-Z]+\s+\S+)\s+Status Code:\s*(\d{3})')
_ERR_CODE_RE = re.compile(r"'error_code'\s*:\s*(\d+)")

def _extract_timestamp(line: str) -> str:
    m = _TS_RE.search(line)
    return m.group(1) if m else ''

def _extract_date(line: str) -> str:
    ts = _extract_timestamp(line)
    return ts[:10] if ts else ''

def _extract_error_details(line: str) -> dict:
    ec = _ERR_CODE_RE.search(line)
    error_code = ec.group(1) if ec else ''
    # Description: everything between ] and the {'error_code': ...} dict
    desc = ''
    bracket_end = line.rfind('] ')
    if bracket_end != -1:
        raw = line[bracket_end + 2:]
        raw = re.sub(r"\s*\{.*\}\s*$", '', raw).strip()
        if raw:
            desc = raw
    return {'error_code': error_code, 'description': desc}

def _parse_row_date(row: Dict) -> date:
    """Parse Date field to a date object for retention comparison."""
    raw = row.get('Date', '')
    try:
        return date.fromisoformat(raw[:10]) if raw else date.min
    except ValueError:
        return date.min


def _parse_log(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    cur_api = cur_code = cur_desc = cur_date = cur_ts = ''

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m_ip = _API_IP_RE.search(line)
        if m_ip:
            cur_api  = m_ip.group(1)
            cur_code = cur_desc = ''
            cur_ts   = _extract_timestamp(line)
            cur_date = _extract_date(line)
            continue

        if '[ERROR]' in line or '[WARNING]' in line:
            ed = _extract_error_details(line)
            if ed['error_code']:  cur_code = ed['error_code']
            if ed['description']: cur_desc = ed['description']
            continue

        m_st = _STATUS_RE.search(line)
        if m_st:
            api_st      = m_st.group(1)
            status_code = m_st.group(2)
            rows.append({
                'Timestamp':   cur_ts   or _extract_timestamp(line),
                'Date':        cur_date or _extract_date(line),
                'Status code': status_code,
                'Error Code':  cur_code,
                'Description': cur_desc or ('Success' if status_code.startswith('2') else 'No error description'),
                'API':         cur_api or api_st,
            })
            cur_api = cur_code = cur_desc = cur_date = cur_ts = ''

    # Story 4970/4968: drop rows outside the retention window
    if LOG_RETENTION_DAYS > 0:
        cutoff = _cutoff_date()
        rows   = [
            r for r in rows
            if _parse_row_date(r) >= cutoff
        ]
    return rows

def _to_csv(rows: List[Dict]) -> str:
    buf = io.StringIO()
    w   = csv.DictWriter(buf, fieldnames=['Timestamp','Date','Status code','Error Code','Description','API'])
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()

def _to_unique_errors_json(rows: List[Dict]) -> str:
    agg: Dict[tuple, dict] = {}
    for row in rows:
        sc = row.get('Status code', '')
        ec = row.get('Error Code',  '')
        if not sc.isdigit() or int(sc) < 400 or not ec:
            continue
        key = (sc, ec, row.get('Description',''), row.get('API',''))
        if key not in agg:
            agg[key] = {'count': 0, 'dates': set(), 'last_seen': ''}
        agg[key]['count'] += 1
        rd = row.get('Date','');    rt = row.get('Timestamp','')
        if rd:
            # Only include dates within the retention window
            try:
                if LOG_RETENTION_DAYS <= 0 or date.fromisoformat(rd[:10]) >= _cutoff_date():
                    agg[key]['dates'].add(rd)
            except ValueError:
                agg[key]['dates'].add(rd)
        if rt and rt > agg[key]['last_seen']: agg[key]['last_seen'] = rt
        elif rd and rd > agg[key]['last_seen']: agg[key]['last_seen'] = rd

    result = [
        {'Status Code': k[0], 'Error Code': k[1], 'Description': k[2], 'API': k[3],
         'Count': m['count'], 'Last Seen': m['last_seen'], 'Dates': sorted(m['dates'])}
        for k, m in sorted(agg.items())
    ]
    return json.dumps(result, indent=2)

# ── Handler ───────────────────────────────────────────────────────────────────

def handler(event, context):
    processed = []
    for record in event.get('Records', []):
        bucket = record['s3']['bucket']['name']
        key    = urllib.parse.unquote_plus(record['s3']['object']['key'])
        logger.info('Processing s3://%s/%s', bucket, key)

        try:
            obj  = s3.get_object(Bucket=bucket, Key=key)
            text = obj['Body'].read().decode('utf-8', errors='replace')
        except Exception as exc:
            logger.error('Failed to read s3://%s/%s: %s', bucket, key, exc)
            continue

        rows = _parse_log(text)
        logger.info('Parsed %d rows from %s', len(rows), key)

        if not rows:
            logger.warning('No rows parsed from %s — skipping', key)
            continue

        # Write CSV
        csv_key = f"{PROCESSED_PREFIX}converted_application_logs.csv"
        try:
            s3.put_object(Bucket=PROCESSED_BUCKET, Key=csv_key,
                          Body=_to_csv(rows).encode('utf-8'),
                          ContentType='text/csv')
            logger.info('CSV written to s3://%s/%s', PROCESSED_BUCKET, csv_key)
        except Exception as exc:
            logger.error('CSV write failed: %s', exc)

        # Write unique_errors.json
        json_key = f"{PROCESSED_PREFIX}unique_errors.json"
        try:
            s3.put_object(Bucket=PROCESSED_BUCKET, Key=json_key,
                          Body=_to_unique_errors_json(rows).encode('utf-8'),
                          ContentType='application/json')
            logger.info('unique_errors.json written to s3://%s/%s', PROCESSED_BUCKET, json_key)
        except Exception as exc:
            logger.error('JSON write failed: %s', exc)

        processed.append({'key': key, 'rows': len(rows)})

    return {'statusCode': 200, 'processed': processed}
