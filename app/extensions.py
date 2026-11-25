from flask import session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

db = SQLAlchemy()
cors = CORS()
csrf = CSRFProtect()


def user_or_ip():
    """Rate-limit key: the logged-in user if any, otherwise the client IP.
    (Keying scans by user avoids one busy classroom NAT-ing everyone into a
    single IP bucket.)"""
    uid = session.get('user_id')
    return f'user:{uid}' if uid else get_remote_address()


# Limits are applied per route with @limiter.limit(...); there is deliberately
# no global default so normal page views and camera scans are never throttled
# by accident.
limiter = Limiter(key_func=get_remote_address)
