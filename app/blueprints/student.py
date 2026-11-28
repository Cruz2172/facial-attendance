import re
from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app, jsonify
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, OperationalError

from werkzeug.security import check_password_hash, generate_password_hash
from ..extensions import db, limiter, user_or_ip
from ..decorators import login_required
from ..audit import audit
from ..timeutil import utcnow, now_local
from ..models import (
    User, Student, Course, CourseEnrollment, CourseEnrollmentRequest,
    AttendanceSession, CourseAttendanceRecord, InviteCode, CourseEnrollmentToken
)
from ..utils import invalidate_face_cache, invalidate_course_roster_cache
from ..face_core import FaceBusy, serialize_encodings
from ..services.enrollment import (
    EnrollmentError, validate_enrollment_form, check_not_already_enrolled,
    parse_face_images, encode_enrollment_images, reject_if_duplicate, check_pose_frame,
    save_face_image
)

student_bp = Blueprint('student', __name__)

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


# ─────────────────────────────────────────────
# Student Dashboard
# ─────────────────────────────────────────────

@student_bp.route('/student/dashboard')
@login_required
def student_dashboard():
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

        if user.role != 'student':
            flash('Access denied. Student only area.', 'danger')
            return redirect(url_for('main.dashboard'))

        student_profile = Student.query.filter_by(user_id=user.id).first()
        has_face_enrolled = student_profile is not None

        invite_code = request.args.get('code', '')

        return render_template('student/dashboard.html',
                             user=user,
                             has_face_enrolled=has_face_enrolled,
                             invite_code=invite_code,
                             now=now_local())

    except Exception:
        current_app.logger.exception("Student dashboard error")
        flash('Error loading student dashboard. Please try again.', 'danger')
        return redirect(url_for('main.home'))


@student_bp.route('/student/courses')
@login_required
def student_courses():
    try:
        if session.get('user_role') != 'student':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])

        enrollments = CourseEnrollment.query.filter_by(
            student_id=user.id,
            status='active'
        ).all()

        course_ids = [e.course_id for e in enrollments]
        courses_map = {}
        if course_ids:
            courses_map = {c.id: c for c in Course.query.filter(Course.id.in_(course_ids)).all()}
        courses = [courses_map[cid] for cid in course_ids if cid in courses_map]

        course_stats = {}
        for course in courses:
            course_sessions = AttendanceSession.query.filter_by(course_id=course.id).all()
            session_ids = [s.id for s in course_sessions]
            total = len(session_ids)

            attended = 0
            if session_ids:
                attended = CourseAttendanceRecord.query.filter(
                    CourseAttendanceRecord.student_id == user.id,
                    CourseAttendanceRecord.session_id.in_(session_ids)
                ).count()

            percentage = round((attended / total) * 100, 2) if total > 0 else 0

            course_stats[course.id] = {
                'total_sessions': total,
                'attended': attended,
                'percentage': percentage
            }

        return render_template('student/my_courses.html',
                             courses=courses,
                             course_stats=course_stats)

    except Exception:
        current_app.logger.exception("Student courses error")
        flash('Error loading courses.', 'danger')
        return redirect(url_for('main.dashboard'))


