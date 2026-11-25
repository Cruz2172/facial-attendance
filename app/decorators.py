from functools import wraps

from flask import session, flash, redirect, url_for, abort, g, jsonify

from .extensions import db
from .http_utils import wants_json
from .models import User


def _reject(message, category='warning', status=401):
    """Respond to a failed auth check in the way the caller can handle:
    JSON for fetch/XHR (so the scan page can redirect cleanly), a flash +
    redirect for normal page loads."""
    if wants_json():
        return jsonify({'success': False, 'message': message,
                        'login_required': status == 401}), status
    flash(message, category)
    return redirect(url_for('auth.login'))


def _check_auth(roles=None):
    """Return an error response, or None when the request is allowed.

    Always re-reads the user from the database so a deleted, deactivated or
    demoted account loses access immediately, and refreshes the role kept in
    the session (templates/routes read session['user_role']).
    """
    uid = session.get('user_id')
    if not uid:
        return _reject('Please login first')

    user = getattr(g, 'current_user', None)
    if user is None or user.id != uid:
        user = db.session.get(User, uid)

    if not user:
        session.clear()
        return _reject('Your account no longer exists. Please login again.')

    if not user.is_approved:
        session.clear()
        return _reject('Your account has been deactivated. Please contact an administrator.')

    if session.get('user_role') != user.role:
        session['user_role'] = user.role
    g.current_user = user

    if roles and user.role not in roles:
        if wants_json():
            return jsonify({'success': False, 'message': 'You do not have permission to do that.'}), 403
        abort(403)
    return None


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        denied = _check_auth()
        if denied is not None:
            return denied
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        denied = _check_auth(roles=('admin',))
        if denied is not None:
            return denied
        return f(*args, **kwargs)
    return decorated_function


def teacher_or_admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        denied = _check_auth(roles=('admin', 'teacher'))
        if denied is not None:
            return denied
        return f(*args, **kwargs)
    return decorated_function
