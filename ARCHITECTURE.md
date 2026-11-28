# Face Attendance System: Code Blueprint

A map of the corrected project: where everything lives, what each part is responsible for, how data flows, and where to make changes. Section 7 (module signatures) and Section 8 (routes) are generated from the code itself.

---

## 1. Project layout

```
face-attendance/
├── wsgi.py                    gunicorn entry point: app = create_app()
├── run.py                     CLI: `python run.py init-db` | dev server (never for production)
├── gunicorn.conf.py           workers / threads / timeouts
├── Dockerfile  Procfile  .dockerignore  .env.example
├── requirements.txt  requirements-dev.txt
│
├── app/
│   ├── __init__.py            create_app(): wires everything together
│   ├── config.py              Config (all settings from env) + validate_config()
│   ├── extensions.py          db, csrf, limiter, cors, user_or_ip()
│   ├── models.py              SQLAlchemy tables
│   ├── bootstrap.py           init_database(): tables, indexes, admin account, CLI command
│   │
│   ├── decorators.py          login_required / admin_required / teacher_or_admin_required
│   ├── errors.py              JSON-or-HTML error handlers (400/403/404/413/429/500)
│   ├── http_utils.py          wants_json(): "is the caller a script or a browser?"
│   ├── context_processors.py  variables injected into every template
│   ├── audit.py               audit(): record sensitive actions
│   ├── timeutil.py            UTC storage, local-timezone display helpers
│   │
│   ├── face_core.py           PURE LOGIC (no Flask/DB): image decode, face selection,
│   │                          multi-template matching, concurrency gates
│   ├── pose_core.py           PURE LOGIC: head-pose / position / smile measurements and
│   │                          the rule for each guided pose
│   ├── face_cache.py          in-memory face index shared safely between workers
│   ├── face_matching.py       identify_student_for_course()
│   ├── utils.py               batch queries, report rows, duplicate-face check, active session
│   │
│   ├── services/
│   │   └── enrollment.py      multi-photo enrollment rules and helpers
│   │
│   ├── blueprints/            URL routes, grouped by audience
│   │   ├── auth.py            register / login / logout
│   │   ├── main.py            home, dashboard redirect
│   │   ├── student.py         dashboard, profile, enroll, course browsing, history
│   │   ├── teacher.py         approvals, invites, student management
│   │   ├── admin.py           users, invites, deletions, reports
│   │   ├── courses.py         courses, sessions, camera scan, manual override, reports
│   │   ├── api.py             JSON endpoints for charts/widgets
│   │   └── legacy.py          old pages; /recognize_face retired (410)
│   │
│   ├── templates/             Jinja pages (enroll.html, mark_attendance.html, courses/, student/, admin/ ...)
│   └── static/js/
│       ├── multi_capture.js   automatic, server-verified guided capture (no capture button)
│       └── script.js          shared page scripts
│
└── tests/
    ├── test_face_core.py          matching, parsing, image handling   (no DB)
    ├── test_pose_core.py          head-pose rules with synthetic faces (no DB)
    ├── pose_helpers.py            builds synthetic faces / fake dlib landmarks for tests
    ├── test_enrollment_service.py enrollment rules, fake dlib         (no DB)
    ├── test_http_layer.py         auth responses, headers, errors     (no DB)
    ├── test_app_integration.py    scan / enroll / manual / cache flows (SQLite)
    └── js/test_multi_capture.js   capture widget in a fake DOM (Node)
```

---

## 2. Layers and dependencies

Each layer depends only on the ones below it. `face_core.py` depends on nothing in the app, so it can be tested alone.

```
 Browser (HTTPS)
     │
 nginx / platform proxy ───────────────  TLS, client_max_body_size, X-Forwarded-*
     │
 gunicorn workers (gthread) ───────────  each worker = its own process + memory
     │
 ┌── Flask app (create_app) ──────────────────────────────────────────────┐
 │  middleware : ProxyFix → CSRF → rate limiter → security headers        │
 │  guards     : decorators.py  (session → DB user → role check)          │
 │                                                                         │
 │  blueprints/*.py   ← routes: parse request, check permissions, respond  │
 │        │                                                                │
 │        ├─ services/enrollment.py   enrollment rules                     │
 │        ├─ face_matching.py         who is this face?                    │
 │        └─ utils.py                 queries / reports                    │
 │                 │                                                       │
 │          face_cache.py  ◄── version counters ──►  app_state table       │
 │                 │                                                       │
 │          face_core.py   (numpy, OpenCV; dlib loaded lazily)            │
 └─────────────────────────────────────────────────────────────────────────┘
     │
 PostgreSQL (SQLite for development only)
```

---

## 3. Data model

