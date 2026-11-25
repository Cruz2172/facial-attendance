import secrets
from datetime import timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, jsonify, current_app

from ..audit import audit
from ..extensions import db, limiter
from ..decorators import login_required, admin_required, teacher_or_admin_required
from ..models import (
    User, Student, Course, InviteCode, AttendanceRecord, CourseEnrollment, AttendanceSession,
    CourseEnrollmentToken, CourseEnrollmentRequest
)
from ..services.enrollment import delete_face_image
from ..timeutil import utcnow, now_local
from ..utils import (
    invalidate_face_cache, invalidate_course_roster_cache, _chunked,
    count_attendance_records_by_student_and_sessions
)

admin_bp = Blueprint('admin', __name__)


def _prepare_user_removal(user):
    """Clear the foreign keys the ORM cascade does not cover.

    Returns an error message when the account cannot be removed without losing
    history (the admin should deactivate it instead), otherwise None. Returns
    the stored face-image name through ``user._face_image_to_delete`` so the
    caller can remove the file after a successful commit."""
    if AttendanceSession.query.filter_by(created_by=user.id).first():
        return (f'{user.fullname} started attendance sessions, so deleting the account would '
                f'lose that history. Deactivate the account instead.')

    CourseEnrollmentToken.query.filter_by(created_by=user.id).delete(synchronize_session=False)
    CourseEnrollmentRequest.query.filter_by(reviewed_by=user.id).update(
        {'reviewed_by': None}, synchronize_session=False)

    student = Student.query.filter_by(user_id=user.id).first()
    user._face_image_to_delete = None
    if student:
        AttendanceRecord.query.filter_by(student_id=student.student_id).delete(synchronize_session=False)
        user._face_image_to_delete = student.face_image
    return None


@admin_bp.route('/admin/delete-student/<int:student_id>', methods=['POST'])
@login_required
@admin_required
def delete_student(student_id):
    """Delete a student record (admin only) — allows re-enrollment."""
    try:
        student = Student.query.get_or_404(student_id)
        student_name = student.fullname
        student_id_num = student.student_id

        stored_image = student.face_image

        AttendanceRecord.query.filter_by(student_id=student_id_num).delete(synchronize_session=False)
        audit('student.delete', 'student', student_id_num, f'name={student_name} by_role=admin')
        db.session.delete(student)
        db.session.commit()
        delete_face_image(stored_image)
        invalidate_face_cache()

        flash(f'✅ Student "{student_name}" (ID: {student_id_num}) has been deleted. They can now re-enroll.', 'success')
        return jsonify({'success': True, 'message': f'Student {student_name} deleted successfully'})

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error deleting student")
        return jsonify({'success': False, 'error': 'Error deleting student'}), 500


