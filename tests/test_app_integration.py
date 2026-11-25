"""End-to-end tests against a real (in-memory SQLite) database.

dlib is replaced by tiny fakes, so these run fast and need no camera/GPU:
    pip install pytest
    pytest tests/test_app_integration.py -q
"""
import base64
import importlib
import json
import os

os.environ.setdefault('APP_ENV', 'development')
os.environ.setdefault('SECRET_KEY', 'test-secret-key-not-for-production')

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app import face_cache  # noqa: E402
from app.bootstrap import init_database  # noqa: E402
from app.config import Config  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    AttendanceSession, AuditLog, Course, CourseAttendanceRecord, CourseEnrollment, Student, User,
)

courses_mod = importlib.import_module('app.blueprints.courses')


class IntegrationConfig(Config):
    TESTING = False
    PROPAGATE_EXCEPTIONS = False
    SECRET_KEY = 'test-secret-key-not-for-production'
    SQLALCHEMY_DATABASE_URI = 'sqlite://'
    SQLALCHEMY_ENGINE_OPTIONS = {}
    WTF_CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    TRUSTED_PROXY_COUNT = 0
    IS_PRODUCTION = False
    ADMIN_PASSWORD = 'Admin-pass-123'


RNG = np.random.default_rng(7)
ENC = {name: RNG.normal(0, 0.09, 128) for name in ('alice', 'bob', 'carol', 'stranger')}


def _data_url():
    ok, buf = cv2.imencode('.jpg', np.zeros((200, 200, 3), np.uint8))
    return 'data:image/jpeg;base64,' + base64.b64encode(buf.tobytes()).decode()


DATA_URL = _data_url()


def make_user(email, name, role, password='Password-123'):
    u = User(fullname=name, email=email, password=generate_password_hash(password),
             role=role, is_approved=True)
    db.session.add(u)
    db.session.flush()
    return u


def make_student(user, sid, enc):
    s = Student(user_id=user.id, student_id=sid, fullname=user.fullname, email=user.email,
                course='CS', level='100', face_encoding=json.dumps(enc.tolist()))
    db.session.add(s)
    return s


def build():
    face_cache.reset_for_tests()
    app = create_app(IntegrationConfig)
    init_database(app, log=lambda *_: None)
    with app.app_context():
        teacher = make_user('teacher@uni.edu', 'Teacher', 'teacher')
        other_teacher = make_user('other@uni.edu', 'Other Teacher', 'teacher')
        alice = make_user('alice@uni.edu', 'Alice', 'student')
        bob = make_user('bob@uni.edu', 'Bob', 'student')
        carol = make_user('carol@uni.edu', 'Carol', 'student')   # has a face, NOT in the course
        course = Course(course_code='CS101', course_name='Intro', teacher_id=teacher.id, is_active=True)
        db.session.add(course)
        db.session.flush()
        for u in (alice, bob):
            db.session.add(CourseEnrollment(student_id=u.id, course_id=course.id, status='active'))
        make_student(alice, 'A001', ENC['alice'])
        make_student(bob, 'B002', ENC['bob'])
        make_student(carol, 'C003', ENC['carol'])
        sess = AttendanceSession(course_id=course.id, topic='Week 1', status='active',
                                 created_by=teacher.id, session_token='TOK123')
        db.session.add(sess)
        db.session.commit()
        ids = dict(teacher=teacher.id, other=other_teacher.id, alice=alice.id, bob=bob.id,
                   carol=carol.id, course=course.id, session=sess.id)
    return app, ids


def login_as(client, user_id, role):
    with client.session_transaction() as s:
        s['user_id'] = user_id
        s['user_role'] = role


def scan(client, monkeypatch, session_id, encoding):
    monkeypatch.setattr(courses_mod, 'detect_faces', lambda img, **k: [(10, 170, 170, 10)])
    monkeypatch.setattr(courses_mod, 'encode_face', lambda img, box, jitters=1: encoding)
    return client.post(f'/courses/session/{session_id}/mark', json={'image': DATA_URL})