Face data lives in one column, `students.face_encoding`: JSON text holding **a list of templates** (one 128-number encoding per enrollment photo). The legacy format (a single flat list of 128 numbers) is still read.

| Table | Purpose | Key columns |
|---|---|---|
| `users` | every account | `email` (unique), `password` (hash), `role` (student / teacher / admin), `is_approved` |
| `students` | face enrollment of a student user | `user_id` (unique, FK users), `student_id` (unique), `face_encoding` (JSON templates), `face_image` (optional file name) |
| `courses` | a class | `course_code` (unique), `teacher_id` (FK users), `is_active` |
| `course_enrollments` | who is in which course | `student_id` (FK users), `course_id`, `status` |
| `course_enrollment_requests` | pending join requests | `student_id`, `course_id`, `status`, `reviewed_by` |
| `course_enrollment_tokens` | join links/codes | `token_code` (unique), `max_uses`, `used_count`, `expires_at` |
| `attendance_sessions` | one class meeting | `course_id`, `session_date` (local), `status` (active / closed), `session_token`. **Unique index: one active session per course.** |
| `course_attendance_records` | who was present | `session_id`, `student_id` (FK users), `face_confidence`, `remarks`. **Unique (session, student).** |
| `invite_codes` | registration invites | `code`, `role`, `max_uses`, `uses_count`, `expires_at` |
| `attendance` | legacy table (no longer written) | kept so old data and foreign keys still work |
| `app_state` | shared counters | `faces_version`, `roster_version`: tells every worker when to reload its cache |
| `audit_log` | trail of sensitive actions | `user_id`, `action`, `target_type`, `target_id`, `details`, `created_at` |

Relationships that matter: `User 1—1 Student`, `User 1—n CourseEnrollment n—1 Course`, `Course 1—n AttendanceSession 1—n CourseAttendanceRecord n—1 User`.

---

## 4. Core flows

### 4a. Request pipeline (every request)
```
request → ProxyFix (real client IP) → CSRF check (POST) → rate limit
        → route → @login_required
                   ├ no session / user deleted / deactivated → 401 JSON (scripts) or redirect to /login (browsers)
                   ├ refresh session role from the database
                   └ role check → 403
        → handler → response + security headers (nosniff, frame, permissions-policy, no-store)
unhandled exception → errors.py → JSON for scripts, self-contained HTML page for browsers (never a redirect)
```

### 4b. Enrollment (multi-photo, poses enforced)
```
enroll page: multi_capture.js  (NO capture button)
  every ~450 ms:  small frame ──► POST /enroll/pose-check {image, pose_index, baseline}
      server: decode → one face? → landmarks → measure head yaw/pitch/roll, position, smile
              → evaluate_pose(pose_index)  ──►  {ok, code, message, baseline?}
      page shows the message ("Turn a little more to your left"), oval turns green when ok
  photo is taken automatically after POSE_HOLD_COUNT consecutive ok replies (≈1.3 s steady)
  order: straight → left → right → up → smile (→ closer → down); all are required
  hidden field face_images = JSON array of the approved frames, in pose order

POST /enroll
  ├ rate limit (per user) → login → role must be student
  ├ parse_face_images → validate_enrollment_form   fields, consent box, photo count (all required)
  ├ check_not_already_enrolled                      (a half-created row from the old bug is reused)
  ├ encode_enrollment_images        ONE 'enroll' gate slot for the whole batch
  │     pass 1, per photo: exactly one clear face → 128-number encoding
  │     check_capture_consistency   every photo must be the SAME person (blames the odd one out)
  │     pass 2, per photo: re-measure the head pose on the server
  │     _verify_poses               photo 1 straight; photos 2.. judged against photo 1
  │                                 (slightly looser than the live check: POSE_FINAL_SLACK_DEG)
  ├ reject_if_duplicate   any template matching another enrolled PERSON → refuse (no personal data shown)
  ├ serialize_encodings → students.face_encoding
  ├ audit('student.face_enroll') → commit → invalidate_face_cache()
  └ optional: enroll into a course from an invite code
```

**How a pose is judged** (`pose_core.py`): two landmark ratios are measured along the eye line (nose offset / eye distance for left-right turn; nose position between eyes and chin for up-down), plus mouth width / eye distance for the smile and the face box for position and distance. Every pose after the first is compared with *that student's own* straight photo, because ratio changes are far more consistent between different faces than absolute angles. The numbers are "equivalent degrees", good enough to tell a real turn from a fake one, not a protractor.

| Pose | Rule (defaults) |
|---|---|
| straight | facing the camera (within 10°), head level, face centred in the oval, right size |
| left / right | turned 12–38° the correct way compared with photo 1, chin level |
| up / down | chin moved 8–30° the correct way, not turned sideways |
| smile | facing the camera and mouth width at least 8 % wider than in photo 1 |
| closer | facing the camera, face at least 12 % bigger than in photo 1 |

