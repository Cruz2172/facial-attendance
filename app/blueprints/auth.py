import re

from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from sqlalchemy import func, or_
from werkzeug.security import generate_password_hash, check_password_hash

from ..extensions import db, limiter
from ..models import User, InviteCode, Course, CourseEnrollment
from ..timeutil import utcnow

auth_bp = Blueprint('auth', __name__)

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

# Verified against when the e-mail is unknown, so "no such user" and "wrong
# password" take the same time (prevents account enumeration by timing).
_DUMMY_HASH = generate_password_hash('not-a-real-password')


def _normalise_email(value):
    return (value or '').strip().lower()


@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit(lambda: current_app.config['REGISTER_RATE_LIMIT'], methods=['POST'])
def register():
    if request.method == 'POST':
        try:
            fullname = (request.form.get('fullname') or '').strip()
            email = _normalise_email(request.form.get('email'))
            password = request.form.get('password')
            confirm_password = request.form.get('confirm_password')
            invite_code = (request.form.get('invite_code') or '').strip()

            if not all([fullname, email, password, confirm_password]):
                flash('All fields are required!', 'danger')
                return redirect(url_for('auth.register'))

            if password != confirm_password:
                flash('Passwords do not match!', 'danger')
                return redirect(url_for('auth.register'))

            min_len = current_app.config['MIN_PASSWORD_LENGTH']
            if len(password) < min_len:
                flash(f'Password must be at least {min_len} characters!', 'danger')
                return redirect(url_for('auth.register'))

            if len(password) > 128:
                flash('Password is too long (maximum 128 characters).', 'danger')
                return redirect(url_for('auth.register'))

            if not EMAIL_RE.match(email) or len(email) > 120:
                flash('Please enter a valid email address!', 'danger')
                return redirect(url_for('auth.register'))

            if len(fullname) > 120:
                flash('Full name is too long (maximum 120 characters).', 'danger')
                return redirect(url_for('auth.register'))

            if User.query.filter(func.lower(User.email) == email).first():
                flash('Email already registered!', 'danger')
                return redirect(url_for('auth.register'))

            role = 'student'
            is_approved = False
            invite_obj = None
            teacher_courses = []

            if invite_code:
                invite_obj = InviteCode.query.filter_by(code=invite_code).first()

                if not invite_obj:
                    flash('Invalid invite code! The code does not exist.', 'danger')
                    return redirect(url_for('auth.register'))

                if not invite_obj.is_valid():
                    if invite_obj.expires_at and utcnow() > invite_obj.expires_at:
                        flash('This invite code has expired!', 'danger')
                    elif invite_obj.used and invite_obj.max_uses != -1:
                        flash('This invite code has already been used!', 'danger')
                    elif invite_obj.max_uses != -1 and invite_obj.uses_count >= invite_obj.max_uses:
                        flash('This invite code has reached its maximum uses!', 'danger')
                    else:
                        flash('This invite code is no longer valid!', 'danger')
                    return redirect(url_for('auth.register'))

                if invite_obj.is_teacher_invite:
                    role = 'student'
                    is_approved = True

                    teacher = User.query.get(invite_obj.created_by)
                    if teacher:
                        teacher_courses = Course.query.filter_by(
                            teacher_id=teacher.id,
                            is_active=True
                        ).all()

                        if not teacher_courses:
                            flash('This teacher has no active courses at the moment.', 'warning')
                            return redirect(url_for('auth.register'))
                    else:
                        flash('Invalid invite code! Teacher not found.', 'danger')
                        return redirect(url_for('auth.register'))
                else:
                    role = invite_obj.role
                    is_approved = True if role in ('admin', 'teacher') else False

                # Claim one use ATOMICALLY. The old read-modify-write let two
                # simultaneous sign-ups both use a single-use invite. The
                # UPDATE only succeeds while uses remain, and it stays in the
                # same transaction as the user insert (rolled back together).
                claimed = InviteCode.query.filter(
                    InviteCode.id == invite_obj.id,
                    or_(InviteCode.max_uses == -1,
                        InviteCode.uses_count < InviteCode.max_uses),
                ).update({InviteCode.uses_count: InviteCode.uses_count + 1},
                         synchronize_session=False)
                if not claimed:
                    db.session.rollback()
                    flash('This invite code has reached its maximum uses!', 'danger')
                    return redirect(url_for('auth.register'))
                db.session.refresh(invite_obj)
                if invite_obj.max_uses != -1 and invite_obj.uses_count >= invite_obj.max_uses:
                    invite_obj.used = True

            new_user = User(
                fullname=fullname,
                email=email,
                password=generate_password_hash(password),
                role=role,
                is_approved=is_approved,
                invite_code=invite_code if invite_code else None,
                created_at=utcnow()
            )
            db.session.add(new_user)
            db.session.commit()

            if invite_obj and invite_obj.is_teacher_invite:
                invite_obj.used_by = new_user.id
                db.session.commit()

            if teacher_courses:
                enrolled_count = 0
                for course in teacher_courses:
                    try:
                        existing_enrollment = CourseEnrollment.query.filter_by(
                            student_id=new_user.id,
                            course_id=course.id
                        ).first()

                        if not existing_enrollment:
                            enrollment = CourseEnrollment(
                                student_id=new_user.id,
                                course_id=course.id,
                                enrolled_at=utcnow(),
                                status='active'
                            )
                            db.session.add(enrollment)
                            enrolled_count += 1
                    except Exception:
                        current_app.logger.exception(f"Error enrolling student in course {course.id}")

                db.session.commit()

                if enrolled_count > 0:
                    flash(f'✅ Registration successful! You have been automatically enrolled in {enrolled_count} course(s).', 'success')
                else:
                    flash('✅ Registration successful!', 'success')

            elif is_approved:
                flash(f'✅ Registration successful! You can login as {role}.', 'success')
            else:
                flash('✅ Registration successful! Your account is pending admin approval.', 'info')

            return redirect(url_for('auth.login'))

        except Exception:
            db.session.rollback()
            current_app.logger.exception("Registration error")
            flash('Registration error. Please try again.', 'danger')
            return redirect(url_for('auth.register'))

    invite_code = request.args.get('code', '')
    return render_template('register.html', invite_code=invite_code)


