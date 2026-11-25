from flask import session, g

from .extensions import db
from .models import User, Course, CourseEnrollmentRequest
from .timeutil import now_local


def register_context_processors(app):

    @app.context_processor
    def inject_pending_count():
        if 'user_id' in session and session.get('user_role') == 'admin':
            try:
                pending_count = User.query.filter_by(is_approved=False).count()
                return dict(pending_count=pending_count)
            except Exception:
                return dict(pending_count=0)
        return dict(pending_count=0)

    @app.context_processor
    def inject_pending_enrollment_requests():
        if 'user_id' in session and session.get('user_role') == 'teacher':
            try:
                teacher_courses = Course.query.filter_by(teacher_id=session['user_id']).all()
                course_ids = [c.id for c in teacher_courses]

                if course_ids:
                    pending_count = CourseEnrollmentRequest.query.filter(
                        CourseEnrollmentRequest.course_id.in_(course_ids),
                        CourseEnrollmentRequest.status == 'pending'
                    ).count()
                    return dict(pending_enrollment_requests=pending_count)
            except Exception:
                pass
        return dict(pending_enrollment_requests=0)

    @app.context_processor
    def inject_now():
        return dict(now=now_local())

    @app.context_processor
    def inject_user_model():
        return dict(User=User)

    @app.context_processor
    def inject_current_user_role():
        return dict(current_user_role=session.get('user_role'))

    @app.context_processor
    def inject_current_user():
        if 'user_id' in session:
            try:
                cached = getattr(g, 'current_user', None)   # already loaded by the auth decorator
                if cached is not None and cached.id == session['user_id']:
                    return dict(user=cached)
                return dict(user=db.session.get(User, session['user_id']))
            except Exception:
                return dict(user=None)
        return dict(user=None)

    @app.context_processor
    def inject_attendance_helpers():
        """attendance_badge_class() / attendance_percentage() callable from
        any template: red <50%, yellow <75%, green >=75%."""

        def attendance_badge_class(percentage):
            if percentage is None:
                return 'secondary'
            if percentage < 50:
                return 'danger'
            elif percentage < 75:
                return 'warning'
            else:
                return 'success'

        def attendance_percentage(attended, total):
            if not total:
                return 0
            return round((attended / total) * 100, 2)

        return dict(
            attendance_badge_class=attendance_badge_class,
            attendance_percentage=attendance_percentage
        )