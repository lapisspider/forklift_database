"""Authentication: Google/Microsoft SSO, sessions, and role helpers.

Access is employee-only: a user may sign in only if their email matches
`allowed_email_domain` (when set). The first bootstrap admins are listed in
`admin_emails`; everyone else defaults to the 'viewer' role. The signed-in
user is kept in the session cookie as {email, name, role}.
"""
from __future__ import annotations

import secrets

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, get_db
from .models import User

# ---------------------------------------------------------------------------
# OAuth registry (only the configured providers are registered)
# ---------------------------------------------------------------------------
oauth = OAuth()
if settings.google_enabled:
    oauth.register(
        name="google",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )
if settings.microsoft_enabled:
    oauth.register(
        name="microsoft",
        client_id=settings.microsoft_client_id,
        client_secret=settings.microsoft_client_secret,
        server_metadata_url=(
            f"https://login.microsoftonline.com/{settings.microsoft_tenant}"
            "/v2.0/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )

router = APIRouter()

# Paths that never require a login.
PUBLIC_PREFIXES = ("/login", "/auth/", "/static/", "/health", "/dev-login", "/favicon")


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def current_user(request: Request) -> dict | None:
    """The signed-in user dict {email, name, role}, or None."""
    return request.session.get("user")


def require_user(request: Request) -> dict:
    """Dependency: 401 if not signed in (middleware normally redirects first)."""
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(request: Request) -> dict:
    """Dependency: 403 unless the signed-in user is an admin."""
    user = require_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admins only")
    return user


def _upsert_user(db: Session, email: str, name: str | None) -> User:
    """Find-or-create the user; assign admin role for bootstrap admins."""
    email = email.lower()
    user = db.query(User).filter(User.email == email).first()
    if not user:
        role = "admin" if email in settings.admin_email_set else "viewer"
        user = User(email=email, name=name, role=role)
        db.add(user)
        db.commit()
        db.refresh(user)
    elif name and user.name != name:
        user.name = name
        db.commit()
    return user


def _establish_session(request: Request, user: User) -> None:
    request.session["user"] = {"email": user.email, "name": user.name, "role": user.role}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    from .main import templates  # avoid circular import at module load
    return templates.TemplateResponse(
        request, "login.html",
        {
            "request": request,
            "google_enabled": settings.google_enabled,
            "microsoft_enabled": settings.microsoft_enabled,
            "password_enabled": settings.password_enabled,
            "dev_login": settings.dev_login,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/auth/password")
def auth_password(request: Request, password: str = Form(...),
                  db: Session = Depends(get_db)):
    """Shared-password sign-in. The admin password grants full access; the
    review/access password grants view-only. Used for preview/demo access."""
    def _login_as(email: str, name: str, role: str):
        user = _upsert_user(db, email, name)
        if user.role != role:
            user.role = role
            db.commit()
        _establish_session(request, user)
        return RedirectResponse("/", status_code=303)

    if settings.admin_password and secrets.compare_digest(password, settings.admin_password):
        return _login_as("admin@preview.local", "Admin", "admin")
    if settings.editor_password and secrets.compare_digest(password, settings.editor_password):
        return _login_as("editor@preview.local", "Editor", "editor")
    if settings.access_password and secrets.compare_digest(password, settings.access_password):
        return _login_as("reviewer@preview.local", "Reviewer", "viewer")
    return RedirectResponse("/login?error=Incorrect+password", status_code=303)


@router.get("/auth/{provider}/login")
async def auth_login(provider: str, request: Request):
    client = oauth.create_client(provider)
    if client is None:
        raise HTTPException(404, "Unknown or unconfigured provider")
    redirect_uri = request.url_for("auth_callback", provider=provider)
    return await client.authorize_redirect(request, str(redirect_uri))


@router.get("/auth/{provider}/callback", name="auth_callback")
async def auth_callback(provider: str, request: Request, db: Session = Depends(get_db)):
    client = oauth.create_client(provider)
    if client is None:
        raise HTTPException(404, "Unknown or unconfigured provider")
    try:
        token = await client.authorize_access_token(request)
    except Exception:  # noqa: BLE001
        return RedirectResponse("/login?error=Sign-in+failed", status_code=303)

    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    name = info.get("name")
    if not email:
        return RedirectResponse("/login?error=No+email+from+provider", status_code=303)
    if not settings.email_allowed(email):
        return RedirectResponse("/login?error=This+account+is+not+permitted", status_code=303)

    user = _upsert_user(db, email, name)
    _establish_session(request, user)
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return RedirectResponse("/login", status_code=303)


@router.get("/dev-login")
def dev_login(request: Request, role: str = "admin", db: Session = Depends(get_db)):
    """LOCAL TESTING ONLY: sign in as a fake user. Disabled unless dev_login=true."""
    if not settings.dev_login:
        raise HTTPException(404, "Not found")
    email = f"dev-{role}@example.com"
    user = db.query(User).filter(User.email == email).first()
    if not user:
        user = User(email=email, name=f"Dev {role.title()}", role=role)
        db.add(user)
    else:
        user.role = role
    db.commit()
    db.refresh(user)
    _establish_session(request, user)
    return RedirectResponse("/", status_code=303)
