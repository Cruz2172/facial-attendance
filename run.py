"""Command line entry point.

    python run.py init-db     create/upgrade tables + admin account (run once per deploy)
    python run.py             DEVELOPMENT server only (never use this in production)

Production uses gunicorn:   gunicorn -c gunicorn.conf.py wsgi:app
"""
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from app import create_app        
from app.bootstrap import init_database  

app = create_app()


def init_db():
    return init_database(app)


if __name__ == '__main__':
    command = sys.argv[1] if len(sys.argv) > 1 else 'serve'

    if command == 'init-db':
        sys.exit(0 if init_db() else 1)

    if command != 'serve':
        print(f"Unknown command '{command}'. Use: init-db | serve")
        sys.exit(2)

    if app.config['IS_PRODUCTION']:
        print('WARNING: this is the Flask development server. In production run:\n'
              '    gunicorn -c gunicorn.conf.py wsgi:app')
    else:
        init_db()   # convenience for local development

    # The interactive debugger allows remote code execution, so it is only
    # ever enabled explicitly (FLASK_DEBUG=1), never in production, and the
    # server listens on localhost unless HOST says otherwise.
    debug = os.environ.get('FLASK_DEBUG') == '1' and not app.config['IS_PRODUCTION']
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', 5001))
    print(f'Starting Face Attendance System at http://{host}:{port} (debug={debug})')
    app.run(debug=debug, host=host, port=port)
