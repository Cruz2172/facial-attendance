import json
from datetime import datetime

import numpy as np
from flask import current_app
from sqlalchemy import text

from .extensions import db
from .timeutil import utcnow, now_local, today_local


class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='student')
    is_approved = db.Column(db.Boolean, default=False)
    invite_code = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    courses_teaching = db.relationship('Course', backref='teacher', lazy=True, foreign_keys='Course.teacher_id')
    enrollments = db.relationship('CourseEnrollment', backref='student', lazy=True, foreign_keys='CourseEnrollment.student_id', cascade='all, delete-orphan')
    enrollment_requests = db.relationship('CourseEnrollmentRequest', backref='student', lazy=True, foreign_keys='CourseEnrollmentRequest.student_id', cascade='all, delete-orphan')
    attendance_records = db.relationship('CourseAttendanceRecord', backref='student', lazy=True, foreign_keys='CourseAttendanceRecord.student_id', cascade='all, delete-orphan')
    student_profile = db.relationship('Student', backref='user', lazy=True, uselist=False, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'fullname': self.fullname,
            'email': self.email,
            'role': self.role,
            'is_approved': self.is_approved,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }


class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), unique=True, nullable=False)
    student_id = db.Column(db.String(50), unique=True, nullable=False)
    fullname = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    course = db.Column(db.String(120), nullable=False)
    level = db.Column(db.String(20), nullable=True)
    face_encoding = db.Column(db.Text, nullable=False)
    face_image = db.Column(db.String(256))
    enrolled_at = db.Column(db.DateTime, default=utcnow)

    __table_args__ = (
        db.Index('idx_student_student_id', 'student_id'),
        db.Index('idx_student_email', 'email'),
        db.Index('idx_student_user_id', 'user_id'),
    )

    def get_encoding(self):
        try:
            return np.array(json.loads(self.face_encoding))
        except Exception as e:
            current_app.logger.error(f"Error loading encoding for student {self.id}: {str(e)}")
            return np.array([])

    def to_dict(self):
        return {
            'id': self.id,
            'student_id': self.student_id,
            'fullname': self.fullname,
            'email': self.email,
            'course': self.course,
            'level': self.level,
            'has_face_image': bool(self.face_image),
            'enrolled_at': self.enrolled_at.isoformat() if self.enrolled_at else None,
        }


class AttendanceRecord(db.Model):
    """Legacy global (non-course-scoped) attendance table. Kept only for
    /recognize_face and /camera_test — the real workflow uses
    CourseAttendanceRecord. See legacy.py."""
    __tablename__ = 'attendance'
    __table_args__ = (
        db.UniqueConstraint('student_id', 'date', name='unique_daily_attendance'),
    )
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(50), db.ForeignKey('students.student_id'), nullable=False)
    student_name = db.Column(db.String(120), nullable=False)
    date = db.Column(db.String(10), nullable=False)
    time = db.Column(db.String(8), nullable=False)
    timestamp = db.Column(db.DateTime, default=utcnow)

    def to_dict(self):
        return {
            'student_id': self.student_id,
            'student_name': self.student_name,
            'date': self.date,
            'time': self.time,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
        }


class InviteCode(db.Model):
    __tablename__ = 'invite_codes'
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)
    role = db.Column(db.String(20), nullable=False)
    used = db.Column(db.Boolean, default=False)
    used_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    max_uses = db.Column(db.Integer, default=1)
    uses_count = db.Column(db.Integer, default=0)
    is_teacher_invite = db.Column(db.Boolean, default=False)

    def is_valid(self):
        if self.used and self.max_uses != -1:
            return False
        if self.expires_at and utcnow() > self.expires_at:
            return False
        if self.max_uses != -1 and self.uses_count >= self.max_uses:
            return False
        return True


