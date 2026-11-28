"""Unit tests for app/face_core.py (no Flask, DB or dlib required)."""
import base64
import importlib.util
import json
import os
import sys
import threading
import time
import types

import cv2
import numpy as np
import pytest

_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'face_core.py')
_spec = importlib.util.spec_from_file_location('face_core', _path)
fc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fc)


def data_url(img_bgr, ext='.jpg'):
    ok, buf = cv2.imencode(ext, img_bgr)
    assert ok
    mime = 'image/jpeg' if ext == '.jpg' else 'image/png'
    return f'data:{mime};base64,' + base64.b64encode(buf.tobytes()).decode()


# ---------------------------- decoding ------------------------------------
def test_decode_ok_and_contiguous():
    img = np.random.randint(0, 255, (120, 160, 3), np.uint8)
    bgr, rgb = fc.decode_data_url(data_url(img, '.png'))
    assert rgb.dtype == np.uint8 and rgb.flags['C_CONTIGUOUS'] and rgb.shape == (120, 160, 3)


def test_decode_downscales_large_images():
    img = np.zeros((2000, 3000, 3), np.uint8)
    _, rgb = fc.decode_data_url(data_url(img), max_dim=640)
    assert max(rgb.shape[:2]) == 640


def test_decode_never_enlarges():
    img = np.zeros((100, 100, 3), np.uint8)
    _, rgb = fc.decode_data_url(data_url(img), max_dim=640)
    assert rgb.shape[:2] == (100, 100)


@pytest.mark.parametrize('bad', [None, '', '   ', 'hello', 'data:text/plain;base64,aGk=',
                                 'data:image/jpeg;base64,', 'data:image/jpeg;base64,@@@@',
                                 'data:image/jpeg;base64,' + base64.b64encode(b'not an image').decode(),
                                 12345])
def test_decode_rejects_garbage(bad):
    with pytest.raises(fc.FaceInputError):
        fc.decode_data_url(bad)


def test_decode_rejects_pixel_bomb():
    big = np.zeros((600, 600, 3), np.uint8)       # 360k px, compresses to nothing
    with pytest.raises(fc.FaceInputError):
        fc.decode_data_url(data_url(big, '.png'), max_pixels=100_000)


def test_decode_tolerates_whitespace_and_missing_padding():
    img = np.random.randint(0, 255, (40, 40, 3), np.uint8)
    url = data_url(img, '.png')
    header, payload = url.split(',', 1)
    mangled = header + ',' + payload.rstrip('=')[:20] + '\n' + payload.rstrip('=')[20:]
    bgr, _ = fc.decode_data_url(mangled)
    assert bgr.shape == (40, 40, 3)


# ---------------------------- face selection ------------------------------
def box(size, x=0, y=0):
    return (y, x + size, y + size, x)       # top, right, bottom, left


def test_no_face():
    assert fc.select_primary_face([]) == (None, 'no_face')


def test_single_face_ok():
    b = box(100)
    assert fc.select_primary_face([b]) == (b, 'ok')


def test_too_small():
    assert fc.select_primary_face([box(30)], min_size=60) == (None, 'too_small')


def test_background_faces_ignored_when_dominant():
    main, bg = box(160), box(70, 300)
    assert fc.select_primary_face([bg, main], min_size=60, ratio=1.6)[0] == main


def test_tiny_background_face_below_min_size_ignored():
    main, tiny = box(120), box(40, 300)
    assert fc.select_primary_face([main, tiny], min_size=60, ratio=float('inf')) == (main, 'ok')


def test_two_similar_faces_is_multiple():
    assert fc.select_primary_face([box(120), box(110, 200)], ratio=1.6) == (None, 'multiple')


def test_enrollment_strict_mode_rejects_two_real_faces():
    assert fc.select_primary_face([box(160), box(90, 300)], ratio=float('inf')) == (None, 'multiple')


# ---------------------------- encodings -----------------------------------
def test_parse_encoding():
    good = json.dumps(list(np.linspace(0, 1, 128)))
    assert fc.parse_encoding(good).shape == (128,)
    for bad in [None, '', '[]', '[1,2,3]', 'not json', json.dumps([float('nan')] * 128).replace('NaN', 'NaN')]:
        assert fc.parse_encoding(bad) is None


