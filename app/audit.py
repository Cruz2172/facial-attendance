from flask import current_app, session

from .extensions import db
from .models import AuditLog


def audit(action, target_type=None, target_id=None, details=None):
    """Record a sensitive action.

    The entry is added to the CURRENT database transaction, so it is saved
    (or rolled back) together with the change it describes - call
    ``db.session.commit()`` afterwards as usual. It is also written to the
    application log so it survives even if the database is later pruned.
    """
    user_id = session.get('user_id')
    target_id = None if target_id is None else str(target_id)[:60]
    details = (details or '')[:2000]
    current_app.logger.info('AUDIT action=%s user=%s target=%s:%s %s',
                            action, user_id, target_type, target_id, details)
    try:
        db.session.add(AuditLog(
            user_id=user_id, action=action[:60],
            target_type=(target_type or None), target_id=target_id, details=details,
        ))
    except Exception:
        current_app.logger.exception('Could not queue audit entry')
