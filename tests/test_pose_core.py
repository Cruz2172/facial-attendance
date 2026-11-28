"""Head-pose measurement and per-pose rules, tested with synthetic faces
projected at KNOWN angles. No dlib, OpenCV, Flask or database needed.

Run:  pytest tests/test_pose_core.py
"""
import importlib.util
import os

import numpy as np
import pytest

_path = os.path.join(os.path.dirname(__file__), '..', 'app', 'pose_core.py')
_spec = importlib.util.spec_from_file_location('pose_core', _path)
pc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pc)

import sys
sys.path.insert(0, os.path.dirname(__file__))
import pose_helpers as ph  # noqa: E402

W, H = 640, 480
GENERIC = ph.GENERIC
personal_model = ph.personal_model


def synth(**kw):
    return ph.synth(w=W, h=H, **kw)


def box(**kw):
    return ph.box(w=W, h=H, **kw)


def metrics(**kw):
    b = kw.pop('box', box())
    return pc.measure(synth(**kw), b, W, H)


def verdict(index, baseline, **kw):
    return pc.evaluate_pose(index, metrics(**kw), baseline)


BASE = pc.baseline_from(metrics())


# ----------------------------------------------------------------- measuring
def test_equivalent_degrees_track_the_true_turn_with_the_right_sign():
    for yaw in (-35, -20, -12, 0, 12, 20, 35):
        got = pc.yaw_left_deg(metrics(yaw_left=yaw))
        assert got == pytest.approx(yaw, abs=max(1.0, 0.10 * abs(yaw))), yaw


def test_equivalent_degrees_track_the_true_chin_movement_with_the_right_sign():
    for pitch in (-30, -15, 0, 15, 30):
        got = pc.pitch_up_deg(metrics(pitch_up=pitch), BASE)
        assert got == pytest.approx(pitch, abs=max(1.0, 0.12 * abs(pitch))), pitch


def test_left_turn_moves_the_nose_toward_image_right_and_chin_up_raises_the_nose():
    p = synth(yaw_left=25)
    assert p['nose'][0] > (p['eye_l'][0] + p['eye_r'][0]) / 2
    assert pc.yaw_left_deg(metrics(yaw_left=25)) > 0
    assert metrics(pitch_up=20)['pitch_ratio'] < metrics(pitch_up=-20)['pitch_ratio']


def test_sideways_head_tilt_does_not_disturb_the_ratios_but_is_reported_as_roll():
    flat, tilted = metrics(yaw_left=20, pitch_up=10), metrics(yaw_left=20, pitch_up=10, roll=18)
    assert tilted['yaw_ratio'] == pytest.approx(flat['yaw_ratio'], abs=1e-9)
    assert tilted['pitch_ratio'] == pytest.approx(flat['pitch_ratio'], abs=1e-9)
    assert abs(metrics(roll=18)['roll']) == pytest.approx(18, abs=0.01)     # a pure tilt is reported as roll
    assert abs(metrics()['roll']) < 0.01


def test_landmark_noise_costs_a_couple_of_degrees():
    errs = []
    for seed in range(300):
        r = np.random.default_rng(seed)
        yaw, pitch = r.uniform(-30, 30), r.uniform(-20, 20)
        m = pc.measure(synth(yaw_left=yaw, pitch_up=pitch, noise=1.5, seed=seed), box(), W, H)
        errs.append((abs(pc.yaw_left_deg(m) - yaw), abs(pc.pitch_up_deg(m, BASE) - pitch)))
    errs = np.array(errs)
    assert errs.mean(axis=0).max() < 3.5 and np.percentile(errs, 95) < 8.0, (errs.mean(axis=0), np.percentile(errs, 95))


def test_mouth_ratio_grows_when_smiling():
    assert metrics(smile=0.15)['mouth_ratio'] > metrics()['mouth_ratio'] * 1.10


def test_face_size_and_position_are_measured():
    m = metrics(box=box(cx=0.1, cy=-0.05, h_ratio=0.6))
    assert m['face_h'] == pytest.approx(0.6) and m['cx'] == pytest.approx(0.1) and m['cy'] == pytest.approx(-0.05)


