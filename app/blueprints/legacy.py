from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app
from sqlalchemy import or_, func, select
from sqlalchemy.orm import defer

from ..extensions import db
from ..decorators import login_required, teacher_or_admin_required
from ..models import (
    User, Student, Course, CourseEnrollment, AttendanceSession,
    CourseAttendanceRecord
)
from ..utils import (
    HAS_FACE, query_students_by_user_ids, query_users_by_ids, get_active_session_for_today
)
from ..timeutil import now_local, today_local, to_local

legacy_bp = Blueprint('legacy', __name__)


@legacy_bp.route('/mark_attendance')
@login_required
def mark_attendance():
    try:
        user = User.query.get(session['user_id'])
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('auth.login'))

        student_profile = Student.query.filter_by(user_id=user.id).first()

        courses = []
        if user.role == 'student':
            enrollments = CourseEnrollment.query.filter_by(
                student_id=user.id,
                status='active'
            ).all()
            for e in enrollments:
                course = Course.query.get(e.course_id)
                if course:
                    courses.append(course)
        elif user.role == 'teacher':
            courses = Course.query.filter_by(teacher_id=user.id, is_active=True).all()
        elif user.role == 'admin':
            courses = Course.query.filter_by(is_active=True).all()
        else:
            flash('Invalid user role.', 'danger')
            return redirect(url_for('main.dashboard'))

        selected_course_id = request.args.get('course', type=int)
        selected_course = None
        active_session = None
        recent_attendance = []

        if selected_course_id:
            candidate_course = Course.query.get(selected_course_id)
            if not candidate_course:
                flash('That course could not be found.', 'warning')
            else:
                access_ok = True
                if user.role == 'student':
                    enrollment = CourseEnrollment.query.filter_by(
                        student_id=user.id,
                        course_id=selected_course_id,
                        status='active'
                    ).first()
                    if not enrollment:
                        access_ok = False
                        flash(
                            f'You do not have an active enrollment in '
                            f'{candidate_course.course_code} - {candidate_course.course_name}, '
                            f'so attendance can\'t be marked for it yet. If a teacher told you '
                            f'this request was approved, check Browse Courses or contact them -- '
                            f'the enrollment record may not have been created correctly.',
                            'warning'
                        )
                elif user.role == 'teacher' and candidate_course.teacher_id != user.id:
                    access_ok = False
                    flash('You can only mark attendance for your own courses.', 'danger')

                if access_ok:
                    selected_course = candidate_course

        if not selected_course and courses:
            selected_course = courses[0]
            selected_course_id = selected_course.id

        if selected_course:
            active_session = get_active_session_for_today(selected_course.id)

            if active_session:
                rows = (
                    db.session.query(CourseAttendanceRecord, User.fullname)
                    .join(User, User.id == CourseAttendanceRecord.student_id)
                    .filter(CourseAttendanceRecord.session_id == active_session.id)
                    .order_by(CourseAttendanceRecord.marked_at.desc())
                    .all()
                )
                for record, fullname in rows:
                    marked_local = to_local(record.marked_at)
                    recent_attendance.append({
                        'student_name': fullname,
                        'time': marked_local.strftime('%I:%M %p') if marked_local else 'N/A',
                        'date': marked_local.strftime('%b %d, %Y') if marked_local else 'N/A',
                        'confidence': record.face_confidence
                    })

        return render_template('mark_attendance.html',
                             user=user,
                             student_profile=student_profile,
                             courses=courses,
                             selected_course=selected_course,
                             selected_course_id=selected_course_id,
                             active_session=active_session,
                             recent_attendance=recent_attendance,
                             now=now_local())

    except Exception:
        current_app.logger.exception("Error loading mark attendance")
        flash('Error loading attendance page. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))


@legacy_bp.route('/recognize_face', methods=['POST'])
@login_required
@teacher_or_admin_required
def recognize_face():
    """DISABLED. This old global (not tied to a class session) endpoint let any
    logged-in account mark attendance for anyone by posting a photo. Attendance
    is now marked only through /courses/session/<id>/mark, run by the course
    teacher. Returns 410 Gone so old integrations fail loudly, not silently."""
    return jsonify({
        'success': False,
        'message': 'This endpoint has been retired. Use a course attendance session instead.'
    }), 410


@legacy_bp.route('/attendance_stats')
@login_required
def attendance_stats():
    try:
        total_students = Student.query.filter(HAS_FACE).count()
        # Distinct students marked present in any session held today (app timezone).
        present_today = db.session.query(
            func.count(func.distinct(CourseAttendanceRecord.student_id))
        ).join(
            AttendanceSession, AttendanceSession.id == CourseAttendanceRecord.session_id
        ).filter(
            AttendanceSession.session_date == today_local()
        ).scalar() or 0
        absent_today = max(total_students - present_today, 0)
        attendance_rate = round((present_today / total_students) * 100, 2) if total_students > 0 else 0

        return jsonify({
            'total_students': total_students,
            'present_today': present_today,
            'absent_today': absent_today,
            'attendance_rate': attendance_rate
        })
    except Exception:
        current_app.logger.exception("Error getting attendance stats")
        return jsonify({'error': 'Error loading stats'}), 500


@legacy_bp.route('/students')
@login_required
@teacher_or_admin_required
def get_students():
    """List students. Admins see everyone; teachers only see students
    actively enrolled in one of their own courses."""
    try:
        user = User.query.get(session['user_id'])

        if user.role == 'teacher':
            teacher_courses = Course.query.filter_by(teacher_id=user.id).all()
            course_ids = [c.id for c in teacher_courses]

            if course_ids:
                enrollments = CourseEnrollment.query.filter(
                    CourseEnrollment.course_id.in_(course_ids),
                    CourseEnrollment.status == 'active'
                ).all()
                student_user_ids = [e.student_id for e in enrollments]
                students = query_students_by_user_ids(student_user_ids)
            else:
                students = []

            return jsonify([{
                'student_id': s.student_id,
                'fullname': s.fullname,
                'course': s.course,
            } for s in students])

        students = Student.query.options(defer(Student.face_encoding)).order_by(Student.fullname).limit(2000).all()
        return jsonify([s.to_dict() for s in students])
    except Exception:
        current_app.logger.exception("Error getting students")
        return jsonify({'error': 'Error loading students'}), 500


@legacy_bp.route('/search_students')
@login_required
@teacher_or_admin_required
def search_students():
    """Search students with role-based access control."""
    try:
        q = request.args.get('q', '').strip()
        user = User.query.get(session['user_id'])

        query = Student.query.options(defer(Student.face_encoding))

        if q:
            like = f'%{q}%'
            query = query.filter(
                or_(
                    Student.fullname.ilike(like),
                    Student.student_id.ilike(like),
                    Student.email.ilike(like),
                    Student.course.ilike(like)
                )
            )

        if user.role == 'teacher':
            # Filtered in SQL (a sub-select), not by loading every student into Python.
            allowed = (
                select(CourseEnrollment.student_id)
                .join(Course, Course.id == CourseEnrollment.course_id)
                .where(Course.teacher_id == user.id, CourseEnrollment.status == 'active')
            )
            query = query.filter(Student.user_id.in_(allowed))

        results = query.order_by(Student.fullname).limit(1000).all()

        user_accounts_by_id = {
            u.id: u for u in query_users_by_ids([s.user_id for s in results])
        }

        students_data = []
        for student in results:
            user_account = user_accounts_by_id.get(student.user_id)
            is_approved = user_account.is_approved if user_account else False

            students_data.append({
                'id': student.id,
                'student_id': student.student_id,
                'fullname': student.fullname,
                'email': student.email,
                'course': student.course,
                'level': student.level,
                'role': 'student',
                'is_approved': is_approved,
                'has_face_image': bool(student.face_image)
            })

        return jsonify(students_data)

    except Exception:
        current_app.logger.exception("Error searching students")
        return jsonify({'error': 'Error searching students'}), 500