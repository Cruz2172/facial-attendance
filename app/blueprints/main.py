import time

from flask import Blueprint, render_template, redirect, url_for, flash, session, current_app
from sqlalchemy import func

from ..decorators import login_required
from ..extensions import db
from ..models import Student, User, Course, AttendanceSession, CourseAttendanceRecord
from ..timeutil import now_local, today_local
from ..utils import HAS_FACE

main_bp = Blueprint('main', __name__)

# The public home page shows four counters. Cache them briefly (per worker) so a
# burst of visitors cannot turn into a burst of database queries.
_HOME_STATS_TTL = 60
_home_stats = {'at': 0.0, 'value': None}


def _load_home_stats():
    total_students = Student.query.filter(HAS_FACE).count()
    total_teachers = User.query.filter_by(role='teacher').count()
    total_courses = Course.query.count()

    present_today = db.session.query(
        func.count(func.distinct(CourseAttendanceRecord.student_id))
    ).join(
        AttendanceSession, AttendanceSession.id == CourseAttendanceRecord.session_id
    ).filter(AttendanceSession.session_date == today_local()).scalar() or 0

    attendance_today = round((present_today / total_students) * 100, 2) if total_students > 0 else 0
    return dict(total_students=total_students, total_teachers=total_teachers,
                total_courses=total_courses, attendance_today=attendance_today)


@main_bp.route('/')
def home():
    try:
        now = time.monotonic()
        if _home_stats['value'] is None or now - _home_stats['at'] > _HOME_STATS_TTL:
            _home_stats['value'] = _load_home_stats()
            _home_stats['at'] = now
        stats = _home_stats['value']
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Home page error")
        stats = dict(total_students=0, total_teachers=0, total_courses=0, attendance_today=0)

    return render_template('home.html', now=now_local(), **stats)


@main_bp.route('/dashboard')
@login_required
def dashboard():
    try:
        if 'user_id' not in session:
            flash('Please login first.', 'warning')
            return redirect(url_for('auth.login'))

        user = User.query.get(session['user_id'])
        if not user:
            session.clear()
            flash('User not found. Please login again.', 'danger')
            return redirect(url_for('auth.login'))

        if not user.is_approved:
            session.clear()
            flash('Your account is pending approval.', 'warning')
            return redirect(url_for('auth.login'))

        if user.role == 'student':
            return redirect(url_for('student.student_dashboard'))
        elif user.role == 'teacher':
            return redirect(url_for('teacher.teacher_dashboard'))
        elif user.role == 'admin':
            return redirect(url_for('admin.admin_dashboard'))
        else:
            flash('Invalid user role.', 'danger')
            return redirect(url_for('main.home'))

    except Exception:
        current_app.logger.exception("Dashboard error")
        flash('Error loading dashboard.', 'danger')
        return redirect(url_for('main.home'))