### 4c. Attendance scan (camera on the teacher's device)
```
mark_attendance.html: every ~2 s capture a frame (≤640 px) → POST /courses/session/<id>/mark
  ├ rate limit (per user) → login → teacher/admin → must own the course (or be admin)
  ├ session must be active AND dated today (app timezone)  → else {closed: true}
  ├ roster has face-enrolled students? (cheap check BEFORE any face work)
  ├ decode → gate slot (busy → HTTP 503 {busy: true}, page retries)
  ├ detect faces → select_primary_face
  │     no_face / too_small → quiet hint      multiple → ask for one at a time
  │     one clearly dominant face is used even if people queue behind it
  ├ encode → identify_student_for_course
  │     person_distances: closest template per PERSON
  │     best > threshold → unknown      runner-up person too close → ambiguous
  │     winner not in this course's roster → not_enrolled
  ├ INSERT record  (unique constraint turns a double-scan race into "already marked")
  └ JSON {success, student_name, already_marked, confidence, time}
Teacher override: /manual-mark and /manual-unmark (reason saved in remarks + audit_log)
```

### 4d. Keeping every worker's face data in sync
```
change (enroll / delete / roster edit) → commit → invalidate_*()  → UPDATE app_state SET value = value + 1
each worker, at most once per second: read the counters
    faces_version changed  → reload (matrix, starts, records) from the students table
    roster_version changed → drop cached course rosters
cache holds plain tuples + a numpy matrix, never SQLAlchemy objects (those expire after a commit and crash later)
```

Cache shape: `matrix` stacks every template of every person; `starts[i]` is the row where person *i* begins; `records[i]` describes person *i*.

### 4e. Decision outcomes of a scan
| Outcome | Meaning | Where decided |
|---|---|---|
| `no_face` / `too_small` / `multiple` | frame not usable | `face_core.select_primary_face` |
| `unknown` | closest person farther than `CONFIDENCE_THRESHOLD` | `face_core.identify_people` |
| `ambiguous` | next different person within `MATCH_MARGIN` | `face_core.identify_people` |
| `not_enrolled` | recognised, but not in this course | `face_matching.identify_student_for_course` |
| `busy` | gate full (HTTP 503) | `face_core.FaceGate` |
| `closed` | session ended or from an earlier day | `courses.mark_course_attendance` |
| success / `already_marked` | recorded or duplicate | `courses.mark_course_attendance` |

---

## 5. Configuration map (environment variables)

| Group | Variables |
|---|---|
| Required in production | `SECRET_KEY`, `DATABASE_URL` |
| Environment | `APP_ENV`, `APP_TIMEZONE`, `LOG_LEVEL`, `TRUSTED_PROXY_COUNT`, `SESSION_COOKIE_SECURE`, `SESSION_LIFETIME_HOURS` |
| Admin bootstrap | `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `SEED_DEMO_DATA` |
| Capacity | `WEB_CONCURRENCY`, `GUNICORN_THREADS`, `FACE_MAX_CONCURRENT`, `FACE_QUEUE_TIMEOUT`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` |
| Recognition | `CONFIDENCE_THRESHOLD`, `DUPLICATE_THRESHOLD`, `MATCH_MARGIN`, `FACE_MIN_SIZE_PX`, `FACE_PRIMARY_RATIO`, `FACE_DETECT_MAX_DIM`, `FACE_SCAN_JITTERS` |
| Multi-photo enrollment | `ENROLL_CAPTURE_COUNT`, `ENROLL_MIN_IMAGES` (default = count: all poses required), `ENROLL_MAX_IMAGES`, `ENROLL_CONSISTENCY_MAX`, `FACE_ENROLL_MAX_DIM`, `FACE_ENROLL_JITTERS` |
| Guided poses | `POSE_HOLD_COUNT`, `POSE_CHECK_INTERVAL_MS`, `POSE_CHECK_MAX_DIM`, `POSE_FINAL_SLACK_DEG`, `POSE_STRAIGHT_MAX_YAW`, `POSE_TURN_MIN`, `POSE_TURN_MAX`, `POSE_PITCH_MIN`, `POSE_PITCH_MAX`, `POSE_SMILE_INCREASE`, `POSE_CHECK_RATE_LIMIT`, `ENROLL_MAX_CONCURRENT`, `POSE_QUEUE_TIMEOUT`, `POSE_MIN_FACE_PX` |
| Limits | `LOGIN_RATE_LIMIT`, `LOGIN_ACCOUNT_RATE_LIMIT`, `REGISTER_RATE_LIMIT`, `ENROLL_RATE_LIMIT`, `SCAN_RATE_LIMIT`, `RATELIMIT_STORAGE_URI` |
| Privacy / storage | `STORE_FACE_IMAGES`, `FACES_DIR`, `CORS_ORIGINS` |