@student_bp.route('/student/course/<int:course_id>')
@login_required
def student_course_detail(course_id):
    try:
        if session.get('user_role') != 'student':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        course = Course.query.get_or_404(course_id)
        user = User.query.get(session['user_id'])

        enrollment = CourseEnrollment.query.filter_by(
            student_id=user.id,
            course_id=course_id,
            status='active'
        ).first()

        if not enrollment:
            flash('You are not enrolled in this course.', 'danger')
            return redirect(url_for('student.student_courses'))

        course_sessions = AttendanceSession.query.filter_by(
            course_id=course_id
        ).order_by(AttendanceSession.session_date.desc()).all()

        session_ids = [s.id for s in course_sessions]
        attended_session_ids = []
        if session_ids:
            attended_session_ids = [
                r.session_id for r in CourseAttendanceRecord.query.filter(
                    CourseAttendanceRecord.student_id == user.id,
                    CourseAttendanceRecord.session_id.in_(session_ids)
                ).all()
            ]

        total = len(course_sessions)
        attended = len(attended_session_ids)
        percentage = round((attended / total) * 100, 2) if total > 0 else 0

        stats = {
            'total_sessions': total,
            'attended': attended,
            'percentage': percentage
        }

        return render_template('student/course_detail.html',
                             course=course,
                             sessions=course_sessions,
                             attended_session_ids=attended_session_ids,
                             stats=stats)

    except Exception:
        current_app.logger.exception("Student course detail error")
        flash('Error loading course details.', 'danger')
        return redirect(url_for('student.student_courses'))


@student_bp.route('/student/attendance-history')
@login_required
def student_attendance_history():
    try:
        if session.get('user_role') != 'student':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])

        course_id = request.args.get('course', type=int)
        from_date = request.args.get('from_date')
        to_date = request.args.get('to_date')

        enrollments = CourseEnrollment.query.filter_by(
            student_id=user.id,
            status='active'
        ).all()
        course_ids = [e.course_id for e in enrollments]
        courses_map = {}
        if course_ids:
            courses_map = {c.id: c for c in Course.query.filter(Course.id.in_(course_ids)).all()}
        courses = [courses_map[cid] for cid in course_ids if cid in courses_map]

        query = CourseAttendanceRecord.query.filter_by(student_id=user.id)

        if course_id:
            sessions_in_course = AttendanceSession.query.filter_by(course_id=course_id).all()
            session_ids = [s.id for s in sessions_in_course]
            query = query.filter(CourseAttendanceRecord.session_id.in_(session_ids))

        from_date_obj = None
        to_date_obj = None

        if from_date:
            try:
                from_date_obj = datetime.strptime(from_date, '%Y-%m-%d')
                query = query.filter(CourseAttendanceRecord.marked_at >= from_date_obj)
            except ValueError:
                from_date_obj = None

        if to_date:
            try:
                to_date_obj = datetime.strptime(to_date, '%Y-%m-%d')
                to_date_obj = to_date_obj + timedelta(days=1)
                query = query.filter(CourseAttendanceRecord.marked_at <= to_date_obj)
            except ValueError:
                to_date_obj = None

        records = query.order_by(CourseAttendanceRecord.marked_at.desc()).all()

        # Only count sessions from the course being filtered on, if any,
        # so the attendance rate matches what present_count actually measures.
        relevant_courses = [c for c in courses if c.id == course_id] if course_id else courses

        all_sessions = []
        for course in relevant_courses:
            sess_query = AttendanceSession.query.filter_by(course_id=course.id)
            # Apply the same date range used to filter attendance records,
            # so total_sessions and present_count refer to the same window.
            if from_date_obj:
                sess_query = sess_query.filter(AttendanceSession.session_date >= from_date_obj)
            if to_date_obj:
                sess_query = sess_query.filter(AttendanceSession.session_date <= to_date_obj)
            all_sessions.extend(sess_query.all())

        total_sessions = len(all_sessions)
        present_count = len(records)
        absent_count = total_sessions - present_count
        attendance_rate = round((present_count / total_sessions) * 100, 2) if total_sessions > 0 else 0

        session_ids_needed = [r.session_id for r in records]
        sessions_map = {}
        if session_ids_needed:
            sessions_map = {
                s.id: s for s in AttendanceSession.query.filter(
                    AttendanceSession.id.in_(session_ids_needed)
                ).all()
            }

        formatted_records = []
        for record in records:
            att_session = sessions_map.get(record.session_id)
            if att_session:
                course = courses_map.get(att_session.course_id)
                formatted_records.append({
                    'course_code': course.course_code if course else 'Unknown',
                    'course_name': course.course_name if course else 'Unknown',
                    'date': att_session.session_date,
                    'time': att_session.session_time,
                    'topic': att_session.topic,
                    'marked_at': record.marked_at
                })

        return render_template('student/attendance_history.html',
                             records=formatted_records,
                             courses=courses,
                             selected_course=course_id,
                             from_date=from_date,
                             to_date=to_date,
                             total_sessions=total_sessions,
                             present_count=present_count,
                             absent_count=absent_count,
                             attendance_rate=attendance_rate)

    except Exception as e:
        current_app.logger.error(f"Student attendance history error: {str(e)}")
        flash('Error loading attendance history.', 'danger')
        return redirect(url_for('main.dashboard'))


