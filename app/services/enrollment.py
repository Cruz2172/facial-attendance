import json
import os
import re
import secrets

import cv2
import numpy as np
from flask import current_app
from sqlalchemy import func

from ..face_core import (
    FaceBusy, FaceInputError, check_capture_consistency, decode_data_url,
    detect_faces, encode_face, get_gate, parse_encodings, select_primary_face,
)
from ..models import Student
from ..timeutil import utcnow
from ..utils import check_face_duplicate

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


class EnrollmentError(Exception):
    """Raised for any expected, user-facing enrollment failure (bad
    input, no face detected, duplicate, etc). The message is safe to
    flash directly - it's never a raw exception string."""
    pass


def parse_face_images(form):
    """Photos posted by the enrollment page: a JSON array in ``face_images``
    (current), or a single ``face_data`` value (older cached pages)."""
    raw = (form.get('face_images') or '').strip()
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            raise EnrollmentError('The captured photos could not be read. Please retake them.')
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise EnrollmentError('The captured photos could not be read. Please retake them.')
        return data
    single = (form.get('face_data') or '').strip()
    return [single] if single else []


def validate_enrollment_form(student_id, fullname, email, course, level, face_images, consent=False):
    cfg = current_app.config

    if not all([student_id, fullname, email, course]):
        raise EnrollmentError('All fields are required!')

    if not level:
        raise EnrollmentError('Please select your level!')

    if not EMAIL_RE.match(email):
        raise EnrollmentError('Please enter a valid email address!')

    if len(student_id) < 3:
        raise EnrollmentError('Student ID must be at least 3 characters!')

    if len(student_id) > 50 or len(fullname) > 120 or len(email) > 120 \
            or len(course) > 120 or len(level) > 20:
        raise EnrollmentError('One of the fields is too long. Please shorten it and try again.')

    if not consent:
        raise EnrollmentError(
            'You must agree to the processing of your facial data to enroll. '
            'Please tick the consent box.')

    minimum, maximum = cfg['ENROLL_MIN_IMAGES'], cfg['ENROLL_MAX_IMAGES']
    if len(face_images) < minimum:
        raise EnrollmentError(
            f'Please capture at least {minimum} photos of your face with slightly '
            f'different head positions (you sent {len(face_images)}).')
    if len(face_images) > maximum:
        raise EnrollmentError(f'Too many photos. Please send no more than {maximum}.')

    for image in face_images:
        if not image.startswith('data:image/'):
            raise EnrollmentError('Invalid image data. Please capture new photos.')
        if len(image) > 2_500_000:
            raise EnrollmentError('One of the photos is too large. Please retake it.')


def _has_face(student):
    return parse_encodings(student.face_encoding) is not None


def check_not_already_enrolled(user_id, student_id, email):
    """Raise if this account/ID/email is already enrolled.

    Returns an existing *incomplete* Student row for this account (created by
    the old profile page without a face) so the caller can finish it instead of
    failing with "you already have an enrollment" forever.
    """
    incomplete = None
    existing_student = Student.query.filter_by(user_id=user_id).first()
    if existing_student:
        if _has_face(existing_student):
            raise EnrollmentError(
                f'⚠️ You already have a face enrollment as {existing_student.fullname} '
                f'(ID: {existing_student.student_id})!'
            )
        incomplete = existing_student

    # Messages deliberately do not reveal who owns an ID/e-mail.
    id_owner = Student.query.filter_by(student_id=student_id).first()
    if id_owner and (incomplete is None or id_owner.id != incomplete.id):
        raise EnrollmentError(
            f'❌ Student ID "{student_id}" is already registered. '
            f'If it is yours, please contact the administrator.'
        )

    email_owner = Student.query.filter(func.lower(Student.email) == email.lower()).first()
    if email_owner and (incomplete is None or email_owner.id != incomplete.id):
        raise EnrollmentError(
            '❌ That email is already registered to another student. '
            'Please use your own email or contact the administrator.'
        )

    return incomplete


def decode_and_normalize_image(face_data):
    cfg = current_app.config
    try:
        img_bgr, img_rgb = decode_data_url(
            face_data,
            max_pixels=cfg['FACE_MAX_IMAGE_PIXELS'],
            max_dim=cfg['FACE_ENROLL_MAX_DIM'],
        )
    except FaceInputError as exc:
        raise EnrollmentError(str(exc))
    except Exception:
        current_app.logger.exception('Image decoding failed')
        raise EnrollmentError('Error processing image. Please retake the photo.')
    return img_bgr, img_rgb


def _single_face_encoding(img_rgb, jitters):
    """Encoding of the ONE face in the photo. Caller must hold a face-gate slot."""
    cfg = current_app.config
    boxes = detect_faces(img_rgb)
    box, status = select_primary_face(
        boxes, min_size=cfg['FACE_MIN_SIZE_PX'], ratio=float('inf'))
    if box is None:
        if status == 'no_face':
            raise EnrollmentError('❌ No face detected. Please retake the photo with your face clearly visible.')
        if status == 'too_small':
            raise EnrollmentError('❌ Your face is too small in the photo. Please move closer and retake it.')
        raise EnrollmentError('❌ Multiple faces detected. Please ensure only one person is in frame.')
    return encode_face(img_rgb, box, jitters)


