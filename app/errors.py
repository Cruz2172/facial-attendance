from flask import flash, redirect, url_for, jsonify, render_template, request
from flask_wtf.csrf import CSRFError
from werkzeug.exceptions import RequestEntityTooLarge

from .extensions import db
from .http_utils import wants_json


def register_error_handlers(app):

    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        if wants_json():
            return jsonify({'success': False, 'csrf': True,
                            'message': 'Your session expired. Please refresh the page and try again.'}), 400
        flash('Your session expired or the form was invalid. Please try again.', 'danger')
        return redirect(request.referrer or url_for('auth.login'))

    @app.errorhandler(RequestEntityTooLarge)
    def handle_large_request(e):
        return jsonify({'success': False, 'message': 'Uploaded image is too large.'}), 413

    @app.errorhandler(429)
    def handle_rate_limited(e):
        message = 'Too many requests. Please wait a moment and try again.'
        if wants_json():
            return jsonify({'success': False, 'message': message, 'rate_limited': True}), 429
        # A 3xx redirect must not carry a 429 status (browsers would not
        # follow it and show a blank page), so flash + normal redirect.
        flash(message, 'warning')
        return redirect(request.referrer or url_for('main.home'))

    @app.errorhandler(403)
    def handle_forbidden(e):
        if wants_json():
            return jsonify({'success': False, 'message': 'You do not have permission to do that.'}), 403
        return render_template('403.html'), 403

    @app.errorhandler(404)
    def handle_not_found(e):
        if wants_json():
            return jsonify({'success': False, 'message': 'Not found.'}), 404
        return render_template('404.html'), 404

    @app.errorhandler(500)
    def handle_internal_error(e):
        app.logger.error('500 Error on %s %s', request.method, request.path,
                         exc_info=getattr(e, 'original_exception', e))
        try:
            db.session.rollback()       # never leave a broken transaction behind
        except Exception:
            pass
        if wants_json():
            return jsonify({'success': False,
                            'message': 'A server error occurred. Please try again.'}), 500
        # Self-contained page: no templates, no database access and no
        # redirect, so a failure in the home page can never cause a loop.
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Something went wrong</title></head>'
            '<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem;text-align:center">'
            '<h1>Something went wrong</h1>'
            '<p>An unexpected error occurred. Please try again in a moment.</p>'
            '<p><a href="/">Back to home</a></p></body></html>',
            500,
        )