# ---------------------------- matching ------------------------------------
rng = np.random.default_rng(0)


def make_db(n):
    return rng.normal(0, 1, (n, 128)) * 0.09        # distinct people: far apart (~1.0)


def noisy(v, d):                                     # same person, distance ~ d
    n = rng.normal(0, 1, 128)
    return v + n / np.linalg.norm(n) * d


def test_identify_true_match():
    m = make_db(500)
    idx, dist, status = fc.identify(m, noisy(m[123], 0.35), 0.50, 0.07)
    assert (idx, status) == (123, 'ok') and dist == pytest.approx(0.35, abs=1e-6)


def test_identify_unknown_person():
    m = make_db(500)
    assert fc.identify(m, make_db(1)[0], 0.50, 0.07)[2] == 'unknown'


def test_identify_ambiguous_lookalike():
    m = make_db(50)
    probe = noisy(m[10], 0.30)                       # true match at 0.30
    m[11] = noisy(probe, 0.34)                       # a lookalike at 0.34 (< 0.07 behind)
    idx, dist, status = fc.identify(m, probe, 0.50, 0.07)
    assert status == 'ambiguous' and idx is None
    assert dist == pytest.approx(0.30, abs=1e-6)


def test_identify_clear_winner_over_far_runner_up():
    m = make_db(50)
    probe = noisy(m[10], 0.30)
    m[11] = noisy(probe, 0.45)                       # runner-up 0.15 behind: fine
    assert fc.identify(m, probe, 0.50, 0.07)[::2] == (10, 'ok')


def test_identify_single_enrolled_face():
    m = make_db(1)
    assert fc.identify(m, noisy(m[0], 0.2), 0.5, 0.07)[::2] == (0, 'ok')


def test_identify_empty_db():
    assert fc.identify(np.empty((0, 128)), np.zeros(128), 0.5, 0.07) == (None, None, 'unknown')
    assert fc.identify(None, np.zeros(128), 0.5, 0.07) == (None, None, 'unknown')


def test_identify_threshold_boundary():
    m = make_db(5)
    probe = noisy(m[0], 0.51)
    assert fc.identify(m, probe, 0.50, 0.07)[2] == 'unknown'


def test_duplicate_detection():
    m = make_db(300)
    assert fc.find_duplicate(m, noisy(m[7], 0.05), 0.5, 0.07) == 7
    assert fc.find_duplicate(m, make_db(1)[0], 0.5, 0.07) is None
    assert fc.find_duplicate(np.empty((0, 128)), np.zeros(128), 0.5, 0.07) is None


def test_matching_is_fast_at_university_scale():
    m = make_db(20000)
    probe = noisy(m[999], 0.3)
    t = time.perf_counter()
    for _ in range(20):
        fc.identify(m, probe, 0.5, 0.07)
    per_call_ms = (time.perf_counter() - t) / 20 * 1000
    assert per_call_ms < 50, per_call_ms


# ---------------------------- detection with a fake dlib ------------------
@pytest.fixture
def fake_fr(monkeypatch):
    mod = types.ModuleType('face_recognition')
    calls = {}
    mod.face_locations = lambda img, number_of_times_to_upsample=1, model='hog': calls.setdefault('loc', [(10, 110, 110, 10)])
    mod.face_encodings = lambda img, known_face_locations=None, num_jitters=1: (
        [np.ones(128) * 0.1] if known_face_locations else [])
    monkeypatch.setitem(sys.modules, 'face_recognition', mod)
    return mod


def test_detect_and_encode_with_fake_dlib(fake_fr):
    img = np.zeros((200, 200, 3), np.uint8)
    boxes = fc.detect_faces(img)
    assert boxes == [(10, 110, 110, 10)]
    enc = fc.encode_face(img, boxes[0], jitters=1)
    assert enc.shape == (128,)


def test_encode_failure_is_user_safe(fake_fr):
    fake_fr.face_encodings = lambda *a, **k: []
    with pytest.raises(fc.FaceInputError):
        fc.encode_face(np.zeros((10, 10, 3), np.uint8), (0, 5, 5, 0))