@student_bp.route('/student/profile', methods=['GET', 'POST'])
@login_required
def student_profile():
    try:
        if session.get('user_role') != 'student':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])
        student = Student.query.filter_by(user_id=user.id).first()

        if request.method == 'POST':
            fullname = (request.form.get('fullname') or '').strip()
            email = (request.form.get('email') or '').strip().lower()
            course = (request.form.get('course') or '').strip()
            level = (request.form.get('level') or '').strip()
            # NOTE: the Student ID is intentionally NOT editable here. It is the
            # student's identity in attendance records, so it is set once when
            # the face is enrolled and afterwards only an administrator can change
            # it. (This page also used to create a Student row with an EMPTY face
            # encoding, which then blocked the student from ever enrolling.)

            if len(fullname) > 120 or len(email) > 120 or len(course) > 120 or len(level) > 20:
                flash('One of the fields is too long.', 'danger')
                return redirect(url_for('student.student_profile'))

            if email and email != (user.email or '').lower():
                if not _EMAIL_RE.match(email):
                    flash('Please enter a valid email address.', 'danger')
                    return redirect(url_for('student.student_profile'))
                existing = User.query.filter(func.lower(User.email) == email, User.id != user.id).first()
                if existing:
                    flash('Email is already taken.', 'danger')
                    return redirect(url_for('student.student_profile'))
                if student:
                    other = Student.query.filter(
                        func.lower(Student.email) == email, Student.id != student.id).first()
                    if other:
                        flash('Email is already used by another enrolled student.', 'danger')
                        return redirect(url_for('student.student_profile'))
                user.email = email
                session['user_email'] = email

            if fullname:
                user.fullname = fullname
                session['user_name'] = fullname

            face_changed = False
            if student:
                if course:
                    student.course = course
                if level:
                    student.level = level
                if fullname:
                    student.fullname = fullname
                if email:
                    student.email = email
                face_changed = True        # cached face records carry name/e-mail/course

            db.session.commit()
            if face_changed:
                invalidate_face_cache()
            flash('Profile updated successfully!', 'success')
            return redirect(url_for('student.student_profile'))

        return render_template('student/profile.html',
                             user=user,
                             student=student)

    except IntegrityError:
        db.session.rollback()
        current_app.logger.exception("Student profile IntegrityError")
        flash('That Student ID or email is already in use. Please choose another.', 'danger')
        return redirect(url_for('student.student_profile'))

    except OperationalError:
        db.session.rollback()
        current_app.logger.exception("Student profile OperationalError")
        flash('Database is busy. Please try again in a moment.', 'danger')
        return redirect(url_for('student.student_profile'))

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Student profile error")
        flash('Error updating profile. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))


@student_bp.route('/student/change-password', methods=['POST'])
@login_required
def student_change_password():
    try:
        if session.get('user_role') != 'student':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])

        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')

        if not all([current_password, new_password, confirm_password]):
            flash('All fields are required.', 'danger')
            return redirect(url_for('student.student_profile'))

        if new_password != confirm_password:
            flash('New passwords do not match.', 'danger')
            return redirect(url_for('student.student_profile'))

        min_len = current_app.config['MIN_PASSWORD_LENGTH']
        if len(new_password) < min_len or len(new_password) > 128:
            flash(f'Password must be between {min_len} and 128 characters.', 'danger')
            return redirect(url_for('student.student_profile'))

        if not check_password_hash(user.password, current_password):
            flash('Current password is incorrect.', 'danger')
            return redirect(url_for('student.student_profile'))

        user.password = generate_password_hash(new_password)
        db.session.commit()

        flash('Password changed successfully!', 'success')
        return redirect(url_for('student.student_profile'))

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Student change password error")
        flash('Error changing password. Please try again.', 'danger')
        return redirect(url_for('student.student_profile'))