def test_unusable_landmarks_return_none():
    assert pc.measure({'nose': (1, 1)}, box(), W, H) is None
    assert pc.measure(None, box(), W, H) is None
    bad = synth(); bad['chin'] = (float('nan'), 1.0)
    assert pc.measure(bad, box(), W, H) is None
    same = {k: (10.0, 10.0) for k in ('nose', 'chin', 'eye_l', 'eye_r', 'mouth_l', 'mouth_r')}
    assert pc.measure(same, box(), W, H) is None
    upside = synth(); upside['chin'] = (upside['chin'][0], 5.0)            # chin above the eyes
    assert pc.measure(upside, box(), W, H) is None
    assert pc.measure(synth(), box(), 0, H) is None


def test_key_points_reads_face_recognition_landmark_dicts():
    lm = {
        'chin': [(i, 0) for i in range(17)],
        'nose_bridge': [(27, 1), (28, 1), (29, 1), (30, 1)],
        'left_eye': [(36, 2), (37, 2), (38, 2), (39, 2), (40, 2), (41, 2)],
        'right_eye': [(42, 3), (43, 3), (44, 3), (45, 3), (46, 3), (47, 3)],
        'top_lip': [(48 + i, 4) for i in range(12)],
    }
    kp = pc.key_points(lm)
    assert kp == {'nose': (30, 1), 'chin': (8, 0), 'eye_l': (36, 2), 'eye_r': (45, 3),
                  'mouth_l': (48, 4), 'mouth_r': (54, 4)}
    assert pc.key_points({}) is None and pc.key_points(None) is None


# ----------------------------------------------------------------- the rules
def test_straight_pose_accepts_a_frontal_face_and_rejects_turned_or_tilted_ones():
    assert verdict(0, None).ok
    assert verdict(0, None, yaw_left=20).code == 'look_straight'
    assert verdict(0, None, yaw_left=-20).code == 'look_straight'
    assert verdict(0, None, roll=20).code == 'tilt'


def test_every_pose_needs_the_straight_baseline_first():
    for i in range(1, 7):
        assert verdict(i, None).code == 'need_baseline'


def test_left_and_right_turn_rules():
    assert verdict(1, BASE, yaw_left=20).ok                              # left
    assert verdict(2, BASE, yaw_left=-20).ok                             # right
    assert verdict(1, BASE, yaw_left=5).code == 'turn_more'              # not enough
    assert verdict(1, BASE, yaw_left=0).code == 'turn_more'              # staying straight is not a turn
    assert verdict(1, BASE, yaw_left=-20).code == 'wrong_way'            # turned the other way
    assert verdict(2, BASE, yaw_left=20).code == 'wrong_way'
    assert verdict(1, BASE, yaw_left=55).code == 'turn_less'             # too far
    assert verdict(1, BASE, yaw_left=20, pitch_up=25).code == 'level_chin'


def test_up_and_down_rules():
    assert verdict(3, BASE, pitch_up=15).ok
    assert verdict(6, BASE, pitch_up=-15).ok
    assert verdict(3, BASE, pitch_up=3).code == 'tilt_more'
    assert verdict(3, BASE, pitch_up=-15).code == 'wrong_way'
    assert verdict(3, BASE, pitch_up=45).code == 'tilt_less'
    assert verdict(3, BASE, pitch_up=15, yaw_left=25).code == 'look_straight'
    assert verdict(6, BASE, pitch_up=3).code == 'tilt_more'


def test_smile_rule():
    assert verdict(4, BASE, smile=0.18).ok
    assert verdict(4, BASE, smile=0.0).code == 'smile'
    assert verdict(4, BASE, smile=0.18, yaw_left=25).code == 'look_straight'


def test_closer_rule_is_relative_to_the_baseline_size():
    assert verdict(5, BASE, box=box(h_ratio=0.62)).ok
    assert verdict(5, BASE, box=box(h_ratio=0.52)).code == 'too_far'


def test_position_and_distance_rules_apply_to_every_pose():
    assert verdict(0, None, box=box(cx=0.3)).code == 'center'
    assert verdict(0, None, box=box(cy=-0.3)).code == 'center'
    assert verdict(0, None, box=box(h_ratio=0.2)).code == 'too_far'
    assert verdict(0, None, box=box(h_ratio=0.9)).code == 'too_close'
    assert verdict(1, BASE, yaw_left=20, box=box(cx=0.3)).code == 'center'


