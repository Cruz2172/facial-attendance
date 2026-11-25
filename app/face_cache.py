"""In-memory face index shared safely between web workers.

Two problems with the old per-process caches are solved here:

1. Cross-worker staleness. Each gunicorn worker has its own memory, so
   invalidating "the cache" only cleared the worker that served the request.
   Now every change bumps a counter row in the ``app_state`` table and each
   worker compares it (at most once per second) with the version it loaded.

2. Detached ORM objects. Caching SQLAlchemy ``Student`` instances across
   requests is unsafe: the next ``db.session.commit()`` expires them, and once
   their session is closed any attribute access raises DetachedInstanceError.
   The cache now holds plain tuples + a numpy matrix only.
"""
import threading
import time
from collections import namedtuple

import numpy as np
from flask import current_app
from sqlalchemy import update

from .extensions import db
from .face_core import ENCODING_LEN, parse_encoding
from .models import AppState, CourseEnrollment, Student

FaceRecord = namedtuple('FaceRecord', 'id user_id student_id fullname email course')

FACES_KEY = 'faces_version'
ROSTER_KEY = 'roster_version'

_RECHECK_SECONDS = 1.0

_lock = threading.RLock()
_local_bump = {FACES_KEY: 0, ROSTER_KEY: 0}     # makes THIS worker refresh even if the DB write failed


class _FaceState:
    def __init__(self):
        self.faces_version = None
        self.roster_version = None
        self.matrix = np.empty((0, ENCODING_LEN))
        self.records = ()
        self.user_to_index = {}
        self.rosters = {}                  # course_id -> frozenset(student pk)
        self.checked_at = 0.0


_state = _FaceState()


# ---------------------------------------------------------------------------
# Version counters
# ---------------------------------------------------------------------------
def _read_versions():
    rows = db.session.query(AppState.key, AppState.value).filter(
        AppState.key.in_((FACES_KEY, ROSTER_KEY))
    ).all()
    values = {k: v for k, v in rows}
    return (
        (values.get(FACES_KEY, 0), _local_bump[FACES_KEY]),
        (values.get(ROSTER_KEY, 0), _local_bump[ROSTER_KEY]),
    )


def _bump(key):
    """Increment a shared counter. Never raises: a failed bump still refreshes
    this worker through the local counter."""
    _local_bump[key] += 1
    _state.checked_at = 0.0          # this worker re-checks immediately, not after the 1 s throttle
    try:
        table = AppState.__table__
        result = db.session.execute(
            update(table).where(table.c.key == key).values(value=table.c.value + 1)
        )
        if result.rowcount == 0:
            db.session.add(AppState(key=key, value=1))
        db.session.commit()
    except Exception:
        db.session.rollback()
        try:   # row may have been created concurrently by another worker
            table = AppState.__table__
            db.session.execute(update(table).where(table.c.key == key).values(value=table.c.value + 1))
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Could not bump %s in app_state', key)


def invalidate_face_cache():
    """Face data changed (enroll / delete). Also invalidates rosters."""
    _bump(FACES_KEY)
    _bump(ROSTER_KEY)


def invalidate_course_roster_cache(course_id=None):
    """Course membership changed. (course_id kept for API compatibility.)"""
    _bump(ROSTER_KEY)


def ensure_state_rows():
    """Create the counter rows (called from init-db). Safe to run repeatedly."""
    for key in (FACES_KEY, ROSTER_KEY):
        if db.session.get(AppState, key) is None:
            db.session.add(AppState(key=key, value=0))
    db.session.commit()


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def _load_faces():
    rows = db.session.query(
        Student.id, Student.user_id, Student.student_id, Student.fullname,
        Student.email, Student.course, Student.face_encoding,
    ).all()

    vectors, records = [], []
    for row in rows:
        enc = parse_encoding(row.face_encoding)
        if enc is None:
            continue
        vectors.append(enc)
        records.append(FaceRecord(row.id, row.user_id, row.student_id,
                                  row.fullname, row.email, row.course))

    matrix = np.vstack(vectors) if vectors else np.empty((0, ENCODING_LEN))
    return matrix, tuple(records)


def get_state():
    """Return an up-to-date _FaceState, reloading from the DB if needed."""
    global _state
    now = time.monotonic()
    st = _state

    if st.faces_version is not None and (now - st.checked_at) < _RECHECK_SECONDS:
        return st

    try:
        faces_v, roster_v = _read_versions()
    except Exception:
        db.session.rollback()
        current_app.logger.exception('Could not read face cache versions')
        if st.faces_version is not None:
            return st                       # keep serving the last good data
        faces_v = roster_v = (0, _local_bump[FACES_KEY])   # first load, DB hiccup: try anyway

    with _lock:
        st = _state
        if st.faces_version != faces_v:
            try:
                matrix, records = _load_faces()
            except Exception:
                db.session.rollback()
                current_app.logger.exception('Could not load known faces')
                if st.faces_version is not None:
                    return st
                raise
            new = _FaceState()
            new.matrix, new.records = matrix, records
            new.user_to_index = {r.user_id: i for i, r in enumerate(records)}
            new.faces_version = faces_v
            new.roster_version = roster_v
            _state = st = new
            current_app.logger.info('Face cache loaded: %d faces', len(records))
        elif st.roster_version != roster_v:
            st.rosters = {}
            st.roster_version = roster_v
        st.checked_at = now
        return st


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------
def get_known_faces():
    """(list of encodings, list of FaceRecord) for every enrolled face."""
    st = get_state()
    return list(st.matrix), list(st.records)


def get_roster_ids(course_id):
    """frozenset of Student primary keys enrolled (active) in the course."""
    st = get_state()
    cached = st.rosters.get(course_id)
    if cached is not None:
        return cached

    user_ids = [e.student_id for e in CourseEnrollment.query.filter_by(
        course_id=course_id, status='active').with_entities(CourseEnrollment.student_id).all()]
    ids = frozenset(
        st.records[st.user_to_index[uid]].id for uid in user_ids if uid in st.user_to_index
    )
    st.rosters[course_id] = ids
    return ids


def get_course_roster(course_id):
    """(list of encodings, list of FaceRecord) for the course roster."""
    st = get_state()
    roster = get_roster_ids(course_id)
    idx = [i for i, r in enumerate(st.records) if r.id in roster]
    return [st.matrix[i] for i in idx], [st.records[i] for i in idx]


def reset_for_tests():
    global _state
    _state = _FaceState()
    _local_bump[FACES_KEY] = _local_bump[ROSTER_KEY] = 0
