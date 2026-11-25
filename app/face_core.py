"""Face-processing building blocks with NO Flask / database dependency.

Everything that decides "is this image safe to process", "which face in the
frame do we use" and "who does this encoding match" lives here so it can be
unit-tested without dlib, a database or a running web server.

``face_recognition`` is imported lazily, only by the two functions that really
need dlib (``detect_faces`` / ``encode_face``).
"""
import base64
import binascii
import json
import threading
from contextlib import contextmanager

import cv2
import numpy as np

ENCODING_LEN = 128


class FaceInputError(ValueError):
    """The image sent by the client is unusable. The message is user-safe."""


class FaceBusy(RuntimeError):
    """Too many face jobs are running on this worker right now."""


# ---------------------------------------------------------------------------
# Image decoding
# ---------------------------------------------------------------------------
def _peek_pixels(raw):
    """Return width*height from the image header WITHOUT decoding pixels.

    Guards against "decompression bombs": a tiny PNG/JPEG that expands to a
    gigantic bitmap and exhausts RAM. Returns None if Pillow isn't available.
    """
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow ships with face_recognition
        return None
    try:
        with Image.open(BytesIO(raw)) as im:
            width, height = im.size
        return width * height
    except Exception:
        raise FaceInputError('That file is not a valid image. Please retake the photo.')


def downscale(img, max_dim):
    """Shrink so the longest side is at most max_dim (never enlarges)."""
    if not max_dim:
        return img
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return img
    scale = max_dim / float(longest)
    new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
    return cv2.resize(img, new_size, interpolation=cv2.INTER_AREA)


def decode_data_url(data_url, max_pixels=25_000_000, max_dim=None):
    """Decode a ``data:image/...;base64,...`` string.

    Returns ``(img_bgr, img_rgb)``; ``img_rgb`` is a C-contiguous uint8 array
    (what dlib requires). Raises FaceInputError with a user-safe message.
    """
    if not data_url or not isinstance(data_url, str):
        raise FaceInputError('No image was received. Please try again.')

    text = data_url.strip()
    header, sep, payload = text.partition(',')
    if not sep or not header.lower().startswith('data:image/'):
        raise FaceInputError('Invalid image data. Please capture a new photo.')

    payload = ''.join(payload.split())          # drop any whitespace/newlines
    payload += '=' * (-len(payload) % 4)        # tolerate stripped padding
    try:
        raw = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        raise FaceInputError('The image data was corrupted. Please try again.')
    if not raw:
        raise FaceInputError('The image was empty. Please try again.')

    pixels = _peek_pixels(raw)
    if pixels is not None and pixels > max_pixels:
        raise FaceInputError('That image is too large. Please use a smaller photo.')

    img_bgr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img_bgr is None or img_bgr.size == 0:
        raise FaceInputError('Could not read the image. Please retake the photo.')

    img_bgr = downscale(img_bgr, max_dim)
    img_rgb = np.ascontiguousarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB), dtype=np.uint8)
    return img_bgr, img_rgb


# ---------------------------------------------------------------------------
# Face detection / selection
# ---------------------------------------------------------------------------
def detect_faces(img_rgb, upsample=1, model='hog'):
    """Return face boxes as (top, right, bottom, left)."""
    import face_recognition
    return face_recognition.face_locations(img_rgb, number_of_times_to_upsample=upsample, model=model)


def _box_size(box):
    top, right, bottom, left = box
    return max(0, bottom - top), max(0, right - left)


def _box_area(box):
    h, w = _box_size(box)
    return h * w