# ---------------------------- concurrency gate ----------------------------
def test_gate_limits_and_releases():
    gate = fc.FaceGate(1)
    with gate.slot(timeout=0.2):
        with pytest.raises(fc.FaceBusy):
            with gate.slot(timeout=0.05):
                pass
    with gate.slot(timeout=0.2):      # released after the first block
        pass


def test_gate_releases_on_exception():
    gate = fc.FaceGate(1)
    with pytest.raises(RuntimeError):
        with gate.slot(timeout=0.2):
            raise RuntimeError('boom')
    with gate.slot(timeout=0.2):
        pass


def test_gate_under_threads():
    gate = fc.FaceGate(2)
    active, peak, lock = [0], [0], threading.Lock()

    def work():
        with gate.slot(timeout=5):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1

    threads = [threading.Thread(target=work) for _ in range(12)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert peak[0] <= 2


# =========================== multi-image enrollment ==========================
def pose_templates(base, n, spread):
    """n photos of ONE person: the base face plus small pose/expression drifts."""
    return np.vstack([base] + [noisy(base, spread) for _ in range(n - 1)])


def stack_people(people):
    """-> (matrix, starts) the way face_cache builds them."""
    matrix = np.vstack(people)
    starts = np.cumsum([0] + [len(p) for p in people[:-1]])
    return matrix, starts


def test_parse_encodings_reads_legacy_and_multi_format():
    one = json.dumps(list(np.linspace(0, 1, 128)))
    assert fc.parse_encodings(one).shape == (1, 128)                       # legacy
    multi = json.dumps([list(np.linspace(0, 1, 128))] * 5)
    assert fc.parse_encodings(multi).shape == (5, 128)                     # current
    assert fc.parse_encoding(multi).shape == (128,)                        # first template


def test_parse_encodings_drops_bad_rows_and_caps_count():
    good = list(np.linspace(0, 1, 128))
    mixed = json.dumps([good, [1, 2, 3], good, [float('nan')] * 128])
    assert fc.parse_encodings(mixed).shape == (2, 128)
    assert fc.parse_encodings(json.dumps([good] * 25)).shape == (fc.MAX_TEMPLATES_PER_PERSON, 128)
    for bad in [None, '', '[]', '[[]]', '[1,2,3]', 'oops', '{"a": 1}', json.dumps([[1, 2], [3, 4]])]:
        assert fc.parse_encodings(bad) is None


def test_serialize_roundtrip():
    encs = [rng.normal(0, 0.09, 128) for _ in range(4)]
    back = fc.parse_encodings(fc.serialize_encodings(encs))
    assert back.shape == (4, 128) and np.allclose(back, np.vstack(encs))


def test_own_templates_never_make_a_person_ambiguous():
    me = pose_templates(make_db(1)[0], 5, 0.12)            # 5 close photos of one person
    others = [pose_templates(v, 3, 0.12) for v in make_db(40)]
    matrix, starts = stack_people([me] + others)
    probe = noisy(me[0], 0.25)

    idx, dist, status = fc.identify_people(matrix, starts, probe, 0.50, 0.07)
    assert (idx, status) == (0, 'ok')
    # the old row-by-row logic would wrongly call this ambiguous: my own 2nd photo is the "runner-up"
    assert fc.identify(matrix, probe, 0.50, 0.07)[2] == 'ambiguous'


def orthonormal(k):
    """k exactly-orthogonal unit directions, so distances add predictably."""
    q, _ = np.linalg.qr(rng.normal(0, 1, (128, k)))
    return [q[:, i] for i in range(k)]


def test_extra_poses_rescue_a_match_the_frontal_photo_misses():
    u, v = orthonormal(2)
    base = make_db(1)[0]
    turned = base + 0.55 * u                                 # same person, head turned (within 0.55)
    probe = turned + 0.10 * v                                # later scan, head still turned
    assert np.linalg.norm(probe - base) > 0.50               # frontal-only enrollment would reject
    assert np.linalg.norm(probe - turned) < 0.11

    singles, s_starts = stack_people([base.reshape(1, -1)] + [x.reshape(1, -1) for x in make_db(30)])
    assert fc.identify_people(singles, s_starts, probe, 0.50, 0.07)[2] == 'unknown'

    matrix, starts = stack_people([np.vstack([base, turned])] + [x.reshape(1, -1) for x in make_db(30)])
    idx, dist, status = fc.identify_people(matrix, starts, probe, 0.50, 0.07)
    assert (idx, status) == (0, 'ok') and dist == pytest.approx(0.10, abs=1e-6)


def test_uneven_template_counts_map_to_the_right_person():
    people = [pose_templates(v, n, 0.1) for v, n in zip(make_db(6), (1, 5, 3, 7, 2, 4))]
    matrix, starts = stack_people(people)
    for i, p in enumerate(people):
        idx, _, status = fc.identify_people(matrix, starts, noisy(p[-1], 0.15), 0.50, 0.07)
        assert (idx, status) == (i, 'ok'), i


def test_lookalike_person_is_still_ambiguous():
    u, w, e1, e2 = orthonormal(4)
    a = make_db(1)[0]
    probe = a + 0.30 * u
    person_a = np.vstack([a, a + 0.05 * e1, a + 0.05 * e2])  # best template is 0.30 from the probe
    person_b = (probe + 0.34 * w).reshape(1, -1)              # a different person only 0.04 behind
    matrix, starts = stack_people([person_a, person_b])
    idx, dist, status = fc.identify_people(matrix, starts, probe, 0.50, 0.07)
    assert status == 'ambiguous' and idx is None and dist == pytest.approx(0.30, abs=1e-6)


def test_unknown_and_empty_with_people_api():
    matrix, starts = stack_people([pose_templates(v, 3, 0.1) for v in make_db(10)])
    assert fc.identify_people(matrix, starts, make_db(1)[0], 0.50, 0.07)[2] == 'unknown'
    assert fc.identify_people(np.empty((0, 128)), np.array([], int), np.zeros(128), 0.5, 0.07) == (None, None, 'unknown')
    assert fc.identify_people(None, None, np.zeros(128), 0.5, 0.07) == (None, None, 'unknown')


def test_duplicate_person_detection_with_multi_templates():
    people = [pose_templates(v, 4, 0.1) for v in make_db(25)]
    matrix, starts = stack_people(people)
    assert fc.find_duplicate_person(matrix, starts, noisy(people[7][2], 0.1), 0.5, 0.07) == 7
    assert fc.find_duplicate_person(matrix, starts, make_db(1)[0], 0.5, 0.07) is None


def test_capture_consistency_accepts_one_person_with_pose_changes():
    base = make_db(1)[0]
    shots = [base] + [noisy(base, 0.18) for _ in range(4)]
    ok, bad, worst = fc.check_capture_consistency(shots, 0.55)
    assert ok and bad is None and worst < 0.55


def test_capture_consistency_catches_a_different_person_in_one_shot():
    base = make_db(1)[0]
    shots = [base, noisy(base, 0.15), make_db(1)[0], noisy(base, 0.15), noisy(base, 0.2)]
    ok, bad, worst = fc.check_capture_consistency(shots, 0.55)
    assert not ok and bad == 2 and worst > 0.55


def test_capture_consistency_single_shot_is_trivially_fine():
    assert fc.check_capture_consistency([make_db(1)[0]], 0.55) == (True, None, 0.0)


def test_multi_template_matching_still_fast_at_university_scale():
    people = [np.vstack([v] * 5) for v in make_db(4000)]     # 4,000 students x 5 templates = 20,000 rows
    matrix, starts = stack_people(people)
    probe = noisy(people[1234][0], 0.3)
    t = time.perf_counter()
    for _ in range(20):
        idx, _, status = fc.identify_people(matrix, starts, probe, 0.5, 0.07)
    per_call_ms = (time.perf_counter() - t) / 20 * 1000
    assert (idx, status) == (1234, 'ok') and per_call_ms < 60, per_call_ms


def test_capture_consistency_blames_the_right_photo_wherever_the_stranger_is():
    base = make_db(1)[0]
    for position in range(5):
        shots = [noisy(base, 0.15) for _ in range(5)]
        shots[position] = make_db(1)[0]                      # a different person in this one photo
        ok, bad, worst = fc.check_capture_consistency(shots, 0.55)
        assert not ok and bad == position, (position, bad)