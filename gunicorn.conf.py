"""Gunicorn settings. Override anything with environment variables.

Sizing: every worker loads dlib's models (~150-250 MB RAM) and does the CPU-
heavy face work. Start with WEB_CONCURRENCY = number of CPU cores (max 2-4 on
small servers) and keep GUNICORN_THREADS modest; scale the machine, not the
thread count, if scans become slow.
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
worker_class = 'gthread'
workers = int(os.environ.get('WEB_CONCURRENCY', 2))
threads = int(os.environ.get('GUNICORN_THREADS', 4))

timeout = int(os.environ.get('GUNICORN_TIMEOUT', 120))   # slow face jobs must not be killed mid-flight
graceful_timeout = 30
keepalive = 5

# Recycle workers occasionally to cap slow memory growth.
max_requests = 2000
max_requests_jitter = 200

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info').lower()

if os.path.isdir('/dev/shm'):
    worker_tmp_dir = '/dev/shm'   # avoids heartbeat stalls on container disks
