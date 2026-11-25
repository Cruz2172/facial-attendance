import secrets

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, session,
    jsonify, current_app, g, abort
)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from ..audit import audit
from ..extensions import db, limiter, user_or_ip
from ..decorators import login_required, teacher_or_admin_required
from ..face_core import (
    FaceBusy, FaceInputError, decode_data_url, detect_faces, encode_face,
    get_gate, select_primary_face,
)
from ..models import (
    User, Course, CourseEnrollment, CourseEnrollmentRequest, CourseEnrollmentToken,
    AttendanceSession, CourseAttendanceRecord, InviteCode
)
from ..utils import (
    query_students_by_user_ids, query_users_by_ids, get_active_session_for_today,
    invalidate_course_roster_cache, build_course_attendance_rows
)
from ..face_cache import get_roster_ids
from ..face_matching import identify_student_for_course
from ..timeutil import utcnow, now_local, today_local, to_local

courses_bp = Blueprint('courses', __name__)


def _claim_token_use(token):
    """Atomically consume one use of an enrollment token.

    The old ``token.used_count += 1`` let several students redeem the last
    use at the same moment. This only succeeds while uses remain."""
    claimed = CourseEnrollmentToken.query.filter(
        CourseEnrollmentToken.id == token.id,
        CourseEnrollmentToken.is_active.is_(True),
        or_(CourseEnrollmentToken.max_uses == -1,
            CourseEnrollmentToken.used_count < CourseEnrollmentToken.max_uses),
    ).update({CourseEnrollmentToken.used_count: CourseEnrollmentToken.used_count + 1},
             synchronize_session=False)
    if claimed:
        db.session.refresh(token)
    return bool(claimed)


def _enroll_teacher_invited_students(course):
    """When a teacher creates a new course, automatically enroll every
    student who registered via that teacher's single 'all courses'
    invite link (User.invite_code) -- so students don't need to use a
    new link every time the teacher adds a course."""
    teacher_invite_codes = [
        ic.code for ic in InviteCode.query.filter_by(
            created_by=course.teacher_id,
            is_teacher_invite=True
        ).all()
    ]

    if not teacher_invite_codes:
        return 0

    linked_students = User.query.filter(
        User.invite_code.in_(teacher_invite_codes),
        User.role == 'student'
    ).all()

    enrolled_count = 0
    for student_user in linked_students:
        existing = CourseEnrollment.query.filter_by(
            student_id=student_user.id,
            course_id=course.id
        ).first()
        if not existing:
            db.session.add(CourseEnrollment(
                student_id=student_user.id,
                course_id=course.id,
                enrolled_at=utcnow(),
                status='active'
            ))
            enrolled_count += 1

    if enrolled_count > 0:
        db.session.commit()

    return enrolled_count


@courses_bp.route('/courses')
@login_required
def list_courses():
    try:
        user = User.query.get(session['user_id'])

        if not user:
            flash('User not found. Please login again.', 'danger')
            return redirect(url_for('auth.login'))

        if user.role == 'teacher':
            courses = Course.query.filter_by(teacher_id=user.id).all()
            return render_template('courses/teacher_courses.html', courses=courses)

        elif user.role == 'student':
            return redirect(url_for('student.student_courses'))

        elif user.role == 'admin':
            courses = Course.query.all()
            return render_template('courses/admin_courses.html', courses=courses)

        else:
            flash('Invalid user role.', 'danger')
            return redirect(url_for('main.dashboard'))

    except Exception:
        current_app.logger.exception("Error in list_courses")
        flash('An error occurred while loading your courses. Please try again.', 'danger')
        return redirect(url_for('main.dashboard'))


