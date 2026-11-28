"""Synthetic faces for testing pose logic without dlib or a camera.

``synth`` projects a 3-D face turned by known angles into an image and returns
the key points; ``landmarks_dict`` wraps them in the shape that
``face_recognition.face_landmarks`` returns, so code that reads real dlib
output can be exercised end to end.
"""
import numpy as np

# A generic face in camera space: x right, y DOWN, z away from the camera, origin at the nose tip.
GENERIC = np.array([
    (0.0, 0.0, 0.0),          # nose tip
    (0.0, 330.0, 65.0),       # chin
    (-225.0, -170.0, 135.0),  # image-left outer eye corner
    (225.0, -170.0, 135.0),   # image-right outer eye corner
    (-150.0, 150.0, 125.0),   # image-left mouth corner
    (150.0, 150.0, 125.0),    # image-right mouth corner
])

# What a student should do for each guided pose (index = pose number), in the
# same order as pose_core.POSE_KEYS.
GOOD_SESSION = [
    {},                          # straight
    {'yaw_left': 22},            # turn left
    {'yaw_left': -22},           # turn right
    {'pitch_up': 16},            # chin up
    {'smile': 0.2},              # smile
    {},                          # closer (box is enlarged separately)
    {'pitch_up': -16},           # chin down
]


def _rx(a): c, s = np.cos(a), np.sin(a); return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
def _ry(a): c, s = np.cos(a), np.sin(a); return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
def _rz(a): c, s = np.cos(a), np.sin(a); return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])


def personal_model(depth=1.3, width=1.1, length=0.93):
    m = GENERIC.copy()
    m[:, 0] *= width
    m[:, 1] *= length
    m[:, 2] *= depth
    return m


def synth(yaw_left=0.0, pitch_up=0.0, roll=0.0, smile=0.0, w=640, h=480, dist=1800.0,
          model=None, noise=0.0, seed=0):
    """Key points {'nose', 'chin', 'eye_l', 'eye_r', 'mouth_l', 'mouth_r'} of a face
    turned by the given degrees, in a ``w`` x ``h`` image. ``smile`` widens the mouth."""
    pts = (GENERIC if model is None else model).copy()
    pts[4, 0] *= (1 + smile)
    pts[5, 0] *= (1 + smile)
    rot = _rz(np.radians(roll)) @ _ry(np.radians(-yaw_left)) @ _rx(np.radians(-pitch_up))
    cam = (rot @ pts.T).T + np.array([0, 0, dist])
    img = np.stack([w * cam[:, 0] / cam[:, 2] + w / 2, w * cam[:, 1] / cam[:, 2] + h / 2], 1)
    if noise:
        img = img + np.random.default_rng(seed).normal(0, noise, img.shape)
    names = ['nose', 'chin', 'eye_l', 'eye_r', 'mouth_l', 'mouth_r']
    return {n: tuple(float(v) for v in p) for n, p in zip(names, img)}


def box(w=640, h=480, cx=0.0, cy=0.0, h_ratio=0.5):
    """(top, right, bottom, left) of a face box with the given offset/size."""
    bh = h_ratio * h
    bw = bh * 0.8
    mx, my = (0.5 + cx) * w, (0.5 + cy) * h
    return (my - bh / 2, mx + bw / 2, my + bh / 2, mx - bw / 2)


def landmarks_dict(points):
    """key points -> the dict shape returned by face_recognition.face_landmarks."""
    filler = points['nose']
    return {
        'chin': [filler] * 8 + [points['chin']] + [filler] * 8,
        'nose_bridge': [filler] * 3 + [points['nose']],
        'left_eye': [points['eye_l']] + [filler] * 5,
        'right_eye': [filler] * 3 + [points['eye_r']] + [filler] * 2,
        'top_lip': [points['mouth_l']] + [filler] * 5 + [points['mouth_r']] + [filler] * 5,
    }