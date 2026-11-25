import logging
import os
import sqlite3
import sys

from flask import Flask, jsonify, request, session
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config, validate_config
from .extensions import db, cors, csrf, limiter
from .timeutil import to_local


@event.listens_for(Engine, 'connect')
def _sqlite_pragmas(dbapi_connection, _record):
    """SQLite (development) only: enforce foreign keys like PostgreSQL does and
    wait instead of failing instantly when the file is briefly locked."""
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute('PRAGMA foreign_keys=ON')
        cursor.execute('PRAGMA busy_timeout=5000')
        cursor.close()


def _configure_logging(app):
    level = getattr(logging, str(app.config.get('LOG_LEVEL', 'INFO')).upper(), logging.INFO)
    gunicorn_error = logging.getLogger('gunicorn.error')
    if gunicorn_error.handlers:
        app.logger.handlers = list(gunicorn_error.handlers)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s [%(name)s] %(message)s'))
        app.logger.handlers = [handler]
    app.logger.setLevel(level)
    app.logger.propagate = False


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    validate_config(app)               # fail fast on missing SECRET_KEY / DATABASE_URL
    _configure_logging(app)

    if os.environ.get('CSRF_SECRET_KEY'):
        app.config['WTF_CSRF_SECRET_KEY'] = os.environ['CSRF_SECRET_KEY']

    proxies = int(app.config.get('TRUSTED_PROXY_COUNT') or 0)
    if proxies > 0:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxies, x_proto=proxies,
                                x_host=proxies, x_prefix=proxies)

    if app.config.get('STORE_FACE_IMAGES'):
        try:
            os.makedirs(app.config['FACES_DIR'], exist_ok=True)
        except OSError:
            app.logger.warning('Cannot create FACES_DIR %s; face photos will not be stored.',
                               app.config['FACES_DIR'])
            app.config['STORE_FACE_IMAGES'] = False

    db.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)
    if app.config.get('CORS_ORIGINS'):
        cors.init_app(app, origins=app.config['CORS_ORIGINS'], supports_credentials=False)

    # Templates: stored timestamps are UTC; show them in the app timezone.
    @app.template_filter('localtime')
    def localtime_filter(value):
        return to_local(value)

    @app.template_filter('localfmt')
    def localfmt_filter(value, fmt='%Y-%m-%d %H:%M'):
        local = to_local(value)
        return local.strftime(fmt) if local else ''

    @app.after_request
    def add_security_headers(response):
        headers = response.headers
        headers.setdefault('X-Content-Type-Options', 'nosniff')
        headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        # The camera is needed by this site only.
        headers.setdefault('Permissions-Policy', 'camera=(self), microphone=(), geolocation=()')
        if app.config.get('IS_PRODUCTION') and request.is_secure:
            headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
        if request.endpoint != 'static':
            headers.setdefault('Cache-Control', 'no-store')
        return response

    # ---- health checks (for the hosting platform / load balancer) ----------
    @app.route('/healthz')
    @limiter.exempt
    def healthz():
        """Liveness: the process is up. Never touches the database."""
        return jsonify(status='ok')

    @app.route('/readyz')
    @limiter.exempt
    def readyz():
        """Readiness: the database answers."""
        try:
            db.session.execute(text('SELECT 1'))
            return jsonify(status='ready')
        except Exception:
            db.session.rollback()
            app.logger.exception('Readiness check failed')
            return jsonify(status='database unavailable'), 503

    from .blueprints.auth import auth_bp
    from .blueprints.main import main_bp
    from .blueprints.student import student_bp
    from .blueprints.teacher import teacher_bp
    from .blueprints.admin import admin_bp
    from .blueprints.courses import courses_bp
    from .blueprints.api import api_bp
    from .blueprints.legacy import legacy_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(student_bp)
    app.register_blueprint(teacher_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(courses_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(legacy_bp)

    from .context_processors import register_context_processors
    register_context_processors(app)

    from .errors import register_error_handlers
    register_error_handlers(app)

    from .bootstrap import register_cli
    register_cli(app)

    return app