@courses_bp.route('/courses/create', methods=['GET', 'POST'])
@login_required
@teacher_or_admin_required
def create_course():
    if request.method == 'POST':
        try:
            course_code = request.form.get('course_code', '').strip().upper()
            course_name = request.form.get('course_name', '').strip()
            description = request.form.get('description', '').strip()
            schedule = request.form.get('schedule', '').strip()

            if not course_code or not course_name:
                flash('Course code and name are required!', 'danger')
                return render_template('courses/create_course.html')

            existing = Course.query.filter_by(course_code=course_code).first()
            if existing:
                flash(f'Course code "{course_code}" already exists!', 'danger')
                return render_template('courses/create_course.html')

            course = Course(
                course_code=course_code,
                course_name=course_name,
                description=description,
                schedule=schedule,
                teacher_id=session['user_id'],
                created_at=utcnow(),
                is_active=True
            )

            db.session.add(course)
            db.session.commit()

            enrolled_count = _enroll_teacher_invited_students(course)

            if enrolled_count > 0:
                flash(
                    f'Course "{course_code} - {course_name}" created successfully! '
                    f'{enrolled_count} student(s) from your invite link were automatically enrolled.',
                    'success'
                )
            else:
                flash(f'Course "{course_code} - {course_name}" created successfully!', 'success')

            return redirect(url_for('courses.view_course', course_id=course.id))

        except Exception:
            db.session.rollback()
            current_app.logger.exception("Error creating course")
            flash('Error creating course. Please try again.', 'danger')
            return render_template('courses/create_course.html')

    return render_template('courses/create_course.html')


@courses_bp.route('/courses/<int:course_id>')
@login_required
def view_course(course_id):
    try:
        course = Course.query.get_or_404(course_id)
        user = User.query.get(session['user_id'])

        if user.role == 'student':
            return redirect(url_for('student.student_course_detail', course_id=course_id))

        if user.role == 'teacher' and course.teacher_id != user.id and session.get('user_role') != 'admin':
            flash('You can only view your own courses.', 'danger')
            return redirect(url_for('courses.list_courses'))

        enrollments = CourseEnrollment.query.filter_by(course_id=course_id).all()
        enrolled_student_ids = [e.student_id for e in enrollments]
        face_enrolled_ids = set()
        if enrolled_student_ids:
            face_enrolled_ids = {
                s.user_id for s in query_students_by_user_ids(enrolled_student_ids)
            }
        face_enrolled_id_list = [uid for uid in enrolled_student_ids if uid in face_enrolled_ids]
        users_by_id = {u.id: u for u in query_users_by_ids(face_enrolled_id_list)}
        enrolled_students = [
            users_by_id[e.student_id] for e in enrollments
            if e.student_id in face_enrolled_ids and e.student_id in users_by_id
        ]

        sessions = AttendanceSession.query.filter_by(course_id=course_id).order_by(
            AttendanceSession.session_date.desc()
        ).all()

        total_sessions = len(sessions)
        session_ids = [s.id for s in sessions]
        student_attendance = {}
        for student_user in enrolled_students:
            if not student_user:
                continue
            attended = 0
            if session_ids:
                attended = CourseAttendanceRecord.query.filter(
                    CourseAttendanceRecord.student_id == student_user.id,
                    CourseAttendanceRecord.session_id.in_(session_ids)
                ).count()
            percentage = round((attended / total_sessions) * 100, 2) if total_sessions > 0 else 0
            student_attendance[student_user.id] = {
                'attended': attended,
                'total': total_sessions,
                'percentage': percentage
            }

        pending_requests = []
        enrollment_tokens = []

        if user.role in ('teacher', 'admin'):
            pending_requests = CourseEnrollmentRequest.query.filter_by(
                course_id=course_id,
                status='pending'
            ).all()

            enrollment_tokens = CourseEnrollmentToken.query.filter_by(
                course_id=course_id,
                is_active=True
            ).all()

        return render_template('courses/view_courses.html',
                             course=course,
                             enrolled_students=enrolled_students,
                             student_attendance=student_attendance,
                             sessions=sessions,
                             pending_requests=pending_requests,
                             enrollment_tokens=enrollment_tokens)

    except Exception:
        current_app.logger.exception("Error viewing course")
        flash('Error loading course.', 'danger')
        return redirect(url_for('courses.list_courses'))


