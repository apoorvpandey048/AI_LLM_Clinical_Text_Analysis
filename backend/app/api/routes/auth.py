"""
Authentication API routes.

Handles user signup, login, JWT tokens, and admin user management.
Uses username + password with admin approval system.
"""

import uuid
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings
from app.db import get_db, User
from app.db.models import UserRole
from app.utils import get_logger

logger = get_logger(__name__)
settings = get_settings()

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])

# Password hashing
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_response(success: bool, data=None, error=None, request_id=None):
    """Create standardized response."""
    return {
        "success": success,
        "data": data,
        "error": error,
        "meta": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id,
        },
    }


# ============================================
# Schemas
# ============================================

class SignupRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    password: str = Field(..., min_length=6)


class LoginRequest(BaseModel):
    username: str
    password: str


class ApproveUserRequest(BaseModel):
    user_id: str
    role: str = "doctor"


# ============================================
# JWT Token Helpers
# ============================================

def create_access_token(user_id: str, username: str, role: str) -> str:
    """Create a JWT access token."""
    expire = datetime.utcnow() + timedelta(hours=settings.jwt_expiration_hours)
    to_encode = {
        "sub": user_id,
        "username": username,
        "role": role,
        "exp": expire,
    }
    return jwt.encode(to_encode, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def verify_token(token: str) -> dict | None:
    """Verify and decode a JWT token."""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return payload
    except JWTError:
        return None


def get_current_user_from_request(request: Request, db: Session) -> User | None:
    """Extract and verify the current user from request headers."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.split(" ", 1)[1]
    payload = verify_token(token)
    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    try:
        user = db.query(User).filter(User.id == uuid.UUID(user_id)).first()
        return user
    except Exception:
        return None


# ============================================
# Auth dependency for protected routes
# ============================================

def require_auth(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency that requires valid authentication."""
    user = get_current_user_from_request(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")
    if user.role == UserRole.PENDING:
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency that requires admin role."""
    user = require_auth(request, db)
    if user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ============================================
# Routes
# ============================================

@router.post("/signup")
async def signup(body: SignupRequest, request: Request, db: Session = Depends(get_db)):
    """
    Register a new user. Account starts in 'pending' status until admin approves.
    """
    request_id = getattr(request.state, "request_id", None)

    # Check if username already exists
    existing = db.query(User).filter(User.username == body.username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already taken")

    # Hash password
    password_hash = pwd_context.hash(body.password)

    # Create user (pending approval)
    user = User(
        username=body.username,
        name=body.name,
        password_hash=password_hash,
        role=UserRole.PENDING,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    logger.info("user_registered", username=body.username, user_id=str(user.id), request_id=request_id)

    return create_response(
        success=True,
        data={
            "message": "Account created. Waiting for admin approval.",
            "user": user.to_dict(),
        },
        request_id=request_id,
    )


@router.post("/login")
async def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)):
    """
    Login with username and password. Returns JWT token.
    """
    request_id = getattr(request.state, "request_id", None)

    user = db.query(User).filter(User.username == body.username).first()
    if not user or not pwd_context.verify(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    if user.role == UserRole.PENDING:
        raise HTTPException(status_code=403, detail="Account pending admin approval. Please contact the administrator.")

    # Update last login
    user.last_login = datetime.utcnow()
    db.commit()

    # Create JWT token
    token = create_access_token(str(user.id), user.username, user.role.value)

    logger.info("user_logged_in", username=body.username, user_id=str(user.id), request_id=request_id)

    return create_response(
        success=True,
        data={
            "token": token,
            "user": user.to_dict(),
        },
        request_id=request_id,
    )


@router.get("/me")
async def get_me(request: Request, user: User = Depends(require_auth)):
    """Get current user profile."""
    request_id = getattr(request.state, "request_id", None)
    return create_response(
        success=True,
        data={"user": user.to_dict()},
        request_id=request_id,
    )


@router.post("/logout")
async def logout(request: Request):
    """
    Logout (client should discard token).
    Server-side: just acknowledge.
    """
    request_id = getattr(request.state, "request_id", None)
    return create_response(
        success=True,
        data={"message": "Logged out successfully"},
        request_id=request_id,
    )


# ============================================
# Admin Routes
# ============================================

@router.get("/users")
async def list_users(
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """List all users (admin only)."""
    request_id = getattr(request.state, "request_id", None)
    users = db.query(User).order_by(User.created_at.desc()).all()
    return create_response(
        success=True,
        data={"users": [u.to_dict() for u in users]},
        request_id=request_id,
    )


@router.post("/approve")
async def approve_user(
    body: ApproveUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Approve a pending user (admin only)."""
    request_id = getattr(request.state, "request_id", None)

    try:
        user_uuid = uuid.UUID(body.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user = db.query(User).filter(User.id == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Set role
    try:
        role = UserRole(body.role)
    except ValueError:
        role = UserRole.DOCTOR

    user.role = role
    db.commit()

    logger.info("user_approved", user_id=body.user_id, role=role.value, admin=admin.username, request_id=request_id)

    return create_response(
        success=True,
        data={
            "message": f"User {user.username} approved as {role.value}",
            "user": user.to_dict(),
        },
        request_id=request_id,
    )


@router.post("/reject")
async def reject_user(
    body: ApproveUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Deactivate/reject a user (admin only)."""
    request_id = getattr(request.state, "request_id", None)

    try:
        user_uuid = uuid.UUID(body.user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid user ID")

    user = db.query(User).filter(User.id == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False
    db.commit()

    logger.info("user_rejected", user_id=body.user_id, admin=admin.username, request_id=request_id)

    return create_response(
        success=True,
        data={"message": f"User {user.username} deactivated"},
        request_id=request_id,
    )