@admin_bp.route('/admin/dashboard')
@login_required
def admin_dashboard():
    try:
        if session.get('user_role') != 'admin':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])
        if not user:
            flash('User not found.', 'danger')
            return redirect(url_for('auth.login'))

        total_users = User.query.count()
        total_students = Student.query.count()
        total_courses = Course.query.count()
        pending_approvals = User.query.filter_by(is_approved=False).count()

        all_active_enrollments = CourseEnrollment.query.filter_by(status='active').all()
        enrolled_course_ids_by_user = {}
        for e in all_active_enrollments:
            enrolled_course_ids_by_user.setdefault(e.student_id, []).append(e.course_id)

        all_course_ids = list({e.course_id for e in all_active_enrollments})
        course_sessions_map = {}
        if all_course_ids:
            for batch in _chunked(all_course_ids):
                for s in AttendanceSession.query.filter(
                    AttendanceSession.course_id.in_(batch)
                ).all():
                    course_sessions_map.setdefault(s.course_id, []).append(s.id)

        all_students = Student.query.order_by(Student.enrolled_at.desc()).all()
        students_data = []
        for student in all_students:
            user_account = User.query.get(student.user_id)

            student_course_ids = enrolled_course_ids_by_user.get(student.user_id, [])
            session_ids_for_student = []
            for cid in student_course_ids:
                session_ids_for_student.extend(course_sessions_map.get(cid, []))

            total_sessions_for_student = len(session_ids_for_student)
            attended_sessions = count_attendance_records_by_student_and_sessions(
                student.user_id, session_ids_for_student
            )
            attendance_pct = round(
                (attended_sessions / total_sessions_for_student) * 100, 2
            ) if total_sessions_for_student > 0 else 0

            students_data.append({
                'id': student.id,
                'student_id': student.student_id,
                'fullname': student.fullname,
                'email': student.email,
                'course': student.course,
                'level': student.level,
                'enrolled_at': student.enrolled_at,
                'user_account': user_account,
                'attended_sessions': attended_sessions,
                'total_sessions': total_sessions_for_student,
                'attendance_percentage': attendance_pct
            })

        recent_users = User.query.order_by(User.created_at.desc()).limit(10).all()
        recent_attendance = AttendanceRecord.query.order_by(
            AttendanceRecord.timestamp.desc()
        ).limit(10).all()

        return render_template('admin/dashboard.html',
                             user=user,
                             total_users=total_users,
                             total_students=total_students,
                             total_courses=total_courses,
                             pending_approvals=pending_approvals,
                             recent_users=recent_users,
                             recent_attendance=recent_attendance,
                             students=students_data,
                             now=now_local())

    except Exception:
        current_app.logger.exception("Error in admin dashboard")
        flash('Error loading admin dashboard.', 'danger')
        return redirect(url_for('main.dashboard'))


@admin_bp.route('/admin/users')
@login_required
@admin_required
def manage_users():
    try:
        users = User.query.order_by(User.created_at.desc()).all()

        stats = {
            'total': len(users),
            'pending': sum(1 for u in users if not u.is_approved),
            'approved': sum(1 for u in users if u.is_approved),
            'admins': sum(1 for u in users if u.role == 'admin'),
            'teachers': sum(1 for u in users if u.role == 'teacher'),
            'students': sum(1 for u in users if u.role == 'student'),
        }

        return render_template('admin/users.html', users=users, stats=stats)
    except Exception:
        current_app.logger.exception("Error loading users")
        flash('Error loading users.', 'danger')
        return redirect(url_for('main.dashboard'))


