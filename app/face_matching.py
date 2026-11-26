"""Strict 1:N face identification for attendance scans.

A scan is matched against EVERY registered face first (not just the course
roster). It is only accepted if:
  1. the closest face is within CONFIDENCE_THRESHOLD,
  2. the runner-up is at least MATCH_MARGIN further away (not ambiguous),
  3. the winner is actually enrolled in this course.

The heavy lifting (vectorised distances, decision rules) is in face_core.py;
the face data comes from face_cache.py (shared safely between workers).
"""
from flask import current_app

from .face_cache import get_state, get_roster_ids
from .face_core import identify_people


def identify_face(face_encoding, known_encodings, known_students,
                  threshold=None, margin=None):
    """Compatibility wrapper. Return (student, best_distance, status).

    status: 'ok' | 'unknown' (too far) | 'ambiguous' (runner-up too close)
    """
    import numpy as np
    if threshold is None:
        threshold = current_app.config['CONFIDENCE_THRESHOLD']
    if margin is None:
        margin = current_app.config['MATCH_MARGIN']
    if known_encodings is None or len(known_encodings) == 0:
        return None, None, 'unknown'
    matrix = np.asarray(known_encodings)
    idx, dist, status = identify_people(
        matrix, np.arange(len(matrix)), face_encoding, threshold, margin)
    return (known_students[idx] if idx is not None else None), dist, status


def identify_student_for_course(face_encoding, course_id):
    """Return (record, status, distance).

    ``record`` is a plain FaceRecord (id, user_id, student_id, fullname,
    email, course) - never an ORM object, so it is safe to use after commits.

    status: 'ok' | 'unknown' | 'ambiguous' | 'not_enrolled'
    """
    state = get_state()
    # Each student has several templates (different poses); distance is taken to
    # the closest template of each PERSON, and the ambiguity margin is measured
    # against the next different person - never against the same person's other photos.
    idx, dist, status = identify_people(
        state.matrix, state.starts, face_encoding,
        current_app.config['CONFIDENCE_THRESHOLD'],
        current_app.config['MATCH_MARGIN'],
    )

    record = state.records[idx] if idx is not None else None
    current_app.logger.info(
        'Scan course=%s best_distance=%s status=%s matched=%s',
        course_id, None if dist is None else round(dist, 4), status,
        getattr(record, 'id', None),
    )

    if status != 'ok':
        return None, status, dist

    if record.id not in get_roster_ids(course_id):
        return None, 'not_enrolled', dist

    return record, 'ok', dist