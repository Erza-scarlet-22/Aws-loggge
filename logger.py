# Application/logger.py
#
# Structured logging for the Log Aggregator application.
#
# AWS mode  (RAW_LOGS_BUCKET env var set by ECS task definition):
#   Writes ONLY to stdout. CloudWatch Logs captures stdout automatically.
#   No local file created — S3 upload is handled by app.py after_request.
#
# Local mode (RAW_LOGS_BUCKET absent):
#   Writes to both local log file AND stdout, exactly as before.
#   Log file path is resolved relative to THIS file so it works regardless
#   of which directory Flask is started from.
#
# Log format (must match Conversion/log_parser.py regex):
#   [YYYY-MM-DDTHH:MM:SS] [LEVEL] <message>
#
# Public API:
#   info(message, data=None)
#   error(message, data=None, exc_info=False)
#   warn(message, data=None)
#   debug(message, data=None)

import logging
import os

# ── AWS / local mode detection ────────────────────────────────────────────────
IS_AWS = bool(os.environ.get("RAW_LOGS_BUCKET", ""))

# Load .env only in local mode (dotenv not needed / not present in containers)
if not IS_AWS:
    try:
        from dotenv import load_dotenv
        _env_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", ".env")
        )
        load_dotenv(_env_path)
    except ImportError:
        pass

# ── Config ────────────────────────────────────────────────────────────────────
LOGS_DIRECTORY = os.environ.get("LOGS_DIRECTORY", "logs")
LOG_FILENAME   = os.environ.get("LOG_FILENAME",   "application.log")
LOG_LEVEL_STR  = os.environ.get("LOG_LEVEL",      "INFO").upper()

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG, "INFO": logging.INFO,
    "WARNING": logging.WARNING, "WARN": logging.WARNING,
    "ERROR": logging.ERROR, "CRITICAL": logging.CRITICAL,
}
_LOG_LEVEL = _LEVEL_MAP.get(LOG_LEVEL_STR, logging.INFO)

# ── Log file path (local mode only) ──────────────────────────────────────────
# Resolve relative to THIS file so the path is always correct regardless of
# the working directory when Flask is started.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_LOG_DIR  = os.path.join(_THIS_DIR, LOGS_DIRECTORY)
_LOG_FILE = os.path.join(_LOG_DIR, LOG_FILENAME)

# ── Handlers ──────────────────────────────────────────────────────────────────
_fmt     = logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s",
                             datefmt="%Y-%m-%dT%H:%M:%S")
_stdout  = logging.StreamHandler()
_stdout.setFormatter(_fmt)
_handlers = [_stdout]

if not IS_AWS:
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        _fh = logging.FileHandler(_LOG_FILE, encoding="utf-8")
        _fh.setFormatter(_fmt)
        _handlers.append(_fh)
    except OSError as _e:
        print(f"[logger] WARNING: cannot create log file {_LOG_FILE}: {_e}")

logging.basicConfig(level=_LOG_LEVEL, handlers=_handlers)

# Silence noisy libraries
for _lib in ("urllib3", "requests", "botocore", "boto3", "s3transfer"):
    logging.getLogger(_lib).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info(
    "[logger] mode=%s level=%s output=%s",
    "aws" if IS_AWS else "local",
    LOG_LEVEL_STR,
    "stdout-only" if IS_AWS else _LOG_FILE,
)


# ── Public API ────────────────────────────────────────────────────────────────

def info(message: str, data=None) -> None:
    logger.info("%s %s", message, data) if data else logger.info("%s", message)

def error(message: str, data=None, exc_info: bool = False) -> None:
    if data:
        logger.error("%s %s", message, data, exc_info=exc_info)
    else:
        logger.error("%s", message, exc_info=exc_info)

def warn(message: str, data=None) -> None:
    logger.warning("%s %s", message, data) if data else logger.warning("%s", message)

def debug(message: str, data=None) -> None:
    logger.debug("%s %s", message, data) if data else logger.debug("%s", message)
