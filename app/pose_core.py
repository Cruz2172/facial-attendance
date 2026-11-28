"""Head pose, position and expression rules for GUIDED enrollment.

Pure logic: depends only on the standard library, never on Flask, the database,
OpenCV or dlib, so it can be unit-tested with synthetic faces.

How it works
------------
1. dlib's 68-point landmarks give six key points (nose tip, chin, outer eye
   corners, mouth corners).
2. Two landmark RATIOS are measured along the line between the eyes, so
   sideways head tilt does not disturb them:
     yaw_ratio    nose-tip offset from the eye midpoint / eye distance
                  (about 0 when facing the camera; grows as the head turns)
     pitch_ratio  where the nose tip sits between the eye line and the chin
                  (falls as the chin goes up, rises as it goes down)
3. Every pose after the first is judged RELATIVE to that student's own
   straight-on photo (the "baseline"). Tested over hundreds of random face
   shapes, these ratio changes are 2-3x more consistent between people than
   angles from a generic 3-D head model, which is why no head model is used.
4. ``evaluate_pose`` compares the measurements with the rule for the requested
   pose (straight, left, right, up, smile, closer, down) and returns a verdict
   plus a short instruction for the student ("Turn a little more to your left").

The numbers are "equivalent degrees": ratio changes divided by a fixed gain
calibrated on synthetic faces (accurate to roughly +-10 %). They are good
enough to tell a real turn from a fake one; they are not a protractor.

Sign conventions
----------------
  yaw   > 0  the person turned their head to THEIR OWN left
  pitch > 0  chin raised / looking up
"""
import math
from collections import namedtuple
from dataclasses import dataclass, replace

# Ratio change per degree of head rotation (calibrated on synthetic faces).
YAW_GAIN = 0.0070
PITCH_GAIN = 0.0051

POSE_KEYS = ('straight', 'left', 'right', 'up', 'smile', 'closer', 'down')

PoseResult = namedtuple('PoseResult', 'ok code message')


@dataclass(frozen=True)
class Tuning:
    """Every threshold in one place (angles are equivalent degrees)."""
    straight_max_yaw: float = 10.0
    max_roll: float = 12.0
    turn_min: float = 12.0          # left / right: how far from the baseline
    turn_max: float = 38.0
    pitch_min: float = 8.0          # up / down: how far from the baseline
    pitch_max: float = 30.0
    off_axis_max: float = 12.0      # the OTHER axis may drift at most this much
    smile_min_increase: float = 0.08   # mouth width / eye distance vs baseline
    closer_min_ratio: float = 1.12     # face height vs baseline
    face_h_min: float = 0.30           # face height / frame height
    face_h_max: float = 0.75
    center_x_tol: float = 0.15         # face centre offset from frame centre
    center_y_tol: float = 0.20

    def with_slack(self, degrees):
        """Looser copy for the final server-side re-check (the stored photo may
        measure a little differently from the live frame that was approved)."""
        if degrees <= 0:
            return self
        return replace(
            self,
            straight_max_yaw=self.straight_max_yaw + degrees,
            max_roll=self.max_roll + degrees,
            turn_min=max(4.0, self.turn_min - degrees),
            turn_max=self.turn_max + degrees,
            pitch_min=max(3.0, self.pitch_min - degrees),
            pitch_max=self.pitch_max + degrees,
            off_axis_max=self.off_axis_max + degrees,
            smile_min_increase=max(0.03, self.smile_min_increase - 0.02),
            closer_min_ratio=max(1.05, self.closer_min_ratio - 0.03),
            face_h_min=self.face_h_min * 0.9,
            face_h_max=min(0.95, self.face_h_max * 1.1),
            center_x_tol=self.center_x_tol * 1.3,
            center_y_tol=self.center_y_tol * 1.3,
        )


