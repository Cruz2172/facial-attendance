import secrets
import traceback
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app

from ..audit import audit
from ..extensions import db
from ..timeutil import utcnow, now_local
from ..decorators import login_required, teacher_or_admin_required
from ..models import (
    User, Course, CourseEnrollment, CourseEnrollmentRequest, Student,
    InviteCode, CourseEnrollmentToken
)
from ..utils import (
    query_students_by_user_ids, query_users_by_ids, _chunked,
    count_attendance_records_by_student_and_sessions, get_active_session_for_today,
    invalidate_course_roster_cache, invalidate_face_cache
)
from ..models import AttendanceSession, AttendanceRecord
from ..services.enrollment import delete_face_image

teacher_bp = Blueprint('teacher', __name__)


@teacher_bp.route('/teacher/dashboard')
@login_required
def teacher_dashboard():
    try:
        if session.get('user_role') != 'teacher':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('auth.login'))

        courses = Course.query.filter_by(teacher_id=user.id).all()
        course_ids = [c.id for c in courses]

        total_students = 0
        pending_requests = 0
        active_sessions = 0
        all_student_ids = []

        for course in courses:
            enrolled = course.get_enrolled_count()
            pending = course.get_pending_requests_count()
            total_students += enrolled
            pending_requests += pending

            active = get_active_session_for_today(course.id)
            if active:
                active_sessions += 1

            enrollments = CourseEnrollment.query.filter_by(
                course_id=course.id,
                status='active'
            ).all()
            for enrollment in enrollments:
                if enrollment.student_id not in all_student_ids:
                    all_student_ids.append(enrollment.student_id)

        if all_student_ids:
            face_enrolled_ids = [
                s.user_id for s in query_students_by_user_ids(all_student_ids)
            ]
        else:
            face_enrolled_ids = []

        unique_students = len(face_enrolled_ids)

        course_sessions_map = {}
        if course_ids:
            for batch in _chunked(course_ids):
                for s in AttendanceSession.query.filter(
                    AttendanceSession.course_id.in_(batch)
                ).all():
                    course_sessions_map.setdefault(s.course_id, []).append(s.id)

        students_list = []
        recent_students = []
        if face_enrolled_ids:
            recent_students = sorted(
                query_users_by_ids(face_enrolled_ids),
                key=lambda u: u.created_at or datetime.min,
                reverse=True
            )[:10]

            for student_user in recent_students:
                student_profile = Student.query.filter_by(user_id=student_user.id).first()
                if student_profile:
                    student_course_ids = [
                        e.course_id for e in CourseEnrollment.query.filter(
                            CourseEnrollment.student_id == student_user.id,
                            CourseEnrollment.course_id.in_(course_ids),
                            CourseEnrollment.status == 'active'
                        ).all()
                    ] if course_ids else []

                    session_ids_for_student = []
                    for cid in student_course_ids:
                        session_ids_for_student.extend(course_sessions_map.get(cid, []))

                    total_sessions_for_student = len(session_ids_for_student)
                    attended_sessions = count_attendance_records_by_student_and_sessions(
                        student_user.id, session_ids_for_student
                    )
                    attendance_pct = round(
                        (attended_sessions / total_sessions_for_student) * 100, 2
                    ) if total_sessions_for_student > 0 else 0

                    students_list.append({
                        'id': student_profile.id,
                        'student_id': student_profile.student_id,
                        'fullname': student_profile.fullname,
                        'email': student_profile.email,
                        'course': student_profile.course,
                        'level': student_profile.level,
                        'enrolled_at': student_profile.enrolled_at,
                        'face_enrolled': True,
                        'attended_sessions': attended_sessions,
                        'total_sessions': total_sessions_for_student,
                        'attendance_percentage': attendance_pct
                    })

        return render_template('teacher/dashboard.html',
                             user=user,
                             courses=courses,
                             total_students=unique_students,
                             total_enrollments=total_students,
                             pending_requests=pending_requests,
                             active_sessions=active_sessions,
                             recent_students=recent_students,
                             students=students_list,
                             now=now_local())

    except Exception:
        current_app.logger.exception("Error in teacher dashboard")
        flash('Error loading teacher dashboard.', 'danger')
        return redirect(url_for('main.dashboard'))


