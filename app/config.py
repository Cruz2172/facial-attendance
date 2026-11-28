import os
from datetime import timedelta

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


# ---------------------------------------------------------------------------
# Small env helpers (never crash on a malformed value, fall back to default)
# ---------------------------------------------------------------------------
def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None or value.strip() == '':
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _normalise_db_url(url):
    """Heroku/Render style 'postgres://' is rejected by SQLAlchemy 2.x."""
    if url and url.startswith('postgres://'):
        return 'postgresql://' + url[len('postgres://'):]
    return url


# Multi-image enrollment: the capture page guides the student through
# ENROLL_CAPTURE_COUNT poses; the server needs at least ENROLL_MIN_IMAGES good
# photos and accepts at most ENROLL_MAX_IMAGES (7 = number of guided poses).
_ENROLL_MIN = max(1, _env_int('ENROLL_MIN_IMAGES', 5))
_ENROLL_MAX = max(_ENROLL_MIN, min(7, _env_int('ENROLL_MAX_IMAGES', 7)))
_ENROLL_COUNT = min(max(_env_int('ENROLL_CAPTURE_COUNT', 5), _ENROLL_MIN), _ENROLL_MAX)

APP_ENV = (os.environ.get('APP_ENV') or os.environ.get('FLASK_ENV') or 'production').strip().lower()
IS_PRODUCTION = APP_ENV == 'production'

_DEV_SECRET = 'dev-only-insecure-secret-key-change-me'
_DATABASE_URL = _normalise_db_url(os.environ.get('DATABASE_URL'))
_SQLITE_DEFAULT = 'sqlite:///' + os.path.join(BASE_DIR, 'faceattend.db')