@student_bp.route('/student/attendance-stats')
@login_required
def student_attendance_stats():
    try:
        if session.get('user_role') != 'student':
            flash('Access denied.', 'danger')
            return redirect(url_for('main.dashboard'))

        user = User.query.get(session['user_id'])

        enrollments = CourseEnrollment.query.filter_by(
            student_id=user.id,
            status='active'
        ).all()
        course_ids = [e.course_id for e in enrollments]
        courses = Course.query.filter(Course.id.in_(course_ids)).all() if course_ids else []

        return render_template('student/attendance_stats.html', courses=courses)

    except Exception:
        current_app.logger.exception("Student attendance stats error")
        flash('Error loading statistics.', 'danger')
        return redirect(url_for('main.dashboard'))


# ─────────────────────────────────────────────
# Face Enrollment
# ─────────────────────────────────────────────

@student_bp.route('/enroll/pose-check', methods=['POST'])
@limiter.limit(lambda: current_app.config['POSE_CHECK_RATE_LIMIT'], key_func=user_or_ip)
@login_required
def enroll_pose_check():
    """Live guidance for the guided capture page.

    The browser posts a small camera frame about twice a second, together with
    the number of the pose being asked for (0 = straight, 1 = left, ...). The
    reply says whether the student is in the right position and, if not, what to
    fix. The page only takes a photo after several consecutive good replies; the
    final /enroll request is re-checked on the server regardless."""
    if session.get('user_role') != 'student':
        return jsonify({'success': False, 'message': 'Only students can enroll a face.'}), 403

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'success': False, 'message': 'Invalid request.'}), 400

    try:
        result = check_pose_frame(data.get('image'), data.get('pose_index'), data.get('baseline'))
    except FaceBusy:
        return jsonify({'success': False, 'busy': True, 'message': 'Server is busy, retrying...'}), 503
    except EnrollmentError as exc:
        return jsonify({'success': False, 'message': str(exc)}), 400
    except Exception:
        current_app.logger.exception('Pose check failed')
        return jsonify({'success': False, 'message': 'Could not check the frame. Retrying...'}), 500

    return jsonify({'success': True, **result})