def detect_single_face(img_rgb):
    """Return the 128-d encoding of the ONE face in a single photo."""
    cfg = current_app.config
    try:
        with get_gate(cfg['FACE_MAX_CONCURRENT']).slot(timeout=cfg['FACE_QUEUE_TIMEOUT'] * 2):
            return _single_face_encoding(img_rgb, cfg['FACE_ENROLL_JITTERS'])
    except FaceBusy:
        raise EnrollmentError('The server is busy right now. Please try again in a moment.')
    except FaceInputError as exc:
        raise EnrollmentError(str(exc))
    except EnrollmentError:
        raise
    except Exception:
        current_app.logger.exception('Face detection failed')
        raise EnrollmentError('Error detecting face. Please retake the photo with better lighting.')


def encode_enrollment_images(face_images):
    """Turn the captured photos into face templates.

    Every photo must contain exactly one clear face, and all photos must show
    the SAME person. Returns ``(encodings, first_photo_bgr)`` where
    ``encodings`` is a list of 128-d arrays (one per photo, same order).
    Errors name the offending photo ("Photo 3: ...").
    """
    cfg = current_app.config

    decoded = []
    for number, data_url in enumerate(face_images, start=1):
        try:
            decoded.append(decode_and_normalize_image(data_url))
        except EnrollmentError as exc:
            raise EnrollmentError(f'Photo {number}: {exc}')

    encodings = []
    try:
        # ONE concurrency slot for the whole batch, so a single student cannot
        # hold several slots at once and starve everyone else.
        with get_gate(cfg['FACE_MAX_CONCURRENT']).slot(timeout=cfg['FACE_QUEUE_TIMEOUT'] * 2):
            for number, (_bgr, rgb) in enumerate(decoded, start=1):
                try:
                    encodings.append(_single_face_encoding(rgb, cfg['FACE_ENROLL_JITTERS']))
                except EnrollmentError as exc:
                    raise EnrollmentError(f'Photo {number}: {exc}')
    except FaceBusy:
        raise EnrollmentError('The server is busy right now. Please try again in a moment.')
    except FaceInputError as exc:
        raise EnrollmentError(str(exc))
    except EnrollmentError:
        raise
    except Exception:
        current_app.logger.exception('Face encoding failed during enrollment')
        raise EnrollmentError('Error detecting face. Please retake the photos with better lighting.')

    consistent, bad_index, worst = check_capture_consistency(encodings, cfg['ENROLL_CONSISTENCY_MAX'])
    if not consistent:
        current_app.logger.warning('Enrollment photos inconsistent: worst pairwise distance %.3f (photo %s)',
                                   worst, bad_index + 1)
        raise EnrollmentError(
            f'❌ Photo {bad_index + 1} does not seem to show the same person as the others. '
            f'Make sure only you are in front of the camera, then retake the photos.')

    return encodings, decoded[0][0]


def reject_if_duplicate(face_encodings, user_id=None):
    """Refuse enrollment if ANY of the new templates unambiguously matches a
    different, already-enrolled person. Accepts one encoding or a list."""
    arr = np.asarray(face_encodings, dtype=np.float64)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)

    for encoding in arr:
        duplicate = check_face_duplicate(encoding)
        if duplicate and duplicate.user_id != user_id:
            # Details go to the server log only: showing another person's name,
            # ID, course and e-mail to whoever tries to enroll leaks personal data.
            current_app.logger.warning(
                'Duplicate face on enrollment: user=%s matches student_pk=%s (%s)',
                user_id, duplicate.id, duplicate.student_id)
            raise EnrollmentError(
                '🚫 This face is already registered in the system. '
                'Each person can only enroll once. If you think this is a mistake, '
                'please contact the administrator.'
            )


def save_face_image(img_bgr, student_id):
    """Optionally keep the enrollment photo (STORE_FACE_IMAGES=true).

    Returns just the file NAME (relative to FACES_DIR), or None when storing is
    off or fails. Failing to keep a photo never blocks an enrollment.
    """
    if not current_app.config.get('STORE_FACE_IMAGES'):
        return None

    # student_id is user-supplied - sanitise before it touches the filesystem.
    safe_student_id = re.sub(r'[^A-Za-z0-9_-]', '', student_id)[:40] or 'student'
    stamp = utcnow().strftime('%Y%m%d%H%M%S')
    face_filename = f'{safe_student_id}_{stamp}_{secrets.token_hex(4)}.jpg'
    face_path = os.path.join(current_app.config['FACES_DIR'], face_filename)

    try:
        os.makedirs(os.path.dirname(face_path), exist_ok=True)
        if not cv2.imwrite(face_path, img_bgr):
            raise OSError('cv2.imwrite returned False')
    except Exception:
        current_app.logger.exception('Could not store face image; continuing without it')
        return None

    return face_filename


def face_image_path(stored_value):
    """Absolute path of a stored photo. Understands both the new relative file
    names and the absolute paths written by older versions."""
    if not stored_value:
        return None
    if os.path.isabs(stored_value):
        return stored_value
    return os.path.join(current_app.config['FACES_DIR'], os.path.basename(stored_value))


def delete_face_image(stored_value):
    path = face_image_path(stored_value)
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            current_app.logger.warning('Could not delete face image %s', path)