@courses_bp.route('/courses/<int:course_id>/start-session', methods=['GET', 'POST'])
@login_required
@teacher_or_admin_required
def start_attendance_session(course_id):
    course = Course.query.get_or_404(course_id)

    try:
        if course.teacher_id != session['user_id'] and session.get('user_role') != 'admin':
            flash('You can only start sessions for your own courses.', 'danger')
            return redirect(url_for('courses.view_course', course_id=course_id))

        active = get_active_session_for_today(course_id)
        if active:
            flash(f'There is already an active session for this course (started at {active.session_time}).', 'warning')
            return redirect(url_for('courses.view_session', session_id=active.id))

        if request.method == 'POST':
            topic = ((request.form.get('topic') or '').strip() or 'Regular Class')[:200]
            session_token = ''.join(secrets.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789') for _ in range(6))

            local_now = now_local()
            new_session = AttendanceSession(
                course_id=course_id,
                session_date=local_now.date(),
                session_time=local_now.time(),
                topic=topic,
                status='active',
                created_by=session['user_id'],
                created_at=utcnow(),
                session_token=session_token
            )

            db.session.add(new_session)
            audit('session.start', 'course', course_id, f'topic={topic}')
            db.session.commit()

            flash(f'Attendance session started for {course.course_code}! Token: {session_token}', 'success')
            return redirect(url_for('courses.view_session', session_id=new_session.id))

    except IntegrityError:
        # Another request started a session for this course a moment ago
        # (database allows only one active session per course).
        db.session.rollback()
        existing = AttendanceSession.query.filter_by(course_id=course_id, status='active').first()
        if existing:
            flash('A session for this course is already active.', 'warning')
            return redirect(url_for('courses.view_session', session_id=existing.id))
        flash('Could not start the session. Please try again.', 'danger')

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error starting session")
        flash('Error starting session. Please try again.', 'danger')

    return render_template('courses/start_session.html', course=course)


@courses_bp.route('/courses/session/<int:session_id>')
@login_required
def view_session(session_id):
    try:
        session_obj = AttendanceSession.query.get_or_404(session_id)
        course = Course.query.get(session_obj.course_id)
        if not course:
            flash('The course for this session no longer exists.', 'danger')
            return redirect(url_for('courses.list_courses'))

        user = User.query.get(session['user_id'])

        if user.role == 'student':
            enrollment = CourseEnrollment.query.filter_by(
                student_id=user.id,
                course_id=course.id,
                status='active'
            ).first()
            if not enrollment:
                flash('Access denied.', 'danger')
                return redirect(url_for('courses.list_courses'))
        elif user.role == 'teacher' and course.teacher_id != user.id and session.get('user_role') != 'admin':
            flash('Access denied.', 'danger')
            return redirect(url_for('courses.list_courses'))

        records = CourseAttendanceRecord.query.filter_by(session_id=session_id).all()
        attended_ids = {r.student_id for r in records}

        enrollments = CourseEnrollment.query.filter_by(
            course_id=course.id,
            status='active'
        ).all()
        enrolled_user_ids = [e.student_id for e in enrollments]

        face_enrolled_ids = set()
        if enrolled_user_ids:
            face_enrolled_ids = {
                s.user_id for s in query_students_by_user_ids(enrolled_user_ids)
            }

        face_registered_ids = [uid for uid in enrolled_user_ids if uid in face_enrolled_ids]
        users_by_id = {u.id: u for u in query_users_by_ids(face_registered_ids)}
        enrolled_users = [users_by_id[uid] for uid in face_registered_ids if uid in users_by_id]

        total_enrolled = len(enrolled_users)
        present_count = len(records)
        absent_count = max(total_enrolled - present_count, 0)

        # One query for every name on the page (the template used to run one
        # query per attendance record).
        record_users = {u.id: u for u in query_users_by_ids([r.student_id for r in records])}

        return render_template('courses/view_session.html',
                             attendance_session=session_obj,
                             course=course,
                             records=records,
                             record_users=record_users,
                             enrolled_users=enrolled_users,
                             attended_ids=attended_ids,
                             total_enrolled=total_enrolled,
                             present_count=present_count,
                             absent_count=absent_count)

    except Exception:
        current_app.logger.exception("Error viewing session")
        flash('Error loading session.', 'danger')
        return redirect(url_for('courses.list_courses'))


@courses_bp.route('/courses/session/<int:session_id>/end', methods=['POST'])
@login_required
@teacher_or_admin_required
def end_attendance_session(session_id):
    session_obj = AttendanceSession.query.get_or_404(session_id)
    course = Course.query.get_or_404(session_obj.course_id)

    try:
        if course.teacher_id != session['user_id'] and session.get('user_role') != 'admin':
            flash('Access denied.', 'danger')
            return redirect(url_for('courses.list_courses'))

        session_obj.status = 'closed'
        audit('session.end', 'session', session_id)
        db.session.commit()

        present_count = CourseAttendanceRecord.query.filter_by(session_id=session_id).count()
        total_enrolled = session_obj.get_total_enrolled()

        flash(f'Session ended. {present_count}/{total_enrolled} students marked present.', 'info')

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error ending session")
        flash('Error ending session. Please try again.', 'danger')

    return redirect(url_for('courses.view_course', course_id=course.id))


def _already_marked_response(student_name, marked_at):
    return jsonify({
        'success': True,
        'already_marked': True,
        'student_name': student_name,
        'message': f'{student_name} was already marked present for this session.',
        'time': to_local(marked_at).strftime('%H:%M:%S') if marked_at else '',
    })


@courses_bp.route('/courses/session/<int:session_id>/mark', methods=['POST'])
@limiter.limit(lambda: current_app.config['SCAN_RATE_LIMIT'], key_func=user_or_ip)
@login_required
@teacher_or_admin_required
def mark_course_attendance(session_id):
    """Runs on the teacher/admin's device at the front of the room. Each
    camera scan is matched against ALL registered faces (strict threshold +
    ambiguity margin), then checked against this course's roster."""
    cfg = current_app.config
    try:
        data = request.get_json(silent=True)
        if not data or not isinstance(data.get('image'), str):
            return jsonify({'success': False, 'message': 'No image data received'})

        session_obj = db.session.get(AttendanceSession, session_id)
        if not session_obj:
            return jsonify({'success': False, 'message': 'Invalid session'})

        course = db.session.get(Course, session_obj.course_id)
        if not course:
            return jsonify({'success': False, 'message': 'Course not found'})

        user = g.current_user
        if user.role != 'admin' and course.teacher_id != user.id:
            return jsonify({'success': False, 'message': 'You do not have permission to run attendance for this course'})

        # Also closes a session left open since a previous day.
        active = get_active_session_for_today(course.id)
        if session_obj.status != 'active' or not active or active.id != session_obj.id:
            return jsonify({'success': False, 'closed': True, 'message': 'This attendance session is closed'})

        # Cheap checks BEFORE the expensive face work.
        if not get_roster_ids(course.id):
            has_any_enrollment = CourseEnrollment.query.filter_by(
                course_id=course.id, status='active'
            ).first() is not None
            if not has_any_enrollment:
                return jsonify({'success': False, 'message': 'No students are enrolled in this course.'})
            return jsonify({'success': False, 'message': 'No enrolled students in this course have completed face enrollment yet.'})

        try:
            _, img_rgb = decode_data_url(
                data['image'],
                max_pixels=cfg['FACE_MAX_IMAGE_PIXELS'],
                max_dim=cfg['FACE_DETECT_MAX_DIM'],
            )
        except FaceInputError as exc:
            return jsonify({'success': False, 'message': str(exc)})

        try:
            with get_gate(cfg['FACE_MAX_CONCURRENT']).slot(timeout=cfg['FACE_QUEUE_TIMEOUT']):
                boxes = detect_faces(img_rgb)
                box, face_status = select_primary_face(
                    boxes, cfg['FACE_MIN_SIZE_PX'], cfg['FACE_PRIMARY_RATIO'])
                encoding = encode_face(img_rgb, box, cfg['FACE_SCAN_JITTERS']) if box is not None else None
        except FaceBusy:
            return jsonify({'success': False, 'busy': True,
                            'message': 'Server is busy - retrying...'}), 503
        except FaceInputError as exc:
            return jsonify({'success': False, 'message': str(exc)})

        if face_status == 'no_face':
            return jsonify({'success': False, 'idle': True, 'message': 'No face in frame.'})
        if face_status == 'too_small':
            return jsonify({'success': False, 'idle': True,
                            'message': 'Face is too far away - please step closer to the camera.'})
        if face_status == 'multiple':
            return jsonify({'success': False, 'message': 'Multiple faces in frame - one student at a time, please.'})

        matched, status, best_distance = identify_student_for_course(encoding, course.id)

        if status == 'unknown':
            return jsonify({'success': False, 'message': 'Face not recognized as a registered student.'})

        if status == 'not_enrolled':
            return jsonify({'success': False, 'message': 'This person is not enrolled in this course.'})

        if status == 'ambiguous':
            return jsonify({
                'success': False,
                'ambiguous': True,
                'message': (
                    'This face is too similar to more than one registered '
                    'student to mark attendance automatically. Please '
                    'move closer with better lighting, or use "Mark present" '
                    'next to the student on the session page.'
                )
            })

        confidence = max(0, min(100, (1 - best_distance) * 100))

        existing = CourseAttendanceRecord.query.filter_by(
            session_id=session_id, student_id=matched.user_id
        ).first()
        if existing:
            return _already_marked_response(matched.fullname, existing.marked_at)

        record = CourseAttendanceRecord(
            session_id=session_id,
            student_id=matched.user_id,
            face_confidence=confidence,
            status='present',
            marked_at=utcnow()
        )
        db.session.add(record)
        try:
            db.session.commit()
        except IntegrityError:
            # Two scans of the same student raced; the other one won.
            db.session.rollback()
            existing = CourseAttendanceRecord.query.filter_by(
                session_id=session_id, student_id=matched.user_id
            ).first()
            return _already_marked_response(matched.fullname, existing.marked_at if existing else None)

        return jsonify({
            'success': True,
            'already_marked': False,
            'student_name': matched.fullname,
            'student_id': matched.user_id,
            'confidence': round(confidence, 2),
            'time': now_local().strftime('%H:%M:%S'),
            'message': f'✅ {matched.fullname} marked present.'
        })

    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error marking course attendance")
        return jsonify({'success': False, 'message': 'Error marking attendance. Please try again.'})


def _manage_session_or_abort(session_id):
    """Load a session + its course and make sure the current user may manage it."""
    session_obj = db.session.get(AttendanceSession, session_id)
    if not session_obj:
        abort(404)
    course = db.session.get(Course, session_obj.course_id)
    if not course:
        abort(404)
    if g.current_user.role != 'admin' and course.teacher_id != g.current_user.id:
        abort(403)
    return session_obj, course


@courses_bp.route('/courses/session/<int:session_id>/manual-mark', methods=['POST'])
@login_required
@teacher_or_admin_required
def manual_mark_attendance(session_id):
    """Teacher override for students the camera cannot recognise (poor light,
    glasses, look-alikes, no face enrolled yet...). Always recorded with the
    teacher's name and reason, and written to the audit log."""
    session_obj, course = _manage_session_or_abort(session_id)

    try:
        student_user_id = request.form.get('student_user_id', type=int)
        reason = (request.form.get('reason') or '').strip()[:100]

        enrollment = None
        if student_user_id:
            enrollment = CourseEnrollment.query.filter_by(
                student_id=student_user_id, course_id=course.id, status='active').first()
        if not enrollment:
            flash('That student is not enrolled in this course.', 'danger')
            return redirect(url_for('courses.view_session', session_id=session_id))

        student_user = db.session.get(User, student_user_id)
        name = student_user.fullname if student_user else f'user {student_user_id}'

        existing = CourseAttendanceRecord.query.filter_by(
            session_id=session_id, student_id=student_user_id).first()
        if existing:
            flash(f'{name} is already marked present.', 'info')
            return redirect(url_for('courses.view_session', session_id=session_id))

        remarks = f"Manual - {g.current_user.fullname}" + (f": {reason}" if reason else '')
        db.session.add(CourseAttendanceRecord(
            session_id=session_id,
            student_id=student_user_id,
            face_confidence=None,
            status='present',
            remarks=remarks[:200],
            marked_at=utcnow(),
        ))
        audit('attendance.manual_mark', 'session', session_id,
              f'student_user_id={student_user_id} reason={reason or "-"}')
        db.session.commit()
        flash(f'{name} marked present manually.', 'success')

    except IntegrityError:
        db.session.rollback()
        flash('That student was just marked present.', 'info')
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Error marking attendance manually')
        flash('Could not mark attendance. Please try again.', 'danger')

    return redirect(url_for('courses.view_session', session_id=session_id))


@courses_bp.route('/courses/session/<int:session_id>/manual-unmark', methods=['POST'])
@login_required
@teacher_or_admin_required
def manual_unmark_attendance(session_id):
    """Remove a mistaken attendance record (e.g. wrong person was scanned)."""
    session_obj, course = _manage_session_or_abort(session_id)

    try:
        student_user_id = request.form.get('student_user_id', type=int)
        record = None
        if student_user_id:
            record = CourseAttendanceRecord.query.filter_by(
                session_id=session_id, student_id=student_user_id).first()
        if not record:
            flash('No attendance record found for that student.', 'warning')
            return redirect(url_for('courses.view_session', session_id=session_id))

        audit('attendance.unmark', 'session', session_id,
              f'student_user_id={student_user_id} previous_remarks={record.remarks or "-"} '
              f'confidence={record.face_confidence}')
        db.session.delete(record)
        db.session.commit()
        flash('Attendance record removed.', 'success')

    except Exception:
        db.session.rollback()
        current_app.logger.exception('Error removing attendance record')
        flash('Could not remove the record. Please try again.', 'danger')

    return redirect(url_for('courses.view_session', session_id=session_id))


@courses_bp.route('/courses/enroll/<token_code>')
@login_required
def enroll_via_link(token_code):
    try:
        if session.get('user_role') != 'student':
            flash('Only students can enroll in courses.', 'danger')
            return redirect(url_for('main.dashboard'))

        token = CourseEnrollmentToken.query.filter_by(
            token_code=token_code,
            is_active=True
        ).first()

        if not token:
            flash('Invalid or expired enrollment link.', 'danger')
            return redirect(url_for('student.browse_courses'))

        if not token.is_valid():
            if token.expires_at and utcnow() > token.expires_at:
                flash('This enrollment link has expired.', 'danger')
            elif token.max_uses != -1 and token.used_count >= token.max_uses:
                flash('This enrollment link has reached its maximum uses.', 'danger')
            else:
                flash('This enrollment link is no longer valid.', 'danger')
            return redirect(url_for('student.browse_courses'))

        course = Course.query.get(token.course_id)
        if not course:
            flash('Course not found.', 'danger')
            return redirect(url_for('student.browse_courses'))

        existing = CourseEnrollment.query.filter_by(
            student_id=session['user_id'],
            course_id=course.id
        ).first()

        if existing:
            flash(f'You are already enrolled in {course.course_code} - {course.course_name}!', 'info')
            return redirect(url_for('courses.view_course', course_id=course.id))

        pending = CourseEnrollmentRequest.query.filter_by(
            student_id=session['user_id'],
            course_id=course.id,
            status='pending'
        ).first()

        if pending:
            flash(f'You already have a pending enrollment request for {course.course_code}.', 'info')
            return redirect(url_for('student.browse_courses'))

        enrollment = CourseEnrollment(
            student_id=session['user_id'],
            course_id=course.id,
            enrolled_at=utcnow(),
            status='active'
        )

        if not _claim_token_use(token):
            flash('This enrollment link has reached its maximum uses.', 'danger')
            return redirect(url_for('student.browse_courses'))

        db.session.add(enrollment)
        db.session.commit()
        invalidate_course_roster_cache(course.id)

        flash(f'Successfully enrolled in {course.course_code} - {course.course_name}!', 'success')
        return redirect(url_for('courses.view_course', course_id=course.id))

    except IntegrityError:
        db.session.rollback()
        flash('You are already enrolled in this course.', 'info')
        return redirect(url_for('student.browse_courses'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error enrolling via link")
        flash('Error enrolling in course. Please try again.', 'danger')
        return redirect(url_for('student.browse_courses'))


@courses_bp.route('/courses/enroll', methods=['GET', 'POST'])
@login_required
def enroll_with_token():
    try:
        if session.get('user_role') != 'student':
            flash('Only students can enroll in courses.', 'danger')
            return redirect(url_for('main.dashboard'))

        if request.method == 'POST':
            token_code = request.form.get('token_code', '').strip().upper()

            if not token_code or len(token_code) != 8:
                flash('Please enter a valid 8-character token.', 'danger')
                return render_template('courses/enroll_course.html', course=None)

            token = CourseEnrollmentToken.query.filter_by(
                token_code=token_code,
                is_active=True
            ).first()

            if not token:
                flash('Invalid enrollment token. Please check with your teacher.', 'danger')
                return render_template('courses/enroll_course.html', course=None)

            if not token.is_valid():
                if token.expires_at and utcnow() > token.expires_at:
                    flash('This enrollment token has expired.', 'danger')
                elif token.max_uses != -1 and token.used_count >= token.max_uses:
                    flash('This enrollment token has reached its maximum uses.', 'danger')
                else:
                    flash('This enrollment token is no longer valid.', 'danger')
                return render_template('courses/enroll_course.html', course=None)

            course = Course.query.get(token.course_id)
            if not course:
                flash('Course not found.', 'danger')
                return render_template('courses/enroll_course.html', course=None)

            existing = CourseEnrollment.query.filter_by(
                student_id=session['user_id'],
                course_id=course.id
            ).first()

            if existing:
                flash(f'You are already enrolled in {course.course_code} - {course.course_name}!', 'info')
                return redirect(url_for('courses.view_course', course_id=course.id))

            pending = CourseEnrollmentRequest.query.filter_by(
                student_id=session['user_id'],
                course_id=course.id,
                status='pending'
            ).first()

            if pending:
                flash(f'You already have a pending enrollment request for {course.course_code}.', 'info')
                return redirect(url_for('student.browse_courses'))

            enrollment = CourseEnrollment(
                student_id=session['user_id'],
                course_id=course.id,
                enrolled_at=utcnow(),
                status='active'
            )

            if not _claim_token_use(token):
                flash('This enrollment token has reached its maximum uses.', 'danger')
                return render_template('courses/enroll_course.html', course=None)
            if token.max_uses != -1 and token.used_count >= token.max_uses:
                token.is_active = False

            db.session.add(enrollment)
            db.session.commit()
            invalidate_course_roster_cache(course.id)

            flash(f'Successfully enrolled in {course.course_code} - {course.course_name}!', 'success')
            return redirect(url_for('courses.view_course', course_id=course.id))

        return render_template('courses/enroll_course.html', course=None)

    except IntegrityError:
        db.session.rollback()
        flash('You are already enrolled in this course.', 'info')
        return redirect(url_for('student.browse_courses'))
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Error enrolling with token")
        flash('Error enrolling in course. Please try again.', 'danger')
        return redirect(url_for('student.browse_courses'))


@courses_bp.route('/attendance-report')
@login_required
@teacher_or_admin_required
def attendance_report():
    """Every enrolled, face-registered student's attendance rate,
    grouped by course. Admins see every course; teachers see only their
    own. Optional ?course=<id> filters to a single course."""
    try:
        user = User.query.get(session['user_id'])

        if user.role == 'admin':
            all_courses = Course.query.order_by(Course.course_code).all()
        else:
            all_courses = Course.query.filter_by(teacher_id=user.id).order_by(Course.course_code).all()

        selected_course_id = request.args.get('course', type=int)
        courses_to_show = all_courses
        if selected_course_id:
            courses_to_show = [c for c in all_courses if c.id == selected_course_id]

        report = []
        for course in courses_to_show:
            student_rows, total_sessions = build_course_attendance_rows(course)
            report.append({
                'course': course,
                'total_sessions': total_sessions,
                'students': student_rows
            })

        return render_template('attendance_report.html',
                             report=report,
                             all_courses=all_courses,
                             selected_course_id=selected_course_id)

    except Exception:
        current_app.logger.exception("Error loading attendance report")
        flash('Error loading attendance report.', 'danger')
        return redirect(url_for('main.dashboard'))


@courses_bp.route('/attendance-report/print/<int:course_id>')
@login_required
@teacher_or_admin_required
def print_attendance_report(course_id):
    """Clean, printable per-course attendance report."""
    try:
        course = Course.query.get_or_404(course_id)
        user = User.query.get(session['user_id'])

        if user.role != 'admin' and course.teacher_id != user.id:
            flash('You can only print attendance reports for your own courses.', 'danger')
            return redirect(url_for('courses.attendance_report'))

        student_rows, total_sessions = build_course_attendance_rows(course)

        return render_template('attendance_report_print.html',
                             course=course,
                             student_rows=student_rows,
                             total_sessions=total_sessions,
                             generated_at=now_local())

    except Exception:
        current_app.logger.exception("Error loading printable attendance report")
        flash('Error loading printable attendance report.', 'danger')
        return redirect(url_for('courses.attendance_report'))