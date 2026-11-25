#!/usr/bin/env python3

import argparse
import secrets

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

# Load .env before creating the Flask app
load_dotenv()

from app import create_app
from app.timeutil import utcnow
from app.extensions import db
from app.models import User


def create_or_promote_admin(email, fullname, password):
    """Create a new admin or promote an existing user."""

    user = User.query.filter_by(email=email).first()

    if user:
        print(f"\nUser already exists: {email}")
        print(f"Current role: {user.role}")
        print(f"Approved: {user.is_approved}")

        user.role = "admin"
        user.is_approved = True

        if password:
            user.password = generate_password_hash(password)
            print("Password has been updated.")

        db.session.commit()

        print("\n" + "=" * 60)
        print("ADMIN USER UPDATED")
        print("=" * 60)
        print(f"Name:     {user.fullname}")
        print(f"Email:    {user.email}")
        print("Role:     admin")
        print("Approved: True")
        print("=" * 60)
        return

    if not password:
        password = secrets.token_urlsafe(9)
        generated = True
    else:
        generated = False

    user = User(
        fullname=fullname or "Admin",
        email=email,
        password=generate_password_hash(password),
        role="admin",
        is_approved=True,
        created_at=utcnow()
    )

    db.session.add(user)
    db.session.commit()

    print("\n" + "=" * 60)
    print("ADMIN USER CREATED")
    print("=" * 60)
    print(f"Name:     {user.fullname}")
    print(f"Email:    {user.email}")
    print("Role:     admin")
    print("Approved: True")

    if generated:
        print(f"Password: {password}")
        print("SAVE THIS PASSWORD NOW.")

    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Create or promote a FaceAttend admin user."
    )

    parser.add_argument("--email", help="Admin email address")
    parser.add_argument("--fullname", help="Admin full name")
    parser.add_argument("--password", help="Admin password")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompts"
    )

    args = parser.parse_args()

    app = create_app()

    database_uri = app.config.get("SQLALCHEMY_DATABASE_URI")

    print("=" * 60)
    print("FaceAttend Admin Creator")
    print("=" * 60)
    print(f"Database: {database_uri}")
    print("=" * 60)

    if not args.yes:
        confirm = input(
            "Continue with this database? (y/n): "
        ).strip().lower()

        if confirm != "y":
            print("Aborted.")
            return

    with app.app_context():

        # Make sure tables exist
        db.create_all()

        email = args.email
        fullname = args.fullname
        password = args.password

        if not email:
            email = input("Admin Email: ").strip()

        if not fullname:
            fullname = input("Admin Full Name: ").strip()

        if not password:
            password = input(
                "Admin Password (leave blank for random): "
            ).strip()

        if password and len(password) < 6:
            print("Password must be at least 6 characters.")
            return

        create_or_promote_admin(
            email=email,
            fullname=fullname,
            password=password
        )


if __name__ == "__main__":
    main()