class Config:
    APP_ENV = APP_ENV
    IS_PRODUCTION = IS_PRODUCTION

    # SECRET_KEY must be identical in every worker process and across restarts.
    # In production it is REQUIRED (validated in create_app); in development a
    # fixed fallback is used so the dev server keeps sessions across reloads.
    SECRET_KEY = os.environ.get('SECRET_KEY') or (None if IS_PRODUCTION else _DEV_SECRET)

    # ---- Sessions / cookies -------------------------------------------------
    # Secure cookies require HTTPS. Set SESSION_COOKIE_SECURE=false ONLY for a
    # plain-HTTP test deployment.
    SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', IS_PRODUCTION)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=_env_int('SESSION_LIFETIME_HOURS', 12))
    PREFERRED_URL_SCHEME = 'https' if IS_PRODUCTION else 'http'

    # Number of reverse proxies (nginx, Render, Railway, Heroku...) in front
    # of the app. Needed so request.remote_addr / rate limiting see the real
    # client IP instead of the proxy's. Set 0 if the app is exposed directly.
    TRUSTED_PROXY_COUNT = _env_int('TRUSTED_PROXY_COUNT', 1 if IS_PRODUCTION else 0)

    # ---- Database -----------------------------------------------------------
    # Production must use a real database (PostgreSQL). SQLite is dev-only.
    SQLALCHEMY_DATABASE_URI = _DATABASE_URL or (None if IS_PRODUCTION else _SQLITE_DEFAULT)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    if SQLALCHEMY_DATABASE_URI and not SQLALCHEMY_DATABASE_URI.startswith('sqlite'):
        SQLALCHEMY_ENGINE_OPTIONS = {
            'pool_pre_ping': True,       # drop dead connections instead of erroring
            'pool_recycle': 280,         # recycle before idle-timeouts of managed DBs
            'pool_size': _env_int('DB_POOL_SIZE', 3),
            'max_overflow': _env_int('DB_MAX_OVERFLOW', 2),
            'pool_timeout': 15,
        }
        if SQLALCHEMY_DATABASE_URI.startswith('postgresql'):
            SQLALCHEMY_ENGINE_OPTIONS['connect_args'] = {'connect_timeout': 10}

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    # Flask/Werkzeug 3.1+ rejects any single form field over 500 KB by default; the
    # enrollment photo is posted as a large base64 field, so allow up to the overall cap.
    MAX_FORM_MEMORY_SIZE = 5 * 1024 * 1024

    # ---- CSRF ---------------------------------------------------------------
    WTF_CSRF_ENABLED = True
    # Tokens live as long as the session. The default (1 hour) would make a
    # scanning page that is left open for a long lecture start failing.
    WTF_CSRF_TIME_LIMIT = None

    # ---- CORS ---------------------------------------------------------------
    # Comma separated list of allowed origins. Empty = CORS disabled
    # (same-origin only), which is what this app needs.
    CORS_ORIGINS = [o.strip() for o in os.environ.get('CORS_ORIGINS', '').split(',') if o.strip()]

    # ---- Rate limiting ------------------------------------------------------
    # memory:// is per worker process. Use redis://... when running several
    # workers/instances so limits are shared.
    RATELIMIT_STORAGE_URI = os.environ.get('RATELIMIT_STORAGE_URI', 'memory://')
    RATELIMIT_HEADERS_ENABLED = True
    # Campus Wi-Fi puts hundreds of students behind ONE public IP, so per-IP limits
    # are generous; the strict limit is per account (see LOGIN_ACCOUNT_RATE_LIMIT).
    LOGIN_RATE_LIMIT = os.environ.get('LOGIN_RATE_LIMIT', '120 per minute;1500 per hour')
    LOGIN_ACCOUNT_RATE_LIMIT = os.environ.get('LOGIN_ACCOUNT_RATE_LIMIT', '10 per 15 minutes')
    REGISTER_RATE_LIMIT = os.environ.get('REGISTER_RATE_LIMIT', '200 per hour')
    ENROLL_RATE_LIMIT = os.environ.get('ENROLL_RATE_LIMIT', '10 per minute')
    SCAN_RATE_LIMIT = os.environ.get('SCAN_RATE_LIMIT', '90 per minute')
    SENSITIVE_RATE_LIMIT = os.environ.get('SENSITIVE_RATE_LIMIT', '30 per minute')

    # ---- Files --------------------------------------------------------------
    FACES_DIR = os.environ.get('FACES_DIR') or os.path.join(BASE_DIR, 'uploads', 'faces')
    # Privacy by default: only the numeric face encoding is kept. Raw photos
    # are stored only when this is explicitly enabled.
    STORE_FACE_IMAGES = _env_bool('STORE_FACE_IMAGES', False)

    # ---- Face recognition ---------------------------------------------------
    CONFIDENCE_THRESHOLD = _env_float('CONFIDENCE_THRESHOLD', 0.50)
    DUPLICATE_THRESHOLD = _env_float('DUPLICATE_THRESHOLD', 0.50)
    MATCH_MARGIN = _env_float('MATCH_MARGIN', 0.07)

    FACE_DETECT_MAX_DIM = _env_int('FACE_DETECT_MAX_DIM', 640)     # scans
    FACE_ENROLL_MAX_DIM = _env_int('FACE_ENROLL_MAX_DIM', 960)     # enrollment
    FACE_MAX_IMAGE_PIXELS = _env_int('FACE_MAX_IMAGE_PIXELS', 25_000_000)
    FACE_MIN_SIZE_PX = _env_int('FACE_MIN_SIZE_PX', 60)            # on the downscaled image
    FACE_PRIMARY_RATIO = _env_float('FACE_PRIMARY_RATIO', 1.6)     # biggest face must be this much bigger
    FACE_SCAN_JITTERS = _env_int('FACE_SCAN_JITTERS', 1)
    FACE_ENROLL_JITTERS = _env_int('FACE_ENROLL_JITTERS', 2)       # per photo; several photos are taken
    ENROLL_CAPTURE_COUNT = _ENROLL_COUNT
    ENROLL_MIN_IMAGES = _ENROLL_MIN
    ENROLL_MAX_IMAGES = _ENROLL_MAX
    # All enrollment photos must show the same person: reject if any two are
    # further apart than this (same person, different pose ~0.2-0.45; different person > 0.6).
    ENROLL_CONSISTENCY_MAX = _env_float('ENROLL_CONSISTENCY_MAX', 0.55)

    # ---- Guided capture: the student must actually hold each pose -------------------
    # The browser asks the server to check frames (about 2 per second); a photo is
    # taken automatically only after POSE_HOLD_COUNT consecutive good checks, and the
    # server re-checks every submitted photo, so the rules cannot be skipped.
    POSE_CHECK_RATE_LIMIT = os.environ.get('POSE_CHECK_RATE_LIMIT', '200 per minute')
    POSE_CHECK_MAX_DIM = _env_int('POSE_CHECK_MAX_DIM', 640)       # frame size analysed (also used for the final re-check)
    POSE_CHECK_INTERVAL_MS = _env_int('POSE_CHECK_INTERVAL_MS', 450)
    POSE_HOLD_COUNT = max(1, _env_int('POSE_HOLD_COUNT', 3))       # consecutive good checks before auto-capture
    POSE_MIN_FACE_PX = _env_int('POSE_MIN_FACE_PX', 40)
    POSE_QUEUE_TIMEOUT = _env_float('POSE_QUEUE_TIMEOUT', 1.5)
    ENROLL_MAX_CONCURRENT = _env_int('ENROLL_MAX_CONCURRENT', 2)   # enrollment face jobs per worker
    POSE_FINAL_SLACK_DEG = _env_float('POSE_FINAL_SLACK_DEG', 4.0) # extra tolerance on the final server re-check
    # Thresholds (equivalent degrees; see app/pose_core.py). Tune with real students.
    POSE_STRAIGHT_MAX_YAW = _env_float('POSE_STRAIGHT_MAX_YAW', 10.0)
    POSE_TURN_MIN = _env_float('POSE_TURN_MIN', 12.0)
    POSE_TURN_MAX = _env_float('POSE_TURN_MAX', 38.0)
    POSE_PITCH_MIN = _env_float('POSE_PITCH_MIN', 8.0)
    POSE_PITCH_MAX = _env_float('POSE_PITCH_MAX', 30.0)
    POSE_SMILE_INCREASE = _env_float('POSE_SMILE_INCREASE', 0.08)
    # Max simultaneous dlib jobs per worker; the rest wait, then get a
    # "server busy, retrying" response instead of exhausting CPU/RAM.
    FACE_MAX_CONCURRENT = _env_int('FACE_MAX_CONCURRENT', 2)
    FACE_QUEUE_TIMEOUT = _env_float('FACE_QUEUE_TIMEOUT', 8.0)

    # SQLite has a compiled-in limit on IN() clause placeholders
    # (SQLITE_MAX_VARIABLE_NUMBER, historically 999). Batch helpers in
    # utils.py use this to stay under it regardless of roster size.
    IN_CLAUSE_BATCH_SIZE = 900

    # ---- Misc ---------------------------------------------------------------
    APP_TIMEZONE = os.environ.get('APP_TIMEZONE', 'Africa/Lagos')
    MIN_PASSWORD_LENGTH = _env_int('MIN_PASSWORD_LENGTH', 8)
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()

    ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@faceattend.com')
    ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD')
    SEED_DEMO_DATA = os.environ.get('SEED_DEMO_DATA', '').lower() == 'true'


def validate_config(app):
    """Fail fast with a clear message instead of misbehaving in production."""
    problems = []
    if not app.config.get('SECRET_KEY'):
        problems.append('SECRET_KEY is not set. Generate one with: '
                        'python -c "import secrets; print(secrets.token_hex(32))"')
    elif app.config.get('IS_PRODUCTION') and app.config['SECRET_KEY'] == _DEV_SECRET:
        problems.append('SECRET_KEY is the development placeholder.')
    if not app.config.get('SQLALCHEMY_DATABASE_URI'):
        problems.append('DATABASE_URL is not set (required in production, e.g. '
                        'postgresql://user:pass@host:5432/dbname).')
    if problems:
        raise RuntimeError('Invalid configuration:\n - ' + '\n - '.join(problems))