class Course(db.Model):
    __tablename__ = 'courses'

    id = db.Column(db.Integer, primary_key=True)
    course_code = db.Column(db.String(20), unique=True, nullable=False)
    course_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    schedule = db.Column(db.String(100))
    teacher_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=utcnow)

    enrollments = db.relationship('CourseEnrollment', backref='course', lazy=True, cascade='all, delete-orphan')
    sessions = db.relationship('AttendanceSession', backref='course', lazy=True, cascade='all, delete-orphan')
    enrollment_requests = db.relationship('CourseEnrollmentRequest', backref='course', lazy=True, cascade='all, delete-orphan')
    enrollment_tokens = db.relationship('CourseEnrollmentToken', backref='course', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'course_code': self.course_code,
            'course_name': self.course_name,
            'description': self.description,
            'schedule': self.schedule,
            'teacher_id': self.teacher_id,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None
        }

    def get_enrolled_count(self):
        """Counts only students who completed face enrollment -- an active
        CourseEnrollment row alone isn't enough. A student who hasn't done
        face enrollment must not show up or count on any teacher/admin view."""
        from .utils import count_students_by_user_ids  # deferred: avoids circular import
        try:
            enrollments = CourseEnrollment.query.filter_by(course_id=self.id, status='active').all()
            student_user_ids = [e.student_id for e in enrollments]
            if not student_user_ids:
                return 0
            return count_students_by_user_ids(student_user_ids)
        except Exception:
            return 0

    def get_pending_requests_count(self):
        try:
            return CourseEnrollmentRequest.query.filter_by(
                course_id=self.id,
                status='pending'
            ).count()
        except Exception:
            return 0

    def get_teacher_name(self):
        try:
            teacher = User.query.get(self.teacher_id)
            return teacher.fullname if teacher else "Unknown Teacher"
        except Exception:
            return "Unknown Teacher"


class CourseEnrollment(db.Model):
    __tablename__ = 'course_enrollments'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id'), nullable=False)
    enrolled_at = db.Column(db.DateTime, default=utcnow)
    status = db.Column(db.String(20), default='active')

    __table_args__ = (
        db.UniqueConstraint('student_id', 'course_id', name='unique_course_enrollment'),
        db.Index('idx_enrollment_student', 'student_id'),
        db.Index('idx_enrollment_course', 'course_id'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'student_id': self.student_id,
            'course_id': self.course_id,
            'enrolled_at': self.enrolled_at.isoformat() if self.enrolled_at else None,
            'status': self.status
        }


class CourseEnrollmentRequest(db.Model):
    __tablename__ = 'course_enrollment_requests'

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id'), nullable=False)
    status = db.Column(db.String(20), default='pending')
    requested_at = db.Column(db.DateTime, default=utcnow)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)
    remarks = db.Column(db.String(200), nullable=True)

    __table_args__ = (db.UniqueConstraint('student_id', 'course_id', name='unique_enrollment_request'),)

    def to_dict(self):
        student = User.query.get(self.student_id)
        reviewer = User.query.get(self.reviewed_by) if self.reviewed_by else None

        return {
            'id': self.id,
            'student_id': self.student_id,
            'student_name': student.fullname if student else 'Unknown',
            'student_email': student.email if student else 'Unknown',
            'course_id': self.course_id,
            'status': self.status,
            'requested_at': self.requested_at.isoformat() if self.requested_at else None,
            'reviewed_at': self.reviewed_at.isoformat() if self.reviewed_at else None,
            'reviewed_by': self.reviewed_by,
            'reviewer_name': reviewer.fullname if reviewer else None,
            'remarks': self.remarks
        }