---

## 6. Where to change what

| I want to… | Change this |
|---|---|
| make matching stricter or looser | `CONFIDENCE_THRESHOLD`, `MATCH_MARGIN` (tune with real data from the `Scan course=… best_distance=…` log lines) |
| change the number or kind of enrollment poses | `ENROLL_CAPTURE_COUNT`; pose texts are the `POSES` array in `multi_capture.js`; the rule for each pose is in `pose_core.evaluate_pose` (keep `POSE_KEYS` and `POSES` in the same order) |
| make poses easier or stricter | `POSE_TURN_MIN/MAX`, `POSE_PITCH_MIN/MAX`, `POSE_SMILE_INCREASE`, `POSE_HOLD_COUNT` (tune with real students) |
| add **liveness detection** | in `courses.mark_course_attendance`, between `select_primary_face` and `encode_face`; reject spoofs before any encoding |
| let students add more photos later | new route in `student.py` reusing `encode_enrollment_images`, then append to the stored template list |
| use another face model (e.g. a different embedding) | `face_core.detect_faces` / `encode_face` and `ENCODING_LEN`; everything else works on plain arrays |
| support 100k+ faces | replace the brute-force `person_distances` with an index (FAISS / pgvector); the rest of the pipeline is unchanged |
| add a new role or permission | `decorators._check_auth(roles=...)` and the blueprint's guard |
| record another sensitive action | call `audit('action.name', 'target_type', target_id, 'details')` before `commit()` |
| change the timezone | `APP_TIMEZONE` (storage stays UTC) |
| add a table or column | edit `models.py`, then re-run `python run.py init-db` (new tables only; adopt Flask-Migrate before altering existing columns) |

---

## 7. Module signatures (generated from the code)

### `app/__init__.py`

- `create_app(config_class=Config)`

### `app/audit.py`

- `audit(action, target_type=None, target_id=None, details=None)` — Record a sensitive action.

### `app/blueprints/admin.py`

- `delete_student(student_id)`  **route** `'/admin/delete-student/<int:student_id>', methods=['POST']` — Delete a student record (admin only) — allows re-enrollment.
- `admin_dashboard()`  **route** `'/admin/dashboard'`
- `manage_users()`  **route** `'/admin/users'`
- `approve_user(user_id)`  **route** `'/admin/approve_user/<int:user_id>', methods=['POST']`
- `reject_user(user_id)`  **route** `'/admin/reject_user/<int:user_id>', methods=['POST']`
- `change_user_role(user_id)`  **route** `'/admin/change_role/<int:user_id>', methods=['POST']`
- `toggle_approval(user_id)`  **route** `'/admin/toggle_approval/<int:user_id>', methods=['POST']`
- `delete_user(user_id)`  **route** `'/admin/delete_user/<int:user_id>', methods=['POST']`
- `manage_invites()`  **route** `'/admin/invites'`
- `generate_invite()`  **route** `'/admin/generate_invite', methods=['GET', 'POST']`
- `delete_invite(invite_id)`  **route** `'/admin/delete_invite/<int:invite_id>', methods=['POST']`
- `check_invite()`  **route** `'/admin/check_invite'` — Public (no login) lightweight JSON check used by the registration
- `list_courses()`  **route** `'/admin/courses'`
- `search_students_page()`  **route** `'/admin/students/search'` — Renders the interactive search page. All data loading, filtering,
- `attendance_report()`  **route** `'/admin/attendance-report'`

### `app/blueprints/api.py`

- `api_student_attendance_stats()`  **route** `'/api/student/attendance-stats'`
- `api_active_sessions(course_id)`  **route** `'/api/courses/<int:course_id>/active-sessions'`
- `api_session_details(session_id)`  **route** `'/api/sessions/<int:session_id>'`

### `app/blueprints/auth.py`

- `register()`  **route** `'/register', methods=['GET', 'POST']`
- `login()`  **route** `'/login', methods=['GET', 'POST']`
- `logout()`  **route** `'/logout'`

### `app/blueprints/courses.py`

