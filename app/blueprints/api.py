from datetime import timedelta

from flask import Blueprint, request, jsonify, session, current_app

from ..decorators import login_required
from ..extensions import db
from ..models import User, Course, CourseEnrollment, AttendanceSession, CourseAttendanceRecord
from ..timeutil import today_local

api_bp = Blueprint('api', __name__)


def _teacher_may_view(course):
    """Teachers may only look at their own courses; admins at any."""
    role = session.get('user_role')
    if role == 'admin':
        return True
    return role == 'teacher' and course.teacher_id == session.get('user_id')


@api_bp.route('/api/student/attendance-stats')
@login_required
def api_student_attendance_stats():
    try:
        if session.get('user_role') != 'student':
            return jsonify({'error': 'Access denied'}), 403

        user_id = session['user_id']
        course_id = request.args.get('course', type=int)

        enrollments = CourseEnrollment.query.filter_by(student_id=user_id, status='active').all()
        course_ids = [e.course_id for e in enrollments if not course_id or e.course_id == course_id]

        all_sessions = []
        if course_ids:
            all_sessions = AttendanceSession.query.filter(
                AttendanceSession.course_id.in_(course_ids)
            ).all()

        # ONE query for everything this student attended (the old code ran one
        # query per session, several times over).
        attended_session_ids = {
            row[0] for row in db.session.query(CourseAttendanceRecord.session_id)
            .filter(CourseAttendanceRecord.student_id == user_id).all()
        }

        total = len(all_sessions)
        present = sum(1 for sess in all_sessions if sess.id in attended_session_ids)
        absent = total - present
        rate = round((present / total) * 100, 2) if total > 0 else 0

        today = today_local()
        sessions_by_date = {}
        for sess in all_sessions:
            sessions_by_date.setdefault(sess.session_date, []).append(sess)

        trend_data = []
        for i in range(30, -1, -1):
            date = today - timedelta(days=i)
            day_sessions = sessions_by_date.get(date, [])
            day_total = len(day_sessions)
            day_present = sum(1 for sess in day_sessions if sess.id in attended_session_ids)

            if day_total > 0 or i == 0:
                trend_data.append({
                    'date': date.strftime('%Y-%m-%d'),
                    'present': day_present,
                    'absent': day_total - day_present
                })

        monthly_data = []
        months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

        year, month = today.year, today.month
        window = []
        for _ in range(6):
            window.append((year, month))
            month -= 1
            if month < 1:
                month = 12
                year -= 1
        window.reverse()

        for (yr, m) in window:
            month_sessions = [x for x in all_sessions
                              if x.session_date and x.session_date.year == yr and x.session_date.month == m]
            month_total = len(month_sessions)
            month_present = sum(1 for x in month_sessions if x.id in attended_session_ids)
            month_rate = round((month_present / month_total) * 100, 2) if month_total > 0 else 0

            if month_total > 0:
                monthly_data.append({
                    'month': f"{months[m - 1]} {yr}",
                    'total': month_total,
                    'present': month_present,
                    'absent': month_total - month_present,
                    'rate': month_rate
                })

        return jsonify({
            'total_sessions': total,
            'present': present,
            'absent': absent,
            'attendance_rate': rate,
            'trend_data': trend_data,
            'monthly_data': monthly_data
        })

    except Exception:
        current_app.logger.exception("API student attendance stats error")
        return jsonify({'error': 'Error loading attendance stats'}), 500


@api_bp.route('/api/courses/<int:course_id>/active-sessions')
@login_required
def api_active_sessions(course_id):
    try:
        course = Course.query.get_or_404(course_id)

        if session.get('user_role') == 'student':
            enrollment = CourseEnrollment.query.filter_by(
                student_id=session['user_id'],
                course_id=course_id
            ).first()
            if not enrollment:
                return jsonify({'error': 'Not enrolled in this course'}), 403
        elif not _teacher_may_view(course):
            return jsonify({'error': 'Access denied'}), 403

        sessions = AttendanceSession.query.filter_by(
            course_id=course_id,
            status='active'
        ).order_by(AttendanceSession.session_date.desc()).all()

        return jsonify({
            'sessions': [{
                'id': s.id,
                'session_date': s.session_date.isoformat() if s.session_date else None,
                'session_time': s.session_time.strftime('%H:%M:%S') if s.session_time else None,
                'topic': s.topic,
                'status': s.status,
                'session_token': s.session_token
            } for s in sessions]
        })
    except Exception:
        current_app.logger.exception("Error in api_active_sessions")
        return jsonify({'error': 'Error loading sessions'}), 500


@api_bp.route('/api/sessions/<int:session_id>')
@login_required
def api_session_details(session_id):
    try:
        session_obj = AttendanceSession.query.get_or_404(session_id)
        course = Course.query.get(session_obj.course_id)

        if session.get('user_role') == 'student':
            enrollment = CourseEnrollment.query.filter_by(
                student_id=session['user_id'],
                course_id=course.id
            ).first()
            if not enrollment:
                return jsonify({'error': 'Not enrolled'}), 403
        elif not _teacher_may_view(course):
            return jsonify({'error': 'Access denied'}), 403

        return jsonify({
            'id': session_obj.id,
            'course_id': course.id,
            'course_code': course.course_code,
            'session_date': session_obj.session_date.isoformat() if session_obj.session_date else None,
            'session_time': session_obj.session_time.strftime('%H:%M:%S') if session_obj.session_time else None,
            'topic': session_obj.topic,
            'status': session_obj.status,
            'session_token': session_obj.session_token,
            'created_at': session_obj.created_at.isoformat() if session_obj.created_at else None
        })
    except Exception:
        current_app.logger.exception("Error in api_session_details")
        return jsonify({'error': 'Error loading session details'}), 500