def _login_account_key():
    return 'acct:' + _normalise_email(request.form.get('email'))


@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit(lambda: current_app.config['LOGIN_RATE_LIMIT'], methods=['POST'])
@limiter.limit(lambda: current_app.config['LOGIN_ACCOUNT_RATE_LIMIT'], key_func=_login_account_key,
               methods=['POST'])
def login():
    if request.method == 'POST':
        try:
            email = _normalise_email(request.form.get('email'))
            password = request.form.get('password') or ''

            if not email or not password:
                flash('Email and password are required!', 'danger')
                return redirect(url_for('auth.login'))

            # func.lower keeps accounts created before e-mails were normalised working.
            user = User.query.filter(func.lower(User.email) == email).first()
            password_ok = check_password_hash(user.password if user else _DUMMY_HASH, password)

            if user and password_ok:
                if not user.is_approved:
                    flash('Your account is pending approval. Please contact an administrator.', 'warning')
                    return redirect(url_for('auth.login'))

                session.clear()          # drop any pre-login session data (session fixation)
                session.permanent = True
                session['user_id'] = user.id
                session['user_name'] = user.fullname
                session['user_email'] = user.email
                session['user_role'] = user.role

                flash('Login successful!', 'success')
                return redirect(url_for('main.dashboard'))

            flash('Invalid email or password!', 'danger')
            return redirect(url_for('auth.login'))

        except Exception:
            current_app.logger.exception("Login error")
            flash('Login error. Please try again.', 'danger')
            return redirect(url_for('auth.login'))

    return render_template('login.html')


@auth_bp.route('/logout')
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('main.home'))