# ------------------------------------------------------------------ scanning
def test_scan_marks_present_then_reports_already_marked(monkeypatch):
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')

    r = scan(c, monkeypatch, ids['session'], ENC['alice'])
    body = r.get_json()
    assert r.status_code == 200 and body['success'] and not body['already_marked']
    assert body['student_name'] == 'Alice'

    # a SECOND scan must still work (this used to crash on a detached cached object)
    r = scan(c, monkeypatch, ids['session'], ENC['alice'])
    assert r.get_json()['already_marked'] is True

    # and a different student afterwards
    r = scan(c, monkeypatch, ids['session'], ENC['bob'])
    assert r.get_json()['student_name'] == 'Bob' and not r.get_json()['already_marked']

    with app.app_context():
        assert CourseAttendanceRecord.query.filter_by(session_id=ids['session']).count() == 2


def test_unknown_face_and_not_enrolled_are_rejected(monkeypatch):
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')

    r = scan(c, monkeypatch, ids['session'], ENC['stranger'])
    assert r.get_json()['success'] is False and 'not recognized' in r.get_json()['message']

    r = scan(c, monkeypatch, ids['session'], ENC['carol'])
    assert 'not enrolled' in r.get_json()['message']


def test_only_the_course_teacher_can_scan(monkeypatch):
    app, ids = build()
    c = app.test_client()

    login_as(c, ids['alice'], 'student')
    assert scan(c, monkeypatch, ids['session'], ENC['alice']).status_code == 403

    login_as(c, ids['other'], 'teacher')
    r = scan(c, monkeypatch, ids['session'], ENC['alice'])
    assert r.get_json()['success'] is False and 'permission' in r.get_json()['message']


def test_closed_session_rejects_scans(monkeypatch):
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')
    with app.app_context():
        db.session.get(AttendanceSession, ids['session']).status = 'closed'
        db.session.commit()
    r = scan(c, monkeypatch, ids['session'], ENC['alice'])
    assert r.get_json()['closed'] is True


def test_deleted_student_is_no_longer_recognised(monkeypatch):
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')
    assert scan(c, monkeypatch, ids['session'], ENC['bob']).get_json()['success']

    with app.app_context():
        db.session.delete(Student.query.filter_by(student_id='B002').first())
        db.session.commit()
        face_cache.invalidate_face_cache()

    with app.app_context():
        db.session.query(CourseAttendanceRecord).delete()
        db.session.commit()
    r = scan(c, monkeypatch, ids['session'], ENC['bob'])
    assert r.get_json()['success'] is False


def test_no_face_and_bad_image_are_handled(monkeypatch):
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')

    monkeypatch.setattr(courses_mod, 'detect_faces', lambda img, **k: [])
    r = c.post(f"/courses/session/{ids['session']}/mark", json={'image': DATA_URL})
    assert r.get_json().get('idle') is True

    r = c.post(f"/courses/session/{ids['session']}/mark", json={'image': 'not-an-image'})
    assert r.get_json()['success'] is False


# ------------------------------------------------------------ manual override
def test_manual_mark_and_unmark_are_audited():
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')

    r = c.post(f"/courses/session/{ids['session']}/manual-mark",
               data={'student_user_id': ids['alice'], 'reason': 'glasses'})
    assert r.status_code == 302
    with app.app_context():
        rec = CourseAttendanceRecord.query.filter_by(session_id=ids['session'], student_id=ids['alice']).first()
        assert rec is not None and 'glasses' in rec.remarks
        assert AuditLog.query.filter_by(action='attendance.manual_mark').count() == 1

    c.post(f"/courses/session/{ids['session']}/manual-unmark", data={'student_user_id': ids['alice']})
    with app.app_context():
        assert CourseAttendanceRecord.query.count() == 0
        assert AuditLog.query.filter_by(action='attendance.unmark').count() == 1


def test_manual_mark_rejects_students_not_in_the_course():
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['teacher'], 'teacher')
    c.post(f"/courses/session/{ids['session']}/manual-mark", data={'student_user_id': ids['carol']})
    with app.app_context():
        assert CourseAttendanceRecord.query.count() == 0


def test_other_teacher_cannot_manual_mark():
    app, ids = build()
    c = app.test_client()
    login_as(c, ids['other'], 'teacher')
    r = c.post(f"/courses/session/{ids['session']}/manual-mark", data={'student_user_id': ids['alice']})
    assert r.status_code == 403


