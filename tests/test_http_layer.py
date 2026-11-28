"""HTTP-layer tests that need no database rows: auth responses, security headers,
error handlers, config validation and health checks.

Run:  pytest tests/test_http_layer.py
"""
import os

os.environ.setdefault('APP_ENV', 'development')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.config import Config, _normalise_db_url  # noqa: E402


class TestConfig(Config):
    TESTING = False                      # let the real 500 handler run
    PROPAGATE_EXCEPTIONS = False
    SECRET_KEY = 'test-secret-key-not-for-production'
    SQLALCHEMY_DATABASE_URI = 'sqlite://'
    WTF_CSRF_ENABLED = False
    TRUSTED_PROXY_COUNT = 0
    IS_PRODUCTION = False


def make_client():
    app = create_app(TestConfig)

    @app.route('/_boom')
    def _boom():
        raise RuntimeError('boom')

    return app, app.test_client()


# ---------------------------- health + headers ----------------------------
def test_healthz_and_security_headers():
    _, c = make_client()
    r = c.get('/healthz')
    assert r.status_code == 200 and r.get_json() == {'status': 'ok'}
    assert r.headers['X-Content-Type-Options'] == 'nosniff'
    assert r.headers['X-Frame-Options'] == 'SAMEORIGIN'
    assert 'camera=(self)' in r.headers['Permissions-Policy']
    assert r.headers['Cache-Control'] == 'no-store'


# ---------------------------- auth responses ------------------------------
def test_scan_endpoint_returns_json_401_when_logged_out():
    _, c = make_client()
    r = c.post('/courses/session/1/mark', json={'image': 'x'})
    assert r.status_code == 401
    body = r.get_json()
    assert body['success'] is False and body['login_required'] is True


def test_html_page_redirects_to_login_when_logged_out():
    _, c = make_client()
    r = c.get('/dashboard')
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_html_stats_page_is_not_mistaken_for_json_endpoint():
    _, c = make_client()
    r = c.get('/student/attendance-stats')          # an HTML page, despite the name
    assert r.status_code == 302 and '/login' in r.headers['Location']


def test_json_stats_endpoint_returns_401_json():
    _, c = make_client()
    r = c.get('/attendance_stats')
    assert r.status_code == 401 and r.get_json()['login_required'] is True


def test_fetch_header_makes_any_route_answer_in_json():
    _, c = make_client()
    r = c.get('/dashboard', headers={'X-Requested-With': 'XMLHttpRequest'})
    assert r.status_code == 401 and r.get_json()['success'] is False


def test_pose_check_route_needs_a_login_and_answers_scripts_in_json():
    _, c = make_client()
    r = c.post('/enroll/pose-check', json={'image': 'x', 'pose_index': 0})
    assert r.status_code == 401
    assert r.get_json()['login_required'] is True


def test_pose_check_route_only_accepts_post():
    _, c = make_client()
    assert c.get('/enroll/pose-check').status_code == 405


def test_retired_recognize_face_requires_login_first():
    _, c = make_client()
    assert c.post('/recognize_face', json={'image': 'x'}).status_code == 401


# ---------------------------- error handlers ------------------------------
def test_unhandled_error_gives_json_for_scripts_and_html_for_browsers():
    _, c = make_client()
    r = c.get('/_boom', headers={'X-Requested-With': 'XMLHttpRequest'})
    assert r.status_code == 500 and r.get_json()['success'] is False

    r = c.get('/_boom')
    assert r.status_code == 500 and b'Something went wrong' in r.data
    assert 'Location' not in r.headers                  # never redirects (no loops)


def test_404_json_for_scripts():
    _, c = make_client()
    r = c.get('/definitely-missing', headers={'X-Requested-With': 'XMLHttpRequest'})
    assert r.status_code == 404 and r.get_json()['success'] is False


# ---------------------------- configuration -------------------------------
def test_production_requires_secret_key_and_database():
    class NoSecret(TestConfig):
        SECRET_KEY = None
        IS_PRODUCTION = True
    with pytest.raises(RuntimeError) as exc:
        create_app(NoSecret)
    assert 'SECRET_KEY' in str(exc.value)

    class NoDb(TestConfig):
        SQLALCHEMY_DATABASE_URI = None
        IS_PRODUCTION = True
    with pytest.raises(RuntimeError) as exc:
        create_app(NoDb)
    assert 'DATABASE_URL' in str(exc.value)


def test_dev_placeholder_secret_rejected_in_production():
    class DevSecret(TestConfig):
        SECRET_KEY = 'dev-only-insecure-secret-key-change-me'
        IS_PRODUCTION = True
    with pytest.raises(RuntimeError):
        create_app(DevSecret)


def test_heroku_style_postgres_url_is_normalised():
    assert _normalise_db_url('postgres://u:p@h:5432/db') == 'postgresql://u:p@h:5432/db'
    assert _normalise_db_url('postgresql://u:p@h/db') == 'postgresql://u:p@h/db'
    assert _normalise_db_url(None) is None


def test_every_worker_gets_the_same_secret_key():
    a1, _ = make_client()
    a2, _ = make_client()
    assert a1.config['SECRET_KEY'] == a2.config['SECRET_KEY'] == TestConfig.SECRET_KEY


def test_form_field_limit_allows_enrollment_photo():
    assert Config.MAX_FORM_MEMORY_SIZE >= 5 * 1024 * 1024