@teacher_bp.route('/teacher/enrollment-requests')
@login_required
@teacher_or_admin_required
def manage_enrollment_requests():
    try:
        user = User.query.get(session['user_id'])

        if user.role == 'admin':
            requests = CourseEnrollmentRequest.query.order_by(
                CourseEnrollmentRequest.requested_at.desc()
            ).all()
        else:
            teacher_courses = Course.query.filter_by(teacher_id=user.id).all()
            course_ids = [c.id for c in teacher_courses]

            if course_ids:
                requests = CourseEnrollmentRequest.query.filter(
                    CourseEnrollmentRequest.course_id.in_(course_ids)
                ).order_by(
                    CourseEnrollmentRequest.requested_at.desc()
                ).all()
            else:
                requests = []

        pending_requests = [r for r in requests if r.status == 'pending']
        approved_requests = [r for r in requests if r.status == 'approved']
        rejected_requests = [r for r in requests if r.status == 'rejected']

        # Lookups so the template can show student and course details
        student_ids = list({r.student_id for r in requests})
        users = {u.id: u for u in query_users_by_ids(student_ids)}
        courses = {}
        for cid in {r.course_id for r in requests}:
            c = Course.query.get(cid)
            if c:
                courses[cid] = c

        return render_template('teacher/enrollment_requests.html',
                             pending_requests=pending_requests,
                             approved_requests=approved_requests,
                             rejected_requests=rejected_requests,
                             total=len(requests),
                             users=users,
                             courses=courses)

    except Exception:
        current_app.logger.exception("Error loading enrollment requests")
        flash('Error loading enrollment requests.', 'danger')
        return redirect(url_for('main.dashboard'))


@teacher_bp.route('/teacher/enrollment-requests/<int:request_id>/approve', methods=['POST'])
@login_required
@teacher_or_admin_required
def approve_enrollment_request(request_id):
    try:
        request_obj = CourseEnrollmentRequest.query.get_or_404(request_id)
        course = Course.query.get(request_obj.course_id)

        if session.get('user_role') != 'admin' and course.teacher_id != session['user_id']:
            flash('You do not have permission to approve this request.', 'danger')
            return redirect(url_for('teacher.manage_enrollment_requests'))

        if request_obj.status != 'pending':
            flash(f'This request has already been {request_obj.status}.', 'warning')
            return redirect(url_for('teacher.manage_enrollment_requests'))

        existing = CourseEnrollment.query.filter_by(
            student_id=request_obj.student_id,
            course_id=request_obj.course_id
        ).first()

        if existing:
            flash('Student is already enrolled in this course.', 'info')
            request_obj.status = 'approved'
            request_obj.reviewed_at = utcnow()
            request_obj.reviewed_by = session['user_id']
            db.session.commit()
            return redirect(url_for('teacher.manage_enrollment_requests'))

        enrollment = CourseEnrollment(
            student_id=request_obj.student_id,
            course_id=request_obj.course_id,
            enrolled_at=utcnow(),
            status='active'
        )

        request_obj.status = 'approved'
        request_obj.reviewed_at = utcnow()
        request_obj.reviewed_by = session['user_id']
        request_obj.remarks = 'Approved by teacher'

        db.session.add(enrollment)
        db.session.commit()
        invalidate_course_roster_cache(course.id)

        student = User.query.get(request_obj.student_id)
        flash(f'Enrollment approved for {student.fullname} in {course.course_code}!', 'success')

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error approving enrollment")
        flash('Error approving enrollment. Please try again.', 'danger')

    return redirect(url_for('teacher.manage_enrollment_requests'))


@teacher_bp.route('/teacher/enrollment-requests/<int:request_id>/reject', methods=['POST'])
@login_required
@teacher_or_admin_required
def reject_enrollment_request(request_id):
    try:
        request_obj = CourseEnrollmentRequest.query.get_or_404(request_id)
        course = Course.query.get(request_obj.course_id)

        if session.get('user_role') != 'admin' and course.teacher_id != session['user_id']:
            flash('You do not have permission to reject this request.', 'danger')
            return redirect(url_for('teacher.manage_enrollment_requests'))

        if request_obj.status != 'pending':
            flash(f'This request has already been {request_obj.status}.', 'warning')
            return redirect(url_for('teacher.manage_enrollment_requests'))

        remarks = request.form.get('remarks', 'Rejected by teacher')

        request_obj.status = 'rejected'
        request_obj.reviewed_at = utcnow()
        request_obj.reviewed_by = session['user_id']
        request_obj.remarks = remarks

        db.session.commit()

        student = User.query.get(request_obj.student_id)
        flash(f'Enrollment rejected for {student.fullname} in {course.course_code}.', 'warning')

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error rejecting enrollment")
        flash('Error rejecting enrollment. Please try again.', 'danger')

    return redirect(url_for('teacher.manage_enrollment_requests'))