# ------------------------------------------------------------ data integrity
def test_database_allows_only_one_active_session_per_course():
    app, ids = build()
    with app.app_context():
        db.session.add(AttendanceSession(course_id=ids['course'], topic='dup', status='active',
                                         created_by=ids['teacher'], session_token='DUP999'))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


def test_double_scan_race_hits_unique_constraint_not_a_crash():
    app, ids = build()
    with app.app_context():
        db.session.add(CourseAttendanceRecord(session_id=ids['session'], student_id=ids['alice']))
        db.session.commit()
        db.session.add(CourseAttendanceRecord(session_id=ids['session'], student_id=ids['alice']))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ------------------------------------------------------------------- accounts
def test_login_is_case_insensitive_and_rejects_bad_password():
    app, ids = build()
    c = app.test_client()
    r = c.post('/login', data={'email': '  TEACHER@Uni.EDU ', 'password': 'Password-123'})
    assert r.status_code == 302 and '/dashboard' in r.headers['Location']

    c2 = app.test_client()
    r = c2.post('/login', data={'email': 'teacher@uni.edu', 'password': 'wrong'}, follow_redirects=False)
    assert r.status_code == 302 and '/login' in r.headers['Location']
    with c2.session_transaction() as s:
        assert 'user_id' not in s


def test_registration_enforces_password_length_and_email_uniqueness():
    app, ids = build()
    c = app.test_client()
    base = {'fullname': 'New', 'confirm_password': 'short'}
    c.post('/register', data={**base, 'email': 'new@uni.edu', 'password': 'short'})
    with app.app_context():
        assert User.query.filter_by(email='new@uni.edu').first() is None

    c.post('/register', data={'fullname': 'Dup', 'email': 'ALICE@uni.edu',
                              'password': 'LongEnough-1', 'confirm_password': 'LongEnough-1'})
    with app.app_context():
        assert User.query.filter(db.func.lower(User.email) == 'alice@uni.edu').count() == 1


def test_profile_save_does_not_create_a_faceless_student_row():
    app, ids = build()
    with app.app_context():
        dave = make_user('dave@uni.edu', 'Dave', 'student')
        db.session.commit()
        dave_id = dave.id
    c = app.test_client()
    login_as(c, dave_id, 'student')
    c.post('/student/profile', data={'fullname': 'Dave D', 'email': 'dave@uni.edu',
                                     'student_id': 'D777', 'course': 'CS', 'level': '100'})
    with app.app_context():
        assert Student.query.filter_by(user_id=dave_id).first() is None


def test_enrollment_requires_consent():
    app, ids = build()
    with app.app_context():
        erin = make_user('erin@uni.edu', 'Erin', 'student')
        db.session.commit()
        erin_id = erin.id
    c = app.test_client()
    login_as(c, erin_id, 'student')
    r = c.post('/enroll', data={'student_id': 'E123', 'fullname': 'Erin', 'email': 'erin@uni.edu',
                                'course': 'CS', 'level': '100', 'face_data': DATA_URL})
    assert b'agree to the processing of your facial data' in r.data     # the flashed error, not just the checkbox label
    with app.app_context():
        assert Student.query.filter_by(user_id=erin_id).first() is None


def test_incomplete_student_row_does_not_block_enrollment_and_is_hidden():
    from app.utils import count_students_by_user_ids
    app, ids = build()
    with app.app_context():
        frank = make_user('frank@uni.edu', 'Frank', 'student')
        db.session.add(Student(user_id=frank.id, student_id='F1', fullname='Frank', email='f@uni.edu',
                               course='', level='', face_encoding='[]'))
        db.session.commit()
        assert count_students_by_user_ids([frank.id]) == 0       # not counted as face-enrolled
        from app.services.enrollment import check_not_already_enrolled
        assert check_not_already_enrolled(frank.id, 'F1', 'f@uni.edu') is not None   # may finish enrolling


# ---------------------------------------------------------------------- cache
def test_cache_holds_plain_records_not_orm_objects(monkeypatch):
    app, ids = build()
    with app.app_context():
        state = face_cache.get_state()
        assert len(state.records) == 3
        rec = state.records[0]
        assert type(rec).__name__ == 'FaceRecord'
        db.session.commit()                    # a commit used to expire cached ORM objects
        assert rec.fullname                    # still readable