@admin_bp.route('/admin/approve_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def approve_user(user_id):
    try:
        user = User.query.get_or_404(user_id)

        if user.is_approved:
            flash(f'User {user.fullname} is already approved.', 'info')
            return redirect(url_for('admin.manage_users'))

        user.is_approved = True
        audit('user.approve', 'user', user.id)
        db.session.commit()

        flash(f'User {user.fullname} has been approved!', 'success')
        return redirect(url_for('admin.manage_users'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error approving user")
        flash('Error approving user.', 'danger')
        return redirect(url_for('admin.manage_users'))


@admin_bp.route('/admin/reject_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def reject_user(user_id):
    try:
        user = User.query.get_or_404(user_id)

        if user.is_approved:
            flash('Cannot reject an approved user.', 'warning')
            return redirect(url_for('admin.manage_users'))

        owned_courses_count = Course.query.filter_by(teacher_id=user.id).count()
        if owned_courses_count > 0:
            flash(
                f'{user.fullname} still owns {owned_courses_count} course(s). '
                f'Reassign or delete those courses first before rejecting this account.',
                'danger'
            )
            return redirect(url_for('admin.manage_users'))

        user_name = user.fullname

        problem = _prepare_user_removal(user)
        if problem:
            db.session.rollback()
            flash(problem, 'danger')
            return redirect(url_for('admin.manage_users'))

        InviteCode.query.filter_by(used_by=user.id).update({'used_by': None})
        InviteCode.query.filter_by(created_by=user.id).delete()

        stored_image = getattr(user, '_face_image_to_delete', None)
        audit('user.delete', 'user', user.id, f'name={user_name} role={user.role}')
        db.session.delete(user)
        db.session.commit()
        delete_face_image(stored_image)
        invalidate_face_cache()

        flash(f'User {user_name} has been rejected and removed.', 'danger')
        return redirect(url_for('admin.manage_users'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error rejecting user")
        flash('Error rejecting user.', 'danger')
        return redirect(url_for('admin.manage_users'))

@admin_bp.route('/admin/change_role/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def change_user_role(user_id):
    try:
        user = User.query.get_or_404(user_id)

        if user.id == session['user_id']:
            flash('You cannot change your own role!', 'warning')
            return redirect(url_for('admin.manage_users'))

        new_role = request.form.get('role')
        if new_role not in ('admin', 'teacher', 'student'):
            flash('Invalid role selected!', 'danger')
            return redirect(url_for('admin.manage_users'))

        if new_role == 'student':
            owned_courses_count = Course.query.filter_by(teacher_id=user.id).count()
            if owned_courses_count > 0:
                flash(
                    f'{user.fullname} still owns {owned_courses_count} course(s). '
                    f'Reassign or delete those courses first before changing this account to student.',
                    'danger'
                )
                return redirect(url_for('admin.manage_users'))

        old_role = user.role
        user.role = new_role
        audit('user.change_role', 'user', user.id, f'{old_role} -> {new_role}')
        db.session.commit()

        flash(f'Role updated from {old_role} to {new_role} for {user.fullname}', 'success')
        return redirect(url_for('admin.manage_users'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error changing user role")
        flash('Error changing role.', 'danger')
        return redirect(url_for('admin.manage_users'))


@admin_bp.route('/admin/toggle_approval/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def toggle_approval(user_id):
    try:
        user = User.query.get_or_404(user_id)

        if user.id == session['user_id']:
            flash('You cannot change your own approval status!', 'warning')
            return redirect(url_for('admin.manage_users'))

        user.is_approved = not user.is_approved
        audit('user.toggle_approval', 'user', user.id, f'is_approved={user.is_approved}')
        db.session.commit()

        status = "approved" if user.is_approved else "unapproved"
        flash(f'{user.fullname} has been {status}.', 'success' if user.is_approved else 'warning')
        return redirect(url_for('admin.manage_users'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error toggling approval")
        flash('Error toggling approval.', 'danger')
        return redirect(url_for('admin.manage_users'))


@admin_bp.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
@admin_required
def delete_user(user_id):
    try:
        user = User.query.get_or_404(user_id)

        if user.id == session['user_id']:
            flash('You cannot delete your own account!', 'warning')
            return redirect(url_for('admin.manage_users'))

        owned_courses_count = Course.query.filter_by(teacher_id=user.id).count()
        if owned_courses_count > 0:
            flash(
                f'{user.fullname} still owns {owned_courses_count} course(s). '
                f'Reassign or delete those courses first before deleting this account.',
                'danger'
            )
            return redirect(url_for('admin.manage_users'))

        user_name = user.fullname

        # InviteCode references a user two ways:
        #  - used_by (nullable): whoever redeemed the code -- clear it,
        #    the invite row itself stays as history.
        #  - created_by (NOT NULL): whoever generated the code -- can't
        #    be nulled, so any invite this user created must go too.
        problem = _prepare_user_removal(user)
        if problem:
            db.session.rollback()
            flash(problem, 'danger')
            return redirect(url_for('admin.manage_users'))

        InviteCode.query.filter_by(used_by=user.id).update({'used_by': None})
        InviteCode.query.filter_by(created_by=user.id).delete()

        stored_image = getattr(user, '_face_image_to_delete', None)
        audit('user.delete', 'user', user.id, f'name={user_name} role={user.role}')
        db.session.delete(user)
        db.session.commit()
        delete_face_image(stored_image)
        invalidate_face_cache()

        flash(f'User {user_name} has been deleted.', 'danger')
        return redirect(url_for('admin.manage_users'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error deleting user")
        flash('Error deleting user.', 'danger')
        return redirect(url_for('admin.manage_users'))

@admin_bp.route('/admin/invites')
@login_required
@admin_required
def manage_invites():
    try:
        invites = InviteCode.query.order_by(InviteCode.created_at.desc()).all()
        return render_template('admin/invites.html', invites=invites, now=utcnow())
    except Exception:
        current_app.logger.exception("Error loading invites")
        flash('Error loading invites.', 'danger')
        return redirect(url_for('main.dashboard'))


@admin_bp.route('/admin/generate_invite', methods=['GET', 'POST'])
@login_required
@admin_required
def generate_invite():
    if request.method == 'POST':
        try:
            role = request.form.get('role', 'student')
            max_uses_raw = request.form.get('max_uses', '1')
            expires_days = request.form.get('expires_days')

            if role not in ('admin', 'teacher', 'student'):
                flash('Invalid role selected!', 'danger')
                return redirect(url_for('admin.generate_invite'))

            if max_uses_raw and max_uses_raw.strip() and not max_uses_raw.strip().lstrip('-').isdigit():
                flash('Max uses must be a whole number.', 'danger')
                return redirect(url_for('admin.generate_invite'))

            max_uses = int(max_uses_raw) if max_uses_raw and max_uses_raw.strip() else 1

            code = secrets.token_urlsafe(12)

            if expires_days and expires_days.isdigit():
                expires_at = utcnow() + timedelta(days=int(expires_days))
            else:
                expires_at = utcnow() + timedelta(days=7)

            invite = InviteCode(
                code=code,
                role=role,
                created_by=session['user_id'],
                max_uses=max_uses,
                expires_at=expires_at,
                created_at=utcnow(),
                used=False,
                uses_count=0
            )

            db.session.add(invite)
            db.session.commit()

            flash(f'{role.capitalize()} invite code generated successfully!', 'success')

            invite_link = f"{request.host_url}register?code={code}"
            return render_template('admin/invite_success.html',
                                 invite=invite,
                                 invite_link=invite_link)

        except Exception:
            db.session.rollback()
            current_app.logger.exception("Error generating invite")
            flash('Error generating invite. Please try again.', 'danger')
            return redirect(url_for('admin.manage_invites'))

    return render_template('admin/generate_invite.html')


@admin_bp.route('/admin/delete_invite/<int:invite_id>', methods=['POST'])
@login_required
@admin_required
def delete_invite(invite_id):
    try:
        invite = InviteCode.query.get_or_404(invite_id)

        if invite.used:
            flash('Cannot delete a used invite code.', 'warning')
            return redirect(url_for('admin.manage_invites'))

        db.session.delete(invite)
        db.session.commit()
        flash('Invite code deleted successfully.', 'success')
        return redirect(url_for('admin.manage_invites'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error deleting invite")
        flash('Error deleting invite.', 'danger')
        return redirect(url_for('admin.manage_invites'))


@admin_bp.route('/admin/check_invite')
@limiter.limit('60 per minute')
def check_invite():
    """Public (no login) lightweight JSON check used by the registration
    page's live invite-code validator -- see register.html. Must be
    reachable by someone who isn't logged in yet, since this runs while
    they're still filling out the registration form."""
    code = request.args.get('code', '').strip()

    if not code:
        return jsonify({'valid': False})

    invite = InviteCode.query.filter_by(code=code).first()

    if not invite or not invite.is_valid():
        return jsonify({'valid': False})

    return jsonify({
        'valid': True,
        'role': invite.role
    })


@admin_bp.route('/admin/courses')
@login_required
@admin_required
def list_courses():
    try:
        courses = Course.query.order_by(Course.course_name).all()
        course_ids = [c.id for c in courses]

        enrollment_counts = {}
        session_counts = {}
        if course_ids:
            for batch in _chunked(course_ids):
                for cid, count in db.session.query(
                    CourseEnrollment.course_id, db.func.count(CourseEnrollment.id)
                ).filter(
                    CourseEnrollment.course_id.in_(batch),
                    CourseEnrollment.status == 'active'
                ).group_by(CourseEnrollment.course_id).all():
                    enrollment_counts[cid] = count

                for cid, count in db.session.query(
                    AttendanceSession.course_id, db.func.count(AttendanceSession.id)
                ).filter(
                    AttendanceSession.course_id.in_(batch)
                ).group_by(AttendanceSession.course_id).all():
                    session_counts[cid] = count

        teacher_ids = list({c.teacher_id for c in courses if c.teacher_id})
        teachers_map = {}
        if teacher_ids:
            teachers_map = {u.id: u for u in User.query.filter(User.id.in_(teacher_ids)).all()}

        courses_data = []
        for course in courses:
            teacher = teachers_map.get(course.teacher_id)
            courses_data.append({
                'id': course.id,
                'course_code': course.course_code,
                'course_name': course.course_name,
                'is_active': course.is_active,
                'teacher_name': teacher.fullname if teacher else 'Unassigned',
                'student_count': enrollment_counts.get(course.id, 0),
                'session_count': session_counts.get(course.id, 0),
            })

        return render_template('admin/courses.html', courses=courses_data)
    except Exception:
        current_app.logger.exception("Error loading courses")
        flash('Error loading courses.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/students/search')
@login_required
@teacher_or_admin_required
def search_students_page():
    """Renders the interactive search page. All data loading, filtering,
    export and delete actions happen client-side via JS calling the real
    JSON endpoints (legacy.search_students, admin.delete_student) -- this
    route just needs to pass the viewer's role so the page can show the
    right copy and hide admin-only actions."""
    user = User.query.get(session['user_id'])
    return render_template('admin/search_students.html', current_user_role=user.role)


@admin_bp.route('/admin/attendance-report')
@login_required
@admin_required
def attendance_report():
    try:
        course_id_filter = request.args.get('course', type=int)

        courses = Course.query.order_by(Course.course_name).all()

        enrollments_query = CourseEnrollment.query.filter_by(status='active')
        if course_id_filter:
            enrollments_query = enrollments_query.filter_by(course_id=course_id_filter)
        all_active_enrollments = enrollments_query.all()

        enrolled_course_ids_by_user = {}
        for e in all_active_enrollments:
            enrolled_course_ids_by_user.setdefault(e.student_id, []).append(e.course_id)

        all_course_ids = list({e.course_id for e in all_active_enrollments})
        course_sessions_map = {}
        if all_course_ids:
            for batch in _chunked(all_course_ids):
                for s in AttendanceSession.query.filter(
                    AttendanceSession.course_id.in_(batch)
                ).all():
                    course_sessions_map.setdefault(s.course_id, []).append(s.id)

        student_user_ids = list(enrolled_course_ids_by_user.keys())
        students_map = {}
        if student_user_ids:
            students_map = {
                s.user_id: s for s in Student.query.filter(
                    Student.user_id.in_(student_user_ids)
                ).all()
            }

        report_data = []
        for user_id, course_ids in enrolled_course_ids_by_user.items():
            student = students_map.get(user_id)
            if not student:
                continue

            session_ids_for_student = []
            for cid in course_ids:
                session_ids_for_student.extend(course_sessions_map.get(cid, []))

            total_sessions = len(session_ids_for_student)
            attended = count_attendance_records_by_student_and_sessions(
                user_id, session_ids_for_student
            )
            percentage = round((attended / total_sessions) * 100, 2) if total_sessions > 0 else 0

            report_data.append({
                'fullname': student.fullname,
                'student_id': student.student_id,
                'email': student.email,
                'attended': attended,
                'total_sessions': total_sessions,
                'percentage': percentage
            })

        report_data.sort(key=lambda r: r['percentage'])

        return render_template('admin/attendance_report.html',
                             report_data=report_data,
                             courses=courses,
                             selected_course=course_id_filter)
    except Exception:
        current_app.logger.exception("Error generating attendance report")
        flash('Error generating attendance report.', 'danger')
        return redirect(url_for('admin.admin_dashboard'))