class AttendanceSession(db.Model):
    __tablename__ = 'attendance_sessions'

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id'), nullable=False)
    session_date = db.Column(db.Date, nullable=False, default=today_local)
    session_time = db.Column(db.Time, nullable=False, default=lambda: now_local().time())
    topic = db.Column(db.String(200))
    status = db.Column(db.String(20), default='active')
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    session_token = db.Column(db.String(50), unique=True, nullable=False)

    # Two simultaneous "start session" clicks used to create two active
    # sessions; this partial unique index makes the second one fail cleanly.
    __table_args__ = (
        db.Index('uq_one_active_session_per_course', 'course_id', unique=True,
                 postgresql_where=text("status = 'active'"),
                 sqlite_where=text("status = 'active'")),
    )

    records = db.relationship('CourseAttendanceRecord', backref='session', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'course_id': self.course_id,
            'session_date': self.session_date.isoformat() if self.session_date else None,
            'session_time': self.session_time.strftime('%H:%M:%S') if self.session_time else None,
            'topic': self.topic,
            'status': self.status,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'session_token': self.session_token
        }

    def get_present_count(self):
        try:
            return CourseAttendanceRecord.query.filter_by(session_id=self.id).count()
        except Exception:
            return 0

    def get_total_enrolled(self):
        """Only counts face-enrolled students -- see Course.get_enrolled_count()."""
        from .utils import count_students_by_user_ids
        try:
            enrollments = CourseEnrollment.query.filter_by(course_id=self.course_id).all()
            student_user_ids = [e.student_id for e in enrollments]
            if not student_user_ids:
                return 0
            return count_students_by_user_ids(student_user_ids)
        except Exception:
            return 0


class CourseAttendanceRecord(db.Model):
    __tablename__ = 'course_attendance_records'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey('attendance_sessions.id'), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    marked_at = db.Column(db.DateTime, default=utcnow)
    face_confidence = db.Column(db.Float)
    status = db.Column(db.String(20), default='present')
    remarks = db.Column(db.String(200))

    __table_args__ = (
        db.UniqueConstraint('session_id', 'student_id', name='unique_course_attendance'),
        db.Index('idx_car_student', 'student_id'),
    )

    def to_dict(self):
        student = User.query.get(self.student_id)
        return {
            'id': self.id,
            'session_id': self.session_id,
            'student_id': self.student_id,
            'student_name': student.fullname if student else 'Unknown',
            'marked_at': self.marked_at.isoformat() if self.marked_at else None,
            'face_confidence': self.face_confidence,
            'status': self.status,
            'remarks': self.remarks
        }


class CourseEnrollmentToken(db.Model):
    __tablename__ = 'course_enrollment_tokens'

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.Integer, db.ForeignKey('courses.id'), nullable=False)
    token_code = db.Column(db.String(10), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    max_uses = db.Column(db.Integer, default=50)
    used_count = db.Column(db.Integer, default=0)
    created_by = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    is_active = db.Column(db.Boolean, default=True)

    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and utcnow() > self.expires_at:
            return False
        if self.max_uses != -1 and self.used_count >= self.max_uses:
            return False
        return True

    def get_enrollment_link(self):
        return f"/courses/enroll/{self.token_code}"

    def to_dict(self):
        return {
            'id': self.id,
            'course_id': self.course_id,
            'token_code': self.token_code,
            'expires_at': self.expires_at.isoformat() if self.expires_at else None,
            'max_uses': self.max_uses,
            'used_count': self.used_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'is_active': self.is_active,
            'enrollment_link': self.get_enrollment_link()
        }


class AppState(db.Model):
    """Tiny key/value counters shared by every worker process.

    ``faces_version`` / ``roster_version`` are bumped whenever face data or
    course rosters change. Each worker compares them with its in-memory cache
    and reloads when they differ, so a deleted or newly enrolled student takes
    effect everywhere (not only in the worker that handled the request).
    """
    __tablename__ = 'app_state'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.Integer, nullable=False, default=0)


class AuditLog(db.Model):
    """Append-only trail of sensitive actions (manual attendance changes,
    deletions, role changes, ...). user_id deliberately has no foreign key so
    history survives deleting the account that performed the action."""
    __tablename__ = 'audit_log'
    __table_args__ = (db.Index('idx_audit_created', 'created_at'),)

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    user_id = db.Column(db.Integer, nullable=True)
    action = db.Column(db.String(60), nullable=False)
    target_type = db.Column(db.String(40))
    target_id = db.Column(db.String(60))
    details = db.Column(db.Text)