# ---------------------------------------------------------------------------
# Measuring
# ---------------------------------------------------------------------------
def key_points(landmarks):
    """face_recognition landmark dict -> the six points we need, or None.

    face_recognition slices dlib's 68 points into named groups; the indexes
    below are the positions of dlib points 30, 8, 36, 45, 48 and 54.
    """
    try:
        return {
            'nose': landmarks['nose_bridge'][3],
            'chin': landmarks['chin'][8],
            'eye_l': landmarks['left_eye'][0],
            'eye_r': landmarks['right_eye'][3],
            'mouth_l': landmarks['top_lip'][0],
            'mouth_r': landmarks['top_lip'][6],
        }
    except (KeyError, IndexError, TypeError):
        return None


def measure(points, box, width, height):
    """Everything the rules need, or None if the landmarks are unusable.

    ``box`` is (top, right, bottom, left) in pixels of a ``width`` x ``height`` frame.
    """
    try:
        nx, ny = (float(v) for v in points['nose'])
        cx_, cy_ = (float(v) for v in points['chin'])
        lx, ly = (float(v) for v in points['eye_l'])
        rx, ry = (float(v) for v in points['eye_r'])
        mlx, mly = (float(v) for v in points['mouth_l'])
        mrx, mry = (float(v) for v in points['mouth_r'])
        top, right, bottom, left = (float(v) for v in box)
    except (KeyError, TypeError, ValueError):
        return None

    values = (nx, ny, cx_, cy_, lx, ly, rx, ry, mlx, mly, mrx, mry, top, right, bottom, left)
    if not all(math.isfinite(v) for v in values) or width <= 0 or height <= 0:
        return None

    span = math.hypot(rx - lx, ry - ly)                 # distance between the outer eye corners
    if span < 1e-6:
        return None
    ux, uy = (rx - lx) / span, (ry - ly) / span          # unit vector along the eyes
    vx, vy = -uy, ux                                     # perpendicular
    if vy < 0:                                           # make it point DOWN the face
        vx, vy = -vx, -vy

    mid_x, mid_y = (lx + rx) / 2.0, (ly + ry) / 2.0
    chin_along_v = (cx_ - mid_x) * vx + (cy_ - mid_y) * vy
    if chin_along_v < 1e-6:                              # chin above the eyes: not a real face
        return None

    return {
        'yaw_ratio': ((nx - mid_x) * ux + (ny - mid_y) * uy) / span,
        'pitch_ratio': ((nx - mid_x) * vx + (ny - mid_y) * vy) / chin_along_v,
        'roll': math.degrees(math.atan2(ry - ly, rx - lx)),
        'mouth_ratio': math.hypot(mrx - mlx, mry - mly) / span,
        'face_h': (bottom - top) / float(height),
        'cx': ((left + right) / 2.0) / width - 0.5,
        'cy': ((top + bottom) / 2.0) / height - 0.5,
    }


def baseline_from(metrics):
    """The numbers later poses are compared with (from the straight photo)."""
    return {k: float(metrics[k]) for k in ('yaw_ratio', 'pitch_ratio', 'mouth_ratio', 'face_h')}


def clean_baseline(raw):
    """Validate a baseline received from a browser. Returns a dict or None."""
    if not isinstance(raw, dict):
        return None
    limits = {'yaw_ratio': (-0.6, 0.6), 'pitch_ratio': (0.0, 1.5),
              'mouth_ratio': (0.05, 2.0), 'face_h': (0.05, 1.0)}
    out = {}
    for key, (lo, hi) in limits.items():
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or not lo <= value <= hi:
            return None
        out[key] = float(value)
    return out


def yaw_left_deg(metrics, baseline=None):
    """Equivalent degrees turned to the person's left (relative to the baseline if given)."""
    ratio = metrics['yaw_ratio'] - (baseline['yaw_ratio'] if baseline else 0.0)
    return ratio / YAW_GAIN


def pitch_up_deg(metrics, baseline):
    """Equivalent degrees the chin is raised compared with the baseline."""
    return -(metrics['pitch_ratio'] - baseline['pitch_ratio']) / PITCH_GAIN


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
def _position_problem(m, t, relative_size):
    if abs(m['cx']) > t.center_x_tol or abs(m['cy']) > t.center_y_tol:
        return PoseResult(False, 'center', 'Center your face in the oval.')
    if not relative_size:
        if m['face_h'] < t.face_h_min:
            return PoseResult(False, 'too_far', 'Move a little closer to the camera.')
        if m['face_h'] > t.face_h_max:
            return PoseResult(False, 'too_close', 'Move back a little.')
    return None


