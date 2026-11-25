"""Database bootstrap, run ONCE per deployment (not on every worker start).

    python run.py init-db            # or:  flask --app wsgi init-db

Everything here is idempotent, so it is safe to run on every deploy.
"""
import secrets
import traceback

import click
from sqlalchemy import text
from werkzeug.security import generate_password_hash

from .extensions import db
from .face_cache import ensure_state_rows
from .models import User, Student, Course
from .timeutil import utcnow

# CREATE ... IF NOT EXISTS works on both PostgreSQL and SQLite and, unlike
# db.create_all(), also adds indexes to tables that already exist.
_INDEX_STATEMENTS = [
    # At most one active session per course (race-proof "start session").
    ("one active session per course",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_session_per_course "
     "ON attendance_sessions (course_id) WHERE status = 'active'"),
    ("session lookup by course",
     "CREATE INDEX IF NOT EXISTS idx_sessions_course_status "
     "ON attendance_sessions (course_id, status)"),
    ("attendance lookup by student",
     "CREATE INDEX IF NOT EXISTS idx_car_student "
     "ON course_attendance_records (student_id)"),
    ("audit log by date",
     "CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at)"),
]


def _create_indexes(log):
    for label, statement in _INDEX_STATEMENTS:
        try:
            db.session.execute(text(statement))
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            log(f"  ! Could not create index '{label}': {exc}")
            if 'active session' in label:
                log("    (If this says duplicate key: a course has two ACTIVE sessions. "
                    "End the extra one, then re-run init-db.)")


def init_database(app, log=print):
    """Create tables, indexes, counters and the admin account."""
    with app.app_context():
        try:
            db.create_all()
            log('=' * 50)
            log('Tables ready: ' + ', '.join(sorted(db.metadata.tables.keys())))

            _create_indexes(log)
            ensure_state_rows()

            admin_email = (app.config['ADMIN_EMAIL'] or '').strip().lower()
            admin = User.query.filter(db.func.lower(User.email) == admin_email).first()

            if not admin:
                admin_password = app.config['ADMIN_PASSWORD']
                generated = False
                if not admin_password:
                    admin_password = secrets.token_urlsafe(12)
                    generated = True

                admin = User(
                    fullname='Admin',
                    email=admin_email,
                    password=generate_password_hash(admin_password),
                    role='admin',
                    is_approved=True,
                    created_at=utcnow(),
                )
                db.session.add(admin)
                db.session.commit()

                log(f'Admin user created: {admin_email}')
                if generated:
                    log(f'Generated password (SAVE THIS NOW): {admin_password}')
                else:
                    log('Password set from ADMIN_PASSWORD environment variable.')
            else:
                log(f'Admin user already exists: {admin_email}')

            if app.config['SEED_DEMO_DATA']:
                _seed_demo(app, admin, log)
            else:
                log('Demo data seeding skipped (set SEED_DEMO_DATA=true to enable)')

            log('=' * 50)
            log(f'Students: {Student.query.count()}  Users: {User.query.count()}  '
                f'Courses: {Course.query.count()}')
            log('=' * 50)
            return True
        except Exception as exc:
            log(f'Database initialization error: {exc}')
            traceback.print_exc()
            db.session.rollback()
            return False


def _seed_demo(app, admin, log):
    teacher = User.query.filter_by(role='teacher').first()
    if not teacher:
        teacher_password = secrets.token_urlsafe(12)
        teacher = User(
            fullname='Sample Teacher',
            email='teacher@school.com',
            password=generate_password_hash(teacher_password),
            role='teacher',
            is_approved=True,
            created_at=utcnow(),
        )
        db.session.add(teacher)
        db.session.commit()
        log(f'Sample teacher created: teacher@school.com / {teacher_password}')

    if Course.query.count() == 0:
        db.session.add(Course(
            course_code='CS101',
            course_name='Introduction to Computer Science',
            description='This is a sample course created by the system.',
            schedule='Monday and Wednesday 10:00 AM - 11:30 AM',
            teacher_id=(teacher or admin).id,
            is_active=True,
            created_at=utcnow(),
        ))
        db.session.commit()
        log('Sample course created: CS101')


def register_cli(app):
    @app.cli.command('init-db')
    def init_db_command():
        """Create/upgrade tables and the admin account (idempotent)."""
        ok = init_database(app, log=click.echo)
        if not ok:
            raise SystemExit(1)