@student_bp.route('/enroll', methods=['GET', 'POST'])
@limiter.limit(lambda: current_app.config['ENROLL_RATE_LIMIT'], key_func=user_or_ip, methods=['POST'])
@login_required
def enroll():
    if session.get('user_role') != 'student':
        flash('Only students can enroll a face.', 'danger')
        return redirect(url_for('main.dashboard'))

    if request.method != 'POST':
        return render_template('enroll.html')

    from_dashboard = request.form.get('from_dashboard', 'false')

    def fail(message):
        flash(message, 'warning' if message.startswith('⚠️') else 'danger')
        if from_dashboard == 'true':
            return redirect(url_for('student.student_dashboard'))
        return render_template('enroll.html')

    student_id = request.form.get('student_id', '').strip()
    fullname = request.form.get('fullname', '').strip()
    email = request.form.get('email', '').strip().lower()
    course = request.form.get('course', '').strip()
    level = request.form.get('level', '').strip()
    invite_code = request.form.get('invite_code', '').strip()
    consent = request.form.get('consent') in ('on', 'true', '1', 'yes')

    try:
        face_images = parse_face_images(request.form)
        validate_enrollment_form(student_id, fullname, email, course, level, face_images, consent)
        incomplete = check_not_already_enrolled(session['user_id'], student_id, email)

        # One template per photo (different head positions / expressions).
        # Each photo must contain exactly one face and all must be the same person.
        encodings, first_photo_bgr = encode_enrollment_images(face_images)
        reject_if_duplicate(encodings, user_id=session['user_id'])

        face_encoding = serialize_encodings(encodings)
        face_path = save_face_image(first_photo_bgr, student_id)    # only if STORE_FACE_IMAGES=true

        if incomplete is not None:
            # Finish the half-created profile instead of failing forever.
            new_student = incomplete
            new_student.student_id = student_id
            new_student.fullname = fullname
            new_student.email = email
            new_student.course = course
            new_student.level = level
            new_student.face_encoding = face_encoding
            new_student.face_image = face_path
            new_student.enrolled_at = utcnow()
        else:
            new_student = Student(
                user_id=session['user_id'],
                student_id=student_id,
                fullname=fullname,
                email=email,
                course=course,
                level=level,
                face_encoding=face_encoding,
                face_image=face_path,
                enrolled_at=utcnow()
            )
            db.session.add(new_student)

        audit('student.face_enroll', 'student', student_id,
              f'user_id={session["user_id"]} templates={len(encodings)}')
        db.session.commit()
        invalidate_face_cache()

        _handle_invite_enrollment(new_student, invite_code, fullname)

        if from_dashboard == 'true':
            return redirect(url_for('student.student_dashboard'))
        return redirect(url_for('main.dashboard'))

    except EnrollmentError as e:
        return fail(str(e))

    except IntegrityError as e:
        db.session.rollback()
        current_app.logger.exception("Enrollment IntegrityError")
        error_text = str(e).lower()
        if 'student_id' in error_text:
            return fail('❌ This Student ID is already registered!')
        elif 'email' in error_text:
            return fail('❌ This email is already registered!')
        elif 'user_id' in error_text:
            return fail('❌ You already have a student profile!')
        return fail('A database error occurred. Please try again.')

    except OperationalError as e:
        db.session.rollback()
        current_app.logger.exception("Enrollment OperationalError")
        if 'database is locked' in str(e):
            return fail('Database is busy. Please try again in a moment.')
        return fail('A database error occurred. Please try again.')

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Unexpected error during enrollment")
        return fail('Error during enrollment. Please try again.')


def _handle_invite_enrollment(new_student, invite_code, fullname):
    """Best-effort: bulk-enroll the newly face-registered student into
    every active course of the teacher who issued the invite. A failure
    here never blocks the enrollment that already succeeded above -- it's
    logged and the student still gets a success message."""
    if not invite_code:
        flash(f'✅ Student "{fullname}" enrolled successfully! Your face is now registered.', 'success')
        return

    try:
        invite = InviteCode.query.filter_by(
            code=invite_code, is_teacher_invite=True, used=False
        ).first()

        if not invite:
            flash(f'✅ Student "{fullname}" enrolled successfully!', 'success')
            return

        teacher = User.query.get(invite.created_by)
        if not teacher:
            flash(f'✅ Student "{fullname}" enrolled successfully!', 'success')
            return

        teacher_courses = Course.query.filter_by(teacher_id=teacher.id, is_active=True).all()
        enrolled_count = 0
        for course_obj in teacher_courses:
            existing = CourseEnrollment.query.filter_by(
                student_id=new_student.user_id, course_id=course_obj.id
            ).first()
            if not existing:
                db.session.add(CourseEnrollment(
                    student_id=new_student.user_id,
                    course_id=course_obj.id,
                    enrolled_at=utcnow(),
                    status='active'
                ))
                enrolled_count += 1

        if enrolled_count > 0:
            db.session.commit()
            invalidate_course_roster_cache()
            flash(f'✅ Student "{fullname}" enrolled successfully and added to {enrolled_count} course(s)!', 'success')
        else:
            flash(f'✅ Student "{fullname}" enrolled successfully!', 'success')

    except Exception:
        current_app.logger.exception("Error enrolling in courses via invite")
        flash(f'✅ Student "{fullname}" enrolled successfully!', 'success')