@teacher_bp.route('/teacher/delete-student/<int:student_id>', methods=['POST'])
@login_required
@teacher_or_admin_required
def teacher_delete_student(student_id):
    """Teacher can delete students from their own courses' rosters; admin
    can delete any student.

    NOTE: role check must happen before the course-ownership check --
    admins generally don't own courses via Course.teacher_id, so
    course_ids is almost always empty for an admin. If the ownership
    branch ran first, every admin would hit "no courses to manage"
    before reaching the admin bypass."""
    try:
        student = Student.query.get_or_404(student_id)
        user = User.query.get(session['user_id'])

        if user.role != 'admin':
            teacher_courses = Course.query.filter_by(teacher_id=user.id).all()
            course_ids = [c.id for c in teacher_courses]

            if not course_ids:
                flash('You have no courses to manage.', 'danger')
                return redirect(url_for('teacher.teacher_dashboard'))

            enrollment = CourseEnrollment.query.filter(
                CourseEnrollment.student_id == student.user_id,
                CourseEnrollment.course_id.in_(course_ids)
            ).first()

            if not enrollment:
                flash('You can only delete students enrolled in your courses.', 'danger')
                return redirect(url_for('teacher.teacher_dashboard'))

            # A student's face data is shared by every course they attend. A
            # teacher may only erase it when no OTHER teacher's course depends on it.
            other_teachers_course = CourseEnrollment.query.join(
                Course, Course.id == CourseEnrollment.course_id
            ).filter(
                CourseEnrollment.student_id == student.user_id,
                Course.teacher_id != user.id,
            ).first()
            if other_teachers_course:
                flash("This student is also enrolled in another teacher's course, so only an "
                      "administrator can remove their face data.", 'danger')
                return redirect(url_for('teacher.teacher_dashboard'))

        student_name = student.fullname
        student_id_num = student.student_id
        stored_image = student.face_image

        # Legacy attendance rows reference students.student_id with a foreign key;
        # on PostgreSQL deleting the student would fail without removing them first.
        AttendanceRecord.query.filter_by(student_id=student_id_num).delete(synchronize_session=False)
        audit('student.delete', 'student', student_id_num, f'name={student_name} by_role={user.role}')
        db.session.delete(student)
        db.session.commit()
        delete_face_image(stored_image)
        invalidate_face_cache()

        flash(f'✅ Student "{student_name}" (ID: {student_id_num}) has been deleted.', 'success')
        return redirect(url_for('teacher.teacher_dashboard'))

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error deleting student")
        flash('Error deleting student. Please try again.', 'danger')
        return redirect(url_for('teacher.teacher_dashboard'))