def test_unknown_pose_index():
    assert pc.evaluate_pose(9, metrics(), BASE).code == 'bad_pose'
    assert pc.evaluate_pose(-1, metrics(), BASE).code == 'bad_pose'


def test_slack_accepts_borderline_photos_that_the_strict_live_check_rejects():
    borderline = metrics(yaw_left=9)                                      # 9 deg turn: just under 12
    strict = pc.evaluate_pose(1, borderline, BASE)
    loose = pc.evaluate_pose(1, borderline, BASE, pc.Tuning().with_slack(4))
    assert not strict.ok and loose.ok
    assert pc.Tuning().with_slack(0) == pc.Tuning()


def test_slack_does_not_make_wrong_poses_pass():
    t = pc.Tuning().with_slack(4)
    assert not pc.evaluate_pose(1, metrics(yaw_left=0), BASE, t).ok       # straight is not "left"
    assert not pc.evaluate_pose(1, metrics(yaw_left=-20), BASE, t).ok     # wrong direction
    assert not pc.evaluate_pose(4, metrics(smile=0.0), BASE, t).ok        # no smile
    assert not pc.evaluate_pose(3, metrics(pitch_up=1), BASE, t).ok       # no chin movement


# ----------------------------------------------------------------- real faces are not the generic model
def test_it_works_for_a_population_of_differently_shaped_faces():
    rng = np.random.default_rng(3)
    failures = []
    for n in range(60):
        person = personal_model(depth=rng.uniform(0.8, 1.25), width=rng.uniform(0.9, 1.1), length=rng.uniform(0.9, 1.1))
        base = pc.baseline_from(pc.measure(synth(model=person), box(), W, H))

        def run(index, **kw):
            return pc.evaluate_pose(index, pc.measure(synth(model=person, **kw), box(), W, H), base)

        checks = {
            'straight': run(0).ok,
            'left 22': run(1, yaw_left=22).ok, 'right 22': run(2, yaw_left=-22).ok,
            'up 16': run(3, pitch_up=16).ok, 'down 16': run(6, pitch_up=-16).ok,
            'smile': run(4, smile=0.2).ok,
            'fake left (3 deg) rejected': not run(1, yaw_left=3).ok,
            'fake right (3 deg) rejected': not run(2, yaw_left=-3).ok,
            'fake up (1 deg) rejected': not run(3, pitch_up=1).ok,
            'no smile rejected': not run(4, smile=0.0).ok,
            'wrong way rejected': not run(1, yaw_left=-22).ok,
        }
        failures += [(n, k) for k, ok in checks.items() if not ok]
    assert not failures, failures[:10]


def test_a_full_five_photo_session_passes_with_realistic_noise():
    person = personal_model(depth=1.2, width=0.95)
    plan = [(0, {}), (1, {'yaw_left': 22}), (2, {'yaw_left': -22}), (3, {'pitch_up': 16}), (4, {'smile': 0.2})]
    base = None
    for index, kw in plan:
        m = pc.measure(synth(model=person, noise=1.0, seed=index, **kw), box(), W, H)
        result = pc.evaluate_pose(index, m, base)
        assert result.ok, (index, result)
        if index == 0:
            base = pc.baseline_from(m)


# ----------------------------------------------------------------- browser-supplied baseline
def test_clean_baseline_validates_untrusted_input():
    good = {'yaw_ratio': 0.01, 'pitch_ratio': 0.34, 'mouth_ratio': 0.6, 'face_h': 0.5}
    assert pc.clean_baseline(good) == {k: float(v) for k, v in good.items()}
    for bad in (None, 5, 'x', {}, {**good, 'yaw_ratio': 'a'}, {**good, 'yaw_ratio': float('nan')},
                {**good, 'face_h': 5}, {**good, 'mouth_ratio': True}, {**good, 'pitch_ratio': -3},
                {k: v for k, v in good.items() if k != 'face_h'}):
        assert pc.clean_baseline(bad) is None