- `list_courses()`  **route** `'/courses'`
- `create_course()`  **route** `'/courses/create', methods=['GET', 'POST']`
- `view_course(course_id)`  **route** `'/courses/<int:course_id>'`
- `start_attendance_session(course_id)`  **route** `'/courses/<int:course_id>/start-session', methods=['GET', 'POST']`
- `view_session(session_id)`  **route** `'/courses/session/<int:session_id>'`
- `end_attendance_session(session_id)`  **route** `'/courses/session/<int:session_id>/end', methods=['POST']`
- `mark_course_attendance(session_id)`  **route** `'/courses/session/<int:session_id>/mark', methods=['POST']` — Runs on the teacher/admin's device at the front of the room. Each
- `manual_mark_attendance(session_id)`  **route** `'/courses/session/<int:session_id>/manual-mark', methods=['POST']` — Teacher override for students the camera cannot recognise (poor light,
- `manual_unmark_attendance(session_id)`  **route** `'/courses/session/<int:session_id>/manual-unmark', methods=['POST']` — Remove a mistaken attendance record (e.g. wrong person was scanned).
- `enroll_via_link(token_code)`  **route** `'/courses/enroll/<token_code>'`
- `enroll_with_token()`  **route** `'/courses/enroll', methods=['GET', 'POST']`
- `attendance_report()`  **route** `'/attendance-report'` — Every enrolled, face-registered student's attendance rate,
- `print_attendance_report(course_id)`  **route** `'/attendance-report/print/<int:course_id>'` — Clean, printable per-course attendance report.

### `app/blueprints/legacy.py`

- `mark_attendance()`  **route** `'/mark_attendance'`
- `recognize_face()`  **route** `'/recognize_face', methods=['POST']` — DISABLED. This old global (not tied to a class session) endpoint let any
- `attendance_stats()`  **route** `'/attendance_stats'`
- `get_students()`  **route** `'/students'` — List students. Admins see everyone; teachers only see students
- `search_students()`  **route** `'/search_students'` — Search students with role-based access control.

### `app/blueprints/main.py`

- `home()`  **route** `'/'`
- `dashboard()`  **route** `'/dashboard'`

### `app/blueprints/student.py`

- `student_dashboard()`  **route** `'/student/dashboard'`
- `student_courses()`  **route** `'/student/courses'`
- `student_course_detail(course_id)`  **route** `'/student/course/<int:course_id>'`
- `student_attendance_history()`  **route** `'/student/attendance-history'`
- `student_profile()`  **route** `'/student/profile', methods=['GET', 'POST']`
- `student_change_password()`  **route** `'/student/change-password', methods=['POST']`
- `student_attendance_stats()`  **route** `'/student/attendance-stats'`
- `enroll_pose_check()`  **route** `'/enroll/pose-check', methods=['POST']` — Live guidance for the guided capture page.
- `enroll()`  **route** `'/enroll', methods=['GET', 'POST']`
- `browse_courses()`  **route** `'/courses/browse'`
- `request_course_enrollment(course_id)`  **route** `'/courses/request-enrollment/<int:course_id>', methods=['POST']`

### `app/blueprints/teacher.py`

- `teacher_dashboard()`  **route** `'/teacher/dashboard'`
- `manage_enrollment_requests()`  **route** `'/teacher/enrollment-requests'`
- `approve_enrollment_request(request_id)`  **route** `'/teacher/enrollment-requests/<int:request_id>/approve', methods=['POST']`
- `reject_enrollment_request(request_id)`  **route** `'/teacher/enrollment-requests/<int:request_id>/reject', methods=['POST']`
- `teacher_delete_student(student_id)`  **route** `'/teacher/delete-student/<int:student_id>', methods=['POST']` — Teacher can delete students from their own courses' rosters; admin
- `generate_student_invite()`  **route** `'/teacher/generate-student-invite', methods=['GET', 'POST']` — Generate a single invite link that enrolls students in ALL of the
- `teacher_invite_status()`  **route** `'/teacher/invite-status'`
- `generate_course_token_teacher()`  **route** `'/teacher/generate-token', methods=['POST']`
- `manage_links()`  **route** `'/teacher/manage-links', methods=['GET']`

### `app/bootstrap.py`
_Database bootstrap, run ONCE per deployment (not on every worker start)._

- `init_database(app, log=print)` — Create tables, indexes, counters and the admin account.
- `register_cli(app)`

### `app/config.py`

- `class Config()`
- `validate_config(app)` — Fail fast with a clear message instead of misbehaving in production.

### `app/context_processors.py`

- `register_context_processors(app)`

### `app/create_admin.py`

- `create_or_promote_admin(email, fullname, password)` — Create a new admin or promote an existing user.
- `main()`

### `app/decorators.py`

- `login_required(f)`
- `admin_required(f)`
- `teacher_or_admin_required(f)`

### `app/errors.py`

- `register_error_handlers(app)`

### `app/extensions.py`

- `user_or_ip()` — Rate-limit key: the logged-in user if any, otherwise the client IP.

### `app/face_cache.py`
_In-memory face index shared safely between web workers._

- `class _FaceState()`
- `invalidate_face_cache()` — Face data changed (enroll / delete). Also invalidates rosters.
- `invalidate_course_roster_cache(course_id=None)` — Course membership changed. (course_id kept for API compatibility.)
- `ensure_state_rows()` — Create the counter rows (called from init-db). Safe to run repeatedly.
- `get_state()` — Return an up-to-date _FaceState, reloading from the DB if needed.
- `get_known_faces()` — (list of encodings, list of FaceRecord): one encoding per person (their
- `get_roster_ids(course_id)` — frozenset of Student primary keys enrolled (active) in the course.
- `get_course_roster(course_id)` — (list of encodings, list of FaceRecord) for the course roster.
- `reset_for_tests()`

### `app/face_core.py`
_Face-processing building blocks with NO Flask / database dependency._

- `class FaceInputError(ValueError)` — The image sent by the client is unusable. The message is user-safe.
- `class FaceBusy(RuntimeError)` — Too many face jobs are running on this worker right now.
- `downscale(img, max_dim)` — Shrink so the longest side is at most max_dim (never enlarges).
- `decode_data_url(data_url, max_pixels=25000000, max_dim=None)` — Decode a ``data:image/...;base64,...`` string.
- `detect_faces(img_rgb, upsample=1, model='hog')` — Return face boxes as (top, right, bottom, left).
- `landmarks_for_box(img_rgb, box)` — dlib 68-point landmarks of the face in ``box`` as face_recognition's dict
- `select_primary_face(boxes, min_size=60, ratio=1.6)` — Pick the face to identify.
- `encode_face(img_rgb, box, jitters=1)` — 128-d encoding (float64 ndarray) of the face in ``box``.
- `class FaceGate()`
    - `slot(self, timeout=8.0)`
- `get_gate(limit, name='default')` — One gate per NAME per process. Attendance scans use 'default';
- `parse_encodings(raw)` — JSON text -> float64 array of shape (k, 128), or None if nothing usable.
- `parse_encoding(raw)` — First template only (kept for backwards compatibility).
- `serialize_encodings(encodings)` — List/array of 128-d encodings -> JSON text for the database.
- `check_capture_consistency(encodings, max_distance)` — Make sure every enrollment photo shows the SAME person.
- `face_distances(matrix, encoding)`
- `person_distances(matrix, starts, encoding)` — Distance from ``encoding`` to each PERSON (the closest of that person's
- `identify_people(matrix, starts, encoding, threshold, margin)` — 1:N identification over multi-template people.
- `find_duplicate_person(matrix, starts, encoding, threshold, margin)` — Index of an already-enrolled PERSON this encoding unambiguously matches,
- `identify(matrix, encoding, threshold, margin)` — Single-template-per-person form (kept for compatibility and tests).
- `find_duplicate(matrix, encoding, threshold, margin)` — Single-template-per-person form (kept for compatibility and tests).

### `app/face_matching.py`
_Strict 1:N face identification for attendance scans._

- `identify_face(face_encoding, known_encodings, known_students, threshold=None, margin=None)` — Compatibility wrapper. Return (student, best_distance, status).
- `identify_student_for_course(face_encoding, course_id)` — Return (record, status, distance).

### `app/http_utils.py`

- `wants_json()` — True when the caller is script code (fetch/XHR) that expects JSON

### `app/models.py`

- `class User(db.Model)`
    - `to_dict(self)`
- `class Student(db.Model)`
    - `get_encodings(self)`
    - `get_encoding(self)`
    - `to_dict(self)`
- `class AttendanceRecord(db.Model)` — Legacy global (non-course-scoped) attendance table. Kept only for
    - `to_dict(self)`
- `class InviteCode(db.Model)`
    - `is_valid(self)`
- `class Course(db.Model)`
    - `to_dict(self)`
    - `get_enrolled_count(self)`
    - `get_pending_requests_count(self)`
    - `get_teacher_name(self)`
- `class CourseEnrollment(db.Model)`
    - `to_dict(self)`
- `class CourseEnrollmentRequest(db.Model)`
    - `to_dict(self)`
- `class AttendanceSession(db.Model)`
    - `to_dict(self)`
    - `get_present_count(self)`
    - `get_total_enrolled(self)`
- `class CourseAttendanceRecord(db.Model)`
    - `to_dict(self)`
- `class CourseEnrollmentToken(db.Model)`
    - `is_valid(self)`
    - `get_enrollment_link(self)`
    - `to_dict(self)`
- `class AppState(db.Model)` — Tiny key/value counters shared by every worker process.
- `class AuditLog(db.Model)` — Append-only trail of sensitive actions (manual attendance changes,

### `app/pose_core.py`
_Head pose, position and expression rules for GUIDED enrollment._

- `class Tuning()` — Every threshold in one place (angles are equivalent degrees).
    - `with_slack(self, degrees)`
- `key_points(landmarks)` — face_recognition landmark dict -> the six points we need, or None.
- `measure(points, box, width, height)` — Everything the rules need, or None if the landmarks are unusable.
- `baseline_from(metrics)` — The numbers later poses are compared with (from the straight photo).
- `clean_baseline(raw)` — Validate a baseline received from a browser. Returns a dict or None.
- `yaw_left_deg(metrics, baseline=None)` — Equivalent degrees turned to the person's left (relative to the baseline if given).
- `pitch_up_deg(metrics, baseline)` — Equivalent degrees the chin is raised compared with the baseline.
- `evaluate_pose(index, m, baseline, tuning=None)` — Judge measurement ``m`` against the rule for pose ``index``.

### `app/services/enrollment.py`

- `class EnrollmentError(Exception)` — Raised for any expected, user-facing enrollment failure (bad
- `parse_face_images(form)` — Photos posted by the enrollment page: a JSON array in ``face_images``
- `validate_enrollment_form(student_id, fullname, email, course, level, face_images, consent=False)`
- `check_not_already_enrolled(user_id, student_id, email)` — Raise if this account/ID/email is already enrolled.
- `decode_and_normalize_image(face_data)`
- `detect_single_face(img_rgb)` — Return the 128-d encoding of the ONE face in a single photo.
- `pose_tuning(slack=0.0)` — Pose thresholds from configuration (optionally loosened for the final re-check).
- `check_pose_frame(data_url, pose_index, baseline_raw)` — Live guidance for ONE camera frame.
- `encode_enrollment_images(face_images)` — Turn the captured photos into face templates.
- `reject_if_duplicate(face_encodings, user_id=None)` — Refuse enrollment if ANY of the new templates unambiguously matches a
- `save_face_image(img_bgr, student_id)` — Optionally keep the enrollment photo (STORE_FACE_IMAGES=true).
- `face_image_path(stored_value)` — Absolute path of a stored photo. Understands both the new relative file
- `delete_face_image(stored_value)`

### `app/timeutil.py`
_Timezone helpers._

- `get_tz(name=None)`
- `utcnow()` — Naive UTC 'now' (what the database columns store).
- `now_local(tz_name=None)` — Naive datetime in the app's local timezone.
- `today_local(tz_name=None)`
- `to_local(dt, tz_name=None)` — Convert a naive-UTC datetime (as stored) to a naive local datetime.
- `format_local(dt, fmt='%Y-%m-%d %H:%M', tz_name=None)`

### `app/utils.py`

- `query_students_by_user_ids(user_ids)`
- `count_students_by_user_ids(user_ids)`
- `query_users_by_ids(user_ids)`
- `count_attendance_records_by_student_and_sessions(student_id, session_ids)`
- `get_active_session_for_today(course_id)` — Returns the course's active session if dated today (app timezone),
- `check_face_duplicate(face_encoding, threshold=None, margin=None)` — Return the FaceRecord of an already-enrolled person this face
- `decode_image_from_base64(data_url, max_dim=None)` — Decode a base64 data-URL into (img_bgr, img_rgb).
- `build_course_attendance_rows(course)` — Shared by /attendance-report and the printable per-course view so


---

## 8. Routes (generated from the running app)

**app** (2)

| Path | Methods | Handler |
|---|---|---|
| `/healthz` | GET | `healthz` |
| `/readyz` | GET | `readyz` |

**auth** (3)

| Path | Methods | Handler |
|---|---|---|
| `/login` | GET,POST | `login` |
| `/logout` | GET | `logout` |
| `/register` | GET,POST | `register` |

**main** (2)

| Path | Methods | Handler |
|---|---|---|
| `/` | GET | `home` |
| `/dashboard` | GET | `dashboard` |

**student** (11)

| Path | Methods | Handler |
|---|---|---|
| `/courses/browse` | GET | `browse_courses` |
| `/courses/request-enrollment/<int:course_id>` | POST | `request_course_enrollment` |
| `/enroll` | GET,POST | `enroll` |
| `/enroll/pose-check` | POST | `enroll_pose_check` |
| `/student/attendance-history` | GET | `student_attendance_history` |
| `/student/attendance-stats` | GET | `student_attendance_stats` |
| `/student/change-password` | POST | `student_change_password` |
| `/student/course/<int:course_id>` | GET | `student_course_detail` |
| `/student/courses` | GET | `student_courses` |
| `/student/dashboard` | GET | `student_dashboard` |
| `/student/profile` | GET,POST | `student_profile` |

**teacher** (9)

| Path | Methods | Handler |
|---|---|---|
| `/teacher/dashboard` | GET | `teacher_dashboard` |
| `/teacher/delete-student/<int:student_id>` | POST | `teacher_delete_student` |
| `/teacher/enrollment-requests` | GET | `manage_enrollment_requests` |
| `/teacher/enrollment-requests/<int:request_id>/approve` | POST | `approve_enrollment_request` |
| `/teacher/enrollment-requests/<int:request_id>/reject` | POST | `reject_enrollment_request` |
| `/teacher/generate-student-invite` | GET,POST | `generate_student_invite` |
| `/teacher/generate-token` | POST | `generate_course_token_teacher` |
| `/teacher/invite-status` | GET | `teacher_invite_status` |
| `/teacher/manage-links` | GET | `manage_links` |

**courses** (13)

| Path | Methods | Handler |
|---|---|---|
| `/attendance-report` | GET | `attendance_report` |
| `/attendance-report/print/<int:course_id>` | GET | `print_attendance_report` |
| `/courses` | GET | `list_courses` |
| `/courses/<int:course_id>` | GET | `view_course` |
| `/courses/<int:course_id>/start-session` | GET,POST | `start_attendance_session` |
| `/courses/create` | GET,POST | `create_course` |
| `/courses/enroll` | GET,POST | `enroll_with_token` |
| `/courses/enroll/<token_code>` | GET | `enroll_via_link` |
| `/courses/session/<int:session_id>` | GET | `view_session` |
| `/courses/session/<int:session_id>/end` | POST | `end_attendance_session` |
| `/courses/session/<int:session_id>/manual-mark` | POST | `manual_mark_attendance` |
| `/courses/session/<int:session_id>/manual-unmark` | POST | `manual_unmark_attendance` |
| `/courses/session/<int:session_id>/mark` | POST | `mark_course_attendance` |

**admin** (15)

| Path | Methods | Handler |
|---|---|---|
| `/admin/approve_user/<int:user_id>` | POST | `approve_user` |
| `/admin/attendance-report` | GET | `attendance_report` |
| `/admin/change_role/<int:user_id>` | POST | `change_user_role` |
| `/admin/check_invite` | GET | `check_invite` |
| `/admin/courses` | GET | `list_courses` |
| `/admin/dashboard` | GET | `admin_dashboard` |
| `/admin/delete-student/<int:student_id>` | POST | `delete_student` |
| `/admin/delete_invite/<int:invite_id>` | POST | `delete_invite` |
| `/admin/delete_user/<int:user_id>` | POST | `delete_user` |
| `/admin/generate_invite` | GET,POST | `generate_invite` |
| `/admin/invites` | GET | `manage_invites` |
| `/admin/reject_user/<int:user_id>` | POST | `reject_user` |
| `/admin/students/search` | GET | `search_students_page` |
| `/admin/toggle_approval/<int:user_id>` | POST | `toggle_approval` |
| `/admin/users` | GET | `manage_users` |

**api** (3)

| Path | Methods | Handler |
|---|---|---|
| `/api/courses/<int:course_id>/active-sessions` | GET | `api_active_sessions` |
| `/api/sessions/<int:session_id>` | GET | `api_session_details` |
| `/api/student/attendance-stats` | GET | `api_student_attendance_stats` |

**legacy** (5)

| Path | Methods | Handler |
|---|---|---|
| `/attendance_stats` | GET | `attendance_stats` |
| `/mark_attendance` | GET | `mark_attendance` |
| `/recognize_face` | POST | `recognize_face` |
| `/search_students` | GET | `search_students` |
| `/students` | GET | `get_students` |


---

## 9. Capture widget outline (`static/js/multi_capture.js`)

```
init()                       runs only if form#enrollForm and video#enrollVideo exist
 ├ state: photos[N] (null = empty slot), baseline, okStreak, inFlight, requestSeq, stream
 ├ the capture button is hidden and disabled: there is no manual capture
 ├ start camera              mirrored preview, guide oval + live message over the video, then the loop
 ├ tick() loop (every POSE_CHECK_INTERVAL_MS; paused while the tab is hidden or during the cooldown)
 │    draw frame → brightness check (too dark / too bright never reaches the server)
 │    POST /enroll/pose-check {image, pose_index = first empty slot, baseline}
 │    handleResult:  401 → stop + redirect · busy/429 → wait · not ok → show the server's message, reset the hold
 │                   ok → hold counter; after POSE_HOLD_COUNT in a row → acceptPhoto (the SAME frame that was checked)
 │    8 s timeout per request; replies from abandoned requests (stop / retake / reset / timeout) are ignored
 ├ acceptPhoto               slot 0 needs the baseline from the server; then a 1.4 s cooldown before the next pose
 ├ retake(i)                 clears one slot; retaking photo 1 clears all (the rest are measured against it)
 └ submit                    all poses + every field required, then the button locks
output: input#face_images = JSON array of data URLs (in pose order)
```