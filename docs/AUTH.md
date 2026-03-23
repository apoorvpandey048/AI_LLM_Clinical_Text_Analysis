# SNAP-AI Authentication

## Overview

SNAP-AI uses JWT-based authentication with bcrypt password hashing.

| Feature | Implementation |
|---------|---------------|
| Password hashing | bcrypt via passlib |
| Token format | JWT (HS256) |
| Token lifetime | 24 hours (configurable) |
| User roles | `admin`, `doctor`, `pending` |
| Middleware | All `/api/v1/` routes protected |

## How It Works

```
1. User submits username + password → POST /api/v1/auth/login
2. Server verifies bcrypt hash → returns JWT token
3. Frontend stores token in localStorage
4. All API requests include: Authorization: Bearer <token>
5. Middleware validates token on each request
6. Token contains: user_id, username, role, expiry
```

## User Roles

| Role | Access |
|------|--------|
| `admin` | Full access — user management, operations dashboard, all endpoints |
| `doctor` | Pipeline access — upload cases, view results, manage prompts |
| `pending` | No access — waiting for admin approval after signup |

## Creating Users

### Default Admin (Automatic)

At startup, the backend creates a default admin user using the `ADMIN_PASSWORD` environment variable:

```bash
# In .env or docker-compose environment
ADMIN_PASSWORD=your-secure-password
```

### Admin Script (Manual)

For creating additional admins or resetting passwords:

```bash
# From the backend directory (inside container or with DB access)
python create_admin.py --username admin --password <secure-password> --name "Dr. Admin"
```

### User Signup

1. Users visit the login page and click "Sign Up"
2. Account is created with `pending` status
3. Admin navigates to Admin → User Management
4. Admin approves user and assigns role (`doctor` or `admin`)

### User Lifecycle Management

| Action | How | Notes |
|--------|-----|-------|
| **Approve** pending user | Admin panel → Approve button | Sets role to `doctor` |
| **Promote** to admin | Admin panel → Make Admin | Assigns `admin` role |
| **Demote** admin | Admin panel → Remove Admin | Reverts to `doctor` |
| **Deactivate** user | Admin panel → Deactivate | Blocks login, preserves data |
| **Reactivate** user | Admin panel → Reactivate | Restores login access |

> **Safety Rules:**
> - Cannot deactivate or change your own account
> - Cannot demote the last remaining admin

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/v1/auth/login` | Public | Login, returns JWT |
| POST | `/api/v1/auth/signup` | Public | Register new user |
| GET | `/api/v1/auth/me` | Required | Get current user profile |
| POST | `/api/v1/auth/logout` | Required | Logout (client-side) |
| GET | `/api/v1/auth/users` | Admin | List all users |
| POST | `/api/v1/auth/approve` | Admin | Approve pending user |
| POST | `/api/v1/auth/reject` | Admin | Deactivate user |
| PATCH | `/api/v1/auth/users/{id}/status` | Admin | Toggle active/inactive |
| PATCH | `/api/v1/auth/users/{id}/role` | Admin | Change role (admin ↔ doctor) |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `JWT_SECRET` | ✅ Yes | — | Secret key for JWT signing |
| `JWT_EXPIRATION_HOURS` | No | 24 | Token lifetime in hours |
| `ADMIN_PASSWORD` | ✅ Yes | — | Default admin password |

> **Security**: Generate JWT_SECRET with:
> ```bash
> python -c "import secrets; print(secrets.token_urlsafe(32))"
> ```

## Public Routes (No Auth Required)

- `/health` — Health check
- `/api/v1/auth/login` — Login
- `/api/v1/auth/signup` — Signup
- `/api/v1/info` — API info
- `/api/v1/stream/*` — SSE streaming (uses UUID as implicit auth)
- `/docs` — Swagger UI