def select_primary_face(boxes, min_size=60, ratio=1.6):
    """Pick the face to identify.

    Returns ``(box_or_None, status)`` where status is one of
      'ok'        exactly one usable face, or one clearly dominant face
      'no_face'   nothing detected
      'too_small' only faces smaller than ``min_size`` px (person too far away)
      'multiple'  several comparable faces - ambiguous who is being scanned

    Small background faces (a queue behind the student at the camera, posters)
    are ignored; the largest face is used if it is ``ratio`` times bigger by
    area than the runner-up. Pass ``ratio=float('inf')`` to demand exactly one
    face (used for enrollment).
    """
    if not boxes:
        return None, 'no_face'

    usable = [b for b in boxes if min(_box_size(b)) >= min_size]
    if not usable:
        return None, 'too_small'

    usable.sort(key=_box_area, reverse=True)
    if len(usable) == 1:
        return usable[0], 'ok'

    if _box_area(usable[0]) >= ratio * _box_area(usable[1]):
        return usable[0], 'ok'
    return None, 'multiple'


def encode_face(img_rgb, box, jitters=1):
    """128-d encoding (float64 ndarray) of the face in ``box``."""
    import face_recognition
    result = face_recognition.face_encodings(img_rgb, known_face_locations=[box], num_jitters=jitters)
    if not result:
        raise FaceInputError('Could not analyse the face. Please try again.')
    enc = np.asarray(result[0], dtype=np.float64)
    if enc.shape != (ENCODING_LEN,) or not np.all(np.isfinite(enc)):
        raise FaceInputError('Could not analyse the face. Please try again.')
    return enc


# ---------------------------------------------------------------------------
# Concurrency gate: stop a burst of scans from exhausting CPU / RAM
# ---------------------------------------------------------------------------
class FaceGate:
    def __init__(self, limit):
        self._sem = threading.BoundedSemaphore(max(1, int(limit)))

    @contextmanager
    def slot(self, timeout=8.0):
        if not self._sem.acquire(timeout=timeout):
            raise FaceBusy()
        try:
            yield
        finally:
            self._sem.release()


_gate = None
_gate_lock = threading.Lock()


def get_gate(limit):
    global _gate
    if _gate is None:
        with _gate_lock:
            if _gate is None:
                _gate = FaceGate(limit)
    return _gate


# ---------------------------------------------------------------------------
# Encodings stored in the database
# ---------------------------------------------------------------------------
def parse_encoding(raw):
    """JSON string -> float64 ndarray of length 128, or None if unusable."""
    if not raw:
        return None
    try:
        arr = np.asarray(json.loads(raw), dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if arr.shape != (ENCODING_LEN,) or not np.all(np.isfinite(arr)):
        return None
    return arr


# ---------------------------------------------------------------------------
# Matching (vectorised; same decision rules the app always used)
# ---------------------------------------------------------------------------
def face_distances(matrix, encoding):
    if matrix is None or len(matrix) == 0:
        return np.empty(0)
    return np.linalg.norm(matrix - encoding, axis=1)


def _two_smallest(distances):
    n = len(distances)
    if n == 1:
        return 0, float(distances[0]), None
    part = np.argpartition(distances, 1)[:2]
    first, second = (part[0], part[1]) if distances[part[0]] <= distances[part[1]] else (part[1], part[0])
    return int(first), float(distances[first]), float(distances[second])


def identify(matrix, encoding, threshold, margin):
    """1:N identification.

    Returns ``(index, best_distance, status)``; status is
    'ok' | 'unknown' (closest face too far) | 'ambiguous' (runner-up too close)
    """
    distances = face_distances(matrix, encoding)
    if len(distances) == 0:
        return None, None, 'unknown'

    best_idx, best, second = _two_smallest(distances)
    if best > threshold:
        return None, best, 'unknown'
    if second is not None and (second - best) < margin:
        return None, best, 'ambiguous'
    return best_idx, best, 'ok'


def find_duplicate(matrix, encoding, threshold, margin):
    """Index of an already-enrolled face that is unambiguously the same
    person, else None (same rules as the original duplicate check)."""
    distances = face_distances(matrix, encoding)
    if len(distances) == 0:
        return None
    best_idx, best, second = _two_smallest(distances)
    if best >= threshold:
        return None
    if second is not None and (second - best) < margin:
        return None
    return best_idx