@teacher_bp.route('/teacher/generate-student-invite', methods=['GET', 'POST'])
@login_required
@teacher_or_admin_required
def generate_student_invite():
    """Generate a single invite link that enrolls students in ALL of the
    teacher's courses."""
    try:
        user = User.query.get(session['user_id'])

        teacher_courses = Course.query.filter_by(teacher_id=user.id, is_active=True).all()

        if not teacher_courses:
            flash('You need to create at least one course first!', 'warning')
            return redirect(url_for('courses.list_courses'))

        if request.method == 'POST':
            existing_invite = InviteCode.query.filter_by(
                created_by=user.id,
                is_teacher_invite=True,
                used=False
            ).first()

            if existing_invite and existing_invite.is_valid():
                invite = existing_invite
                invite_code = existing_invite.code
                flash('✅ Using existing valid invite link.', 'info')
            else:
                code = secrets.token_urlsafe(12)
                expires_at = utcnow() + timedelta(days=30)

                invite = InviteCode(
                    code=code,
                    role='student',
                    is_teacher_invite=True,
                    created_by=user.id,
                    max_uses=1000,
                    expires_at=expires_at,
                    created_at=utcnow(),
                    used=False,
                    uses_count=0
                )

                db.session.add(invite)
                db.session.commit()
                invite_code = code

            courses = Course.query.filter_by(teacher_id=user.id, is_active=True).all()
            registration_link = f"{request.host_url}register?code={invite_code}"

            flash('✅ Student registration link generated successfully!', 'success')

            return render_template('teacher/teacher_invite.html',
                                 teacher=user,
                                 courses=courses,
                                 registration_link=registration_link,
                                 invite_code=invite_code,
                                 invite=invite)

        return render_template('teacher/generate_teacher_invite.html',
                             teacher=user,
                             courses=teacher_courses)

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error generating teacher invite")
        flash('Error generating invite. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))


@teacher_bp.route('/teacher/invite-status')
@login_required
@teacher_or_admin_required
def teacher_invite_status():
    try:
        user = User.query.get(session['user_id'])

        invite = InviteCode.query.filter_by(
            created_by=user.id,
            is_teacher_invite=True
        ).first()

        if not invite:
            return jsonify({
                'has_invite': False,
                'message': 'No invite link found. Generate one first.'
            })

        return jsonify({
            'has_invite': True,
            'code': invite.code,
            'uses_count': invite.uses_count,
            'max_uses': invite.max_uses,
            'expires_at': invite.expires_at.isoformat() if invite.expires_at else None,
            'is_valid': invite.is_valid(),
            'created_at': invite.created_at.isoformat() if invite.created_at else None,
            'link': f"{request.host_url}register?code={invite.code}"
        })

    except Exception:
        current_app.logger.exception("Error checking invite status")
        return jsonify({'error': 'Error checking invite status'}), 500


@teacher_bp.route('/teacher/generate-token', methods=['POST'])
@login_required
@teacher_or_admin_required
def generate_course_token_teacher():
    try:
        course_id = request.form.get('course_id')
        max_uses = request.form.get('max_uses', 50)
        expires_days = request.form.get('expires_days', 7)

        if not course_id:
            flash('Please select a course.', 'danger')
            return redirect(url_for('teacher.manage_links'))

        course = Course.query.get_or_404(course_id)

        if session.get('user_role') != 'admin' and course.teacher_id != session['user_id']:
            flash('You do not have permission to generate links for this course.', 'danger')
            return redirect(url_for('teacher.manage_links'))

        token_code = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(8))
        expires_at = utcnow() + timedelta(days=int(expires_days))

        token = CourseEnrollmentToken(
            course_id=course_id,
            token_code=token_code,
            expires_at=expires_at,
            max_uses=int(max_uses),
            created_by=session['user_id'],
            created_at=utcnow(),
            is_active=True
        )

        db.session.add(token)
        db.session.commit()

        flash(f'Enrollment link generated for {course.course_code}!', 'success')

        enrollment_link = f"{request.host_url}courses/enroll/{token_code}"

        return render_template('teacher/token_generated.html',
                             token=token,
                             course=course,
                             enrollment_link=enrollment_link)

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error generating token")
        flash('Error generating link. Please try again.', 'danger')
        return redirect(url_for('teacher.manage_links'))


@teacher_bp.route('/teacher/manage-links', methods=['GET'])
@login_required
@teacher_or_admin_required
def manage_links():
    try:
        user = User.query.get(session['user_id'])

        if user.role == 'admin':
            teacher_courses = Course.query.all()
        else:
            teacher_courses = Course.query.filter_by(teacher_id=user.id).all()

        active_links = []
        expired_links = []

        for course in teacher_courses:
            tokens = CourseEnrollmentToken.query.filter_by(
                course_id=course.id,
                is_active=True
            ).all()

            for token in tokens:
                entry = {
                    'id': token.id,
                    'token_code': token.token_code,
                    'course': course,
                    'max_uses': token.max_uses,
                    'used_count': token.used_count,
                    'created_at': token.created_at,
                    'expires_at': token.expires_at
                }
                if token.is_valid():
                    active_links.append(entry)
                else:
                    expired_links.append(entry)

        return render_template('teacher/manage_links.html',
                             teacher_courses=teacher_courses,
                             active_links=active_links,
                             expired_links=expired_links)

    except Exception:
        current_app.logger.exception("Error managing links")
        flash('Error loading enrollment links.', 'danger')
        return redirect(url_for('main.dashboard'))