from flask import current_app

from .extensions import db
from .face_core import (
    FaceInputError, decode_data_url, find_duplicate_person,
)
from .face_cache import (          # re-exported: other modules import these from utils
    get_known_faces, get_course_roster, get_roster_ids, get_state,
    invalidate_face_cache, invalidate_course_roster_cache,
)
from .models import (
    Student, User, CourseEnrollment,
    CourseAttendanceRecord, AttendanceSession
)
from .timeutil import today_local

# A Student row with no usable encoding (left over from the old profile bug)
# is "incomplete", not "face-enrolled".
_HAS_FACE = (Student.face_encoding.isnot(None)) & (Student.face_encoding != '') & (Student.face_encoding != '[]')
HAS_FACE = _HAS_FACE


# ─────────────────────────────────────────────
# Batched IN() helpers -- SQLite has a compiled-in limit on the number of
# placeholders a single query can have (SQLITE_MAX_VARIABLE_NUMBER,
# historically 999). Every .in_(some_list) crashes the instant that list
# crosses that limit, which a single large course hits in real use. These
# helpers run the filter in safe-sized batches and merge results.
# ─────────────────────────────────────────────

def _chunked(seq):
    size = current_app.config['IN_CLAUSE_BATCH_SIZE']
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def query_students_by_user_ids(user_ids):
    if not user_ids:
        return []
    results = []
    for batch in _chunked(user_ids):
        results.extend(Student.query.filter(Student.user_id.in_(batch), _HAS_FACE).all())
    return results


def count_students_by_user_ids(user_ids):
    if not user_ids:
        return 0
    total = 0
    for batch in _chunked(user_ids):
        total += Student.query.filter(Student.user_id.in_(batch), _HAS_FACE).count()
    return total


def query_users_by_ids(user_ids):
    if not user_ids:
        return []
    results = []
    for batch in _chunked(user_ids):
        results.extend(User.query.filter(User.id.in_(batch)).all())
    return results


def count_attendance_records_by_student_and_sessions(student_id, session_ids):
    if not session_ids:
        return 0
    total = 0
    for batch in _chunked(session_ids):
        total += CourseAttendanceRecord.query.filter(
            CourseAttendanceRecord.student_id == student_id,
            CourseAttendanceRecord.session_id.in_(batch)
        ).count()
    return total


# ─────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────

def get_active_session_for_today(course_id):
    """Returns the course's active session if dated today (app timezone),
    else None.

    A session left open past its own day is auto-closed so the camera can't
    keep writing into yesterday's session. Uses the app timezone (not the
    server's) so a lecture does not "roll over" at the wrong hour."""
    active = AttendanceSession.query.filter_by(course_id=course_id, status='active').first()
    if not active:
        return None

    if active.session_date == today_local():
        return active

    active.status = 'closed'
    db.session.commit()
    return None


# ─────────────────────────────────────────────
# Duplicate enrollment check + image decoding
# ─────────────────────────────────────────────

def check_face_duplicate(face_encoding, threshold=None, margin=None):
    """Return the FaceRecord of an already-enrolled person this face
    unambiguously matches, else None (same rules as before: closest match
    must beat the threshold AND the runner-up by the margin)."""
    if threshold is None:
        threshold = current_app.config['DUPLICATE_THRESHOLD']
    if margin is None:
        margin = current_app.config['MATCH_MARGIN']
    try:
        state = get_state()
        idx = find_duplicate_person(state.matrix, state.starts, face_encoding, threshold, margin)
        return state.records[idx] if idx is not None else None
    except Exception:
        current_app.logger.exception('Error checking duplicate face')
        return None


def decode_image_from_base64(data_url, max_dim=None):
    """Decode a base64 data-URL into (img_bgr, img_rgb).

    Returns (None, None) on any problem (kept for backwards compatibility);
    use face_core.decode_data_url directly to get a user-safe error message."""
    try:
        return decode_data_url(
            data_url,
            max_pixels=current_app.config['FACE_MAX_IMAGE_PIXELS'],
            max_dim=max_dim,
        )
    except FaceInputError as e:
        current_app.logger.info('Image rejected: %s', e)
        return None, None
    except Exception:
        current_app.logger.exception('Unexpected error decoding image')
        return None, None


def build_course_attendance_rows(course):
    """Shared by /attendance-report and the printable per-course view so
    the numbers can never drift between the two pages."""
    from sqlalchemy import func

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

    sessions = AttendanceSession.query.filter_by(course_id=course.id).all()
    session_ids = [s.id for s in sessions]
    total_sessions = len(sessions)

    face_registered_ids = [uid for uid in enrolled_user_ids if uid in face_enrolled_ids]
    student_profiles_by_uid = {s.user_id: s for s in query_students_by_user_ids(face_registered_ids)}

    attended_counts = {}
    if session_ids:
        for batch in _chunked(session_ids):
            rows = db.session.query(
                CourseAttendanceRecord.student_id,
                func.count(CourseAttendanceRecord.id)
            ).filter(
                CourseAttendanceRecord.session_id.in_(batch)
            ).group_by(CourseAttendanceRecord.student_id).all()
            for uid, cnt in rows:
                attended_counts[uid] = attended_counts.get(uid, 0) + cnt

    student_rows = []
    for uid in face_registered_ids:
        student_profile = student_profiles_by_uid.get(uid)
        if not student_profile:
            continue

        attended = attended_counts.get(uid, 0)
        percentage = round((attended / total_sessions) * 100, 2) if total_sessions > 0 else 0

        student_rows.append({
            'student_id': student_profile.student_id,
            'fullname': student_profile.fullname,
            'email': student_profile.email,
            'attended': attended,
            'total': total_sessions,
            'percentage': percentage
        })

    student_rows.sort(key=lambda r: r['percentage'])
    return student_rows, total_sessions