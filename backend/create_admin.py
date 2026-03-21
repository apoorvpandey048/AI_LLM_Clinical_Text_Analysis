#!/usr/bin/env python3
"""
Create an admin user for SNAP-AI.

Usage:
    python create_admin.py --username admin --password <password> --name "SNAP-AI Administrator"

This script connects to the database and creates (or updates) an admin user
with a bcrypt-hashed password. Safe to run multiple times — if the user
already exists, it updates the password and ensures the role is ADMIN.

Requires the same environment variables as the backend:
    POSTGRES_HOST, POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD
"""

import argparse
import os
import sys

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "."))

from passlib.context import CryptContext
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, User, UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def get_database_url():
    """Build database URL from environment variables."""
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "snapai")
    user = os.environ.get("POSTGRES_USER", "snapai")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def create_admin(username: str, password: str, name: str):
    """Create or update an admin user."""
    engine = create_engine(get_database_url())
    Base.metadata.create_all(bind=engine, checkfirst=True)
    Session = sessionmaker(bind=engine)
    db = Session()

    try:
        existing = db.query(User).filter(User.username == username).first()
        password_hash = pwd_context.hash(password)

        if existing:
            existing.password_hash = password_hash
            existing.role = UserRole.ADMIN
            existing.is_active = True
            existing.name = name
            db.commit()
            print(f"✓ Updated existing user '{username}' → role=admin")
        else:
            user = User(
                username=username,
                name=name,
                password_hash=password_hash,
                role=UserRole.ADMIN,
                is_active=True,
            )
            db.add(user)
            db.commit()
            print(f"✓ Created admin user '{username}'")
    except Exception as e:
        db.rollback()
        print(f"✗ Failed to create admin: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description="Create a SNAP-AI admin user")
    parser.add_argument("--username", required=True, help="Admin username")
    parser.add_argument("--password", required=True, help="Admin password")
    parser.add_argument("--name", default="SNAP-AI Administrator", help="Display name")
    args = parser.parse_args()

    if len(args.password) < 6:
        print("✗ Password must be at least 6 characters", file=sys.stderr)
        sys.exit(1)

    create_admin(args.username, args.password, args.name)


if __name__ == "__main__":
    main()
