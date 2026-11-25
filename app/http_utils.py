from flask import request

_JSON_PATH_SUFFIXES = ('/mark', '_stats', '/search_students', '/recognize_face')


def wants_json():
    """True when the caller is script code (fetch/XHR) that expects JSON
    rather than a browser navigation that expects an HTML page/redirect."""
    if request.is_json:
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    accept = request.accept_mimetypes
    if accept.best == 'application/json':
        return True
    path = request.path
    return path.startswith('/api/') or path.endswith(_JSON_PATH_SUFFIXES)