def _turn_result(toward, lo, hi, side):
    """``toward`` is signed so that positive = toward the requested side."""
    if toward < -(lo / 2.0):
        return PoseResult(False, 'wrong_way', f'Wrong way: turn your head to YOUR {side}.')
    if toward < lo:
        return PoseResult(False, 'turn_more', f'Turn a little more to your {side}.')
    if toward > hi:
        return PoseResult(False, 'turn_less', "That's too far: turn back a little.")
    return None


def evaluate_pose(index, m, baseline, tuning=None):
    """Judge measurement ``m`` against the rule for pose ``index``.

    Returns PoseResult(ok, code, message). For every pose except the first,
    ``baseline`` (from the straight photo) is required.
    """
    t = tuning or Tuning()
    if index < 0 or index >= len(POSE_KEYS):
        return PoseResult(False, 'bad_pose', 'Unknown pose.')
    key = POSE_KEYS[index]

    if key != 'straight' and baseline is None:
        return PoseResult(False, 'need_baseline', 'Take the first (straight) photo before this one.')

    problem = _position_problem(m, t, relative_size=(key == 'closer'))
    if problem:
        return problem

    if abs(m['roll']) > t.max_roll:
        return PoseResult(False, 'tilt', "Keep your head level: don't tilt it sideways.")

    if key == 'straight':
        # Facing the camera means the nose is centred between the eyes. (Chin
        # height cannot be judged without a baseline, so only turning is checked.)
        if abs(yaw_left_deg(m)) > t.straight_max_yaw:
            return PoseResult(False, 'look_straight', 'Look straight at the camera.')
        return PoseResult(True, 'ok', 'Good. Hold still...')

    d_yaw = yaw_left_deg(m, baseline)
    d_pitch = pitch_up_deg(m, baseline)

    if key in ('left', 'right'):
        side = 'left' if key == 'left' else 'right'
        toward = d_yaw if key == 'left' else -d_yaw
        bad = _turn_result(toward, t.turn_min, t.turn_max, side)
        if bad:
            return bad
        if abs(d_pitch) > t.off_axis_max:
            return PoseResult(False, 'level_chin', 'Keep your chin level while you turn.')
        return PoseResult(True, 'ok', 'Good. Hold still...')

    if key in ('up', 'down'):
        toward = d_pitch if key == 'up' else -d_pitch
        if toward < -(t.pitch_min / 2.0):
            return PoseResult(False, 'wrong_way',
                              'Wrong way: raise your chin.' if key == 'up' else 'Wrong way: lower your chin.')
        if toward < t.pitch_min:
            return PoseResult(False, 'tilt_more',
                              'Raise your chin a little more.' if key == 'up' else 'Lower your chin a little more.')
        if toward > t.pitch_max:
            return PoseResult(False, 'tilt_less', "That's too far: come back a little.")
        if abs(d_yaw) > t.off_axis_max:
            return PoseResult(False, 'look_straight', "Don't turn sideways: face the camera.")
        return PoseResult(True, 'ok', 'Good. Hold still...')

    # smile / closer: face the camera as in the baseline
    if abs(d_yaw) > t.off_axis_max or abs(d_pitch) > t.off_axis_max:
        return PoseResult(False, 'look_straight', 'Face the camera, like in the first photo.')

    if key == 'smile':
        if m['mouth_ratio'] < baseline['mouth_ratio'] * (1.0 + t.smile_min_increase):
            return PoseResult(False, 'smile', 'Smile a little wider.')
        return PoseResult(True, 'ok', 'Good. Hold still...')

    # closer
    if m['face_h'] < baseline['face_h'] * t.closer_min_ratio:
        return PoseResult(False, 'too_far', 'Move a little closer to the camera.')
    if m['face_h'] > t.face_h_max * 1.15:
        return PoseResult(False, 'too_close', 'Move back a little.')
    return PoseResult(True, 'ok', 'Good. Hold still...')