# ─────────────────────────────────────────────
# Student Course Enrollment (browse / request)
# ─────────────────────────────────────────────

@student_bp.route('/courses/browse')
@login_required
def browse_courses():
    try:
        if session.get('user_role') != 'student':
            flash('Only students can browse courses.', 'danger')
            return redirect(url_for('main.dashboard'))

        courses = Course.query.filter_by(is_active=True).all()

        enrolled_course_ids = [e.course_id for e in CourseEnrollment.query.filter_by(
            student_id=session['user_id'], status='active'
        ).all()]

        requested_course_ids = [r.course_id for r in CourseEnrollmentRequest.query.filter_by(
            student_id=session['user_id'], status='pending'
        ).all()]

        rejected_course_ids = [r.course_id for r in CourseEnrollmentRequest.query.filter_by(
            student_id=session['user_id'], status='rejected'
        ).all()]

        rejected_requests = CourseEnrollmentRequest.query.filter_by(
            student_id=session['user_id'], status='rejected'
        ).all()
        rejected_remarks = {r.course_id: r.remarks for r in rejected_requests if r.remarks}

        course_tokens = {}
        for course in courses:
            token = CourseEnrollmentToken.query.filter_by(
                course_id=course.id,
                is_active=True
            ).first()
            if token and token.is_valid():
                course_tokens[course.id] = token.token_code

        direct_enroll_course_ids = list(course_tokens.keys())

        return render_template('courses/browse_courses.html',
                             courses=courses,
                             enrolled_course_ids=enrolled_course_ids,
                             requested_course_ids=requested_course_ids,
                             rejected_course_ids=rejected_course_ids,
                             rejected_remarks=rejected_remarks,
                             course_tokens=course_tokens,
                             direct_enroll_course_ids=direct_enroll_course_ids)

    except Exception:
        current_app.logger.exception("Error browsing courses")
        flash('Error loading courses.', 'danger')
        return redirect(url_for('main.dashboard'))


@student_bp.route('/courses/request-enrollment/<int:course_id>', methods=['POST'])
@login_required
def request_course_enrollment(course_id):
    try:
        if session.get('user_role') != 'student':
            flash('Only students can enroll in courses.', 'danger')
            return redirect(url_for('main.dashboard'))

        course = Course.query.get_or_404(course_id)

        if not course.is_active:
            flash('This course is not currently accepting enrollments.', 'danger')
            return redirect(url_for('student.browse_courses'))

        existing_enrollment = CourseEnrollment.query.filter_by(
            student_id=session['user_id'],
            course_id=course_id
        ).first()

        if existing_enrollment:
            flash('You are already enrolled in this course!', 'warning')
            return redirect(url_for('student.browse_courses'))

        existing_request = CourseEnrollmentRequest.query.filter_by(
            student_id=session['user_id'],
            course_id=course_id,
            status='pending'
        ).first()

        if existing_request:
            flash('You already have a pending enrollment request for this course.', 'info')
            return redirect(url_for('student.browse_courses'))

        rejected_request = CourseEnrollmentRequest.query.filter_by(
            student_id=session['user_id'],
            course_id=course_id,
            status='rejected'
        ).first()

        if rejected_request:
            flash('Your previous enrollment request was rejected. Please contact the teacher.', 'warning')
            return redirect(url_for('student.browse_courses'))

        request_obj = CourseEnrollmentRequest(
            student_id=session['user_id'],
            course_id=course_id,
            status='pending',
            requested_at=utcnow()
        )

        db.session.add(request_obj)
        db.session.commit()

        flash(f'Enrollment request sent for {course.course_code} - {course.course_name}. Waiting for teacher approval.', 'success')

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error requesting enrollment")
        flash('Error submitting enrollment request. Please try again.', 'danger')

    return redirect(url_for('student.browse_courses'))