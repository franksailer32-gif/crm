from datetime import timedelta
from typing import Any, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status, Response, Cookie, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.api import deps
from app.core import security
from app.core.i18n import translator
from app.core.config import settings
from app.core.limiter import limiter
from app.core.redis import redis_client
from app.services.verification_service import (
    generate_verification_token,
    store_pending_registration,
    get_pending_registration,
    delete_pending_registration,
)
from app.services.email_service import send_verification_email, send_password_reset_email
from jose import jwt
from pydantic import ValidationError
from app.crud.crud_audit_log import write_audit_log
from app.services.geoip_service import resolve_ip_location

router = APIRouter()

@router.post("/track-visit")
async def track_visit(
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    current_user: Optional[models.User] = Depends(deps.get_current_user_optional),
) -> Any:
    """
    Tracks a visit to the site. 
    Captures IP, location data, and user (if logged in).
    """
    ip = request.client.host if request.client else "unknown"
    
    # Resolve location
    geo_data = await resolve_ip_location(ip)
    
    # Log visit
    await write_audit_log(
        db,
        action="site.visit",
        actor=current_user,
        details={
            "path": request.query_params.get("path", "/"),
            "user_agent": request.headers.get("user-agent"),
            "geo": geo_data
        },
        ip_address=ip
    )
    await db.commit()
    return {"status": "ok"}

@router.post("/register")
@limiter.limit("10/hour")
async def register_user(
    *,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    reg_in: schemas.RegistrationRequest,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Step 1 of Registration: Validates data, stores securely in Redis,
    and sends a verification email.
    """
    user = await crud.user.get_by_email(db, email=reg_in.user.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail=translator.t("email_exists_system", lang=lang),
        )
    
    # If starting as a company, check slug
    if reg_in.org:
        existing_org = await crud.org.get_by_slug(db, slug=reg_in.org.slug)
        if existing_org:
            raise HTTPException(
                status_code=400,
                detail=translator.t("org_slug_exists", lang=lang),
            )
            
    # Generate token and store in Redis
    token = generate_verification_token()
    payload = reg_in.model_dump(mode="json")
    await store_pending_registration(token, payload)
    
    # Send email
    await send_verification_email(reg_in.user.email, token)
    
    return {"message": translator.t("verification_email_sent", lang=lang)}


@router.get("/verify-email")
async def verify_email(
    *,
    response: Response,
    db: AsyncSession = Depends(deps.get_db),
    token: str,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Step 2 of Registration: Validates the token, creates the user in the DB,
    and returns an access token so they are immediately logged in.
    """
    payload = await get_pending_registration(token)
    if not payload:
        raise HTTPException(
            status_code=400, 
            detail=translator.t("invalid_token", lang=lang)
        )
        
    # 1. Handle Email Change for existing user
    if payload.get("type") == "email_change":
        user_id = payload.get("user_id")
        new_email = payload.get("new_email")
        if not user_id or not new_email:
             raise HTTPException(status_code=400, detail=translator.t("invalid_token_content", lang=lang))
        
        user = await crud.user.get(db, id=UUID(user_id))
        if not user:
             raise HTTPException(status_code=404, detail=translator.t("user_not_found", lang=lang))
        
        # Check if email was taken in the meantime
        email_taken = await crud.user.get_by_email(db, email=new_email)
        if email_taken:
             raise HTTPException(status_code=400, detail=translator.t("email_already_used", lang=lang))
        
        # Update user
        await crud.user.update(db, db_obj=user, obj_in={"email": new_email, "is_email_verified": True})
        
        # Clean up token
        await delete_pending_registration(token)
        
        return {"message": translator.t("email_updated", lang=lang), "user_id": str(user.id)}

    # 2. Handle New Registration (Default Flow)
    # Reconstruct the Pydantic model to ensure data is still valid
    reg_data = schemas.RegistrationRequest(**payload)
    
    # Check email again just in case it was taken while token was pending
    existing_user = await crud.user.get_by_email(db, email=reg_data.user.email)
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail=translator.t("user_already_created", lang=lang),
        )
        
    # Determine user identity and role
    org_id = None
    role = models.UserRole.rep
    user_type = models.UserType.solo
    organization = None

    if reg_data.org:
        # Create Organization first
        organization = await crud.org.create(db, obj_in=reg_data.org)
        org_id = organization.id
        role = models.UserRole.org_admin
        user_type = models.UserType.company_member
    
    # Consolidate user data for creation
    db_user_data = reg_data.user.model_dump()
    db_user_data.update({
        "org_id": org_id,
        "role": role,
        "user_type": user_type,
        "is_email_verified": True
    })

    # Create user
    new_user = await crud.user.create(db, obj_in=schemas.UserCreate(**db_user_data))
    
    # Update org owner if applicable
    if reg_data.org:
        await crud.org.update(db, db_obj=organization, obj_in={"owner_id": new_user.id})
        
    # Clean up token
    await delete_pending_registration(token)
    
    # Generate login token for seamless UX
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    import uuid
    jti = str(uuid.uuid4())
    token_data = {
        "role": new_user.role.value,
        "org_id": str(new_user.org_id) if new_user.org_id else None,
        "jti": jti
    }
    access_token = security.create_access_token(
        new_user.id, expires_delta=access_token_expires, extra_claims=token_data
    )
    refresh_token = security.create_refresh_token(
        new_user.id, expires_delta=timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES),
        extra_claims={"jti": jti}
    )
    
    await redis_client.setex(
        f"session:{new_user.id}",
        int(timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES).total_seconds()),
        jti
    )
    
    # Set cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=security.create_refresh_token(new_user.id),
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    )
    
    return {
        "message": translator.t("verified_logged_in", lang=lang),
        "user_id": str(new_user.id)
    }


@router.post("/login/access-token")
@limiter.limit("5/minute")
async def login_access_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(deps.get_db), 
    form_data: OAuth2PasswordRequestForm = Depends(),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    OAuth2 compatible token login, get an access token for future requests.
    """
    user = await crud.user.authenticate(
        db, email=form_data.username, password=form_data.password
    )
    if not user:
        raise HTTPException(status_code=400, detail=translator.t("invalid_credentials", lang=lang))
    
    # Block company_member users from standard login
    if getattr(user, "user_type", None) == models.UserType.company_member:
        raise HTTPException(
            status_code=403, 
            detail=translator.t("org_login_required", lang=lang)
        )
        
    if not user.is_active:
        raise HTTPException(status_code=400, detail=translator.t("inactive_user", lang=lang))
    elif not user.is_email_verified:
        raise HTTPException(status_code=400, detail=translator.t("verify_email_first", lang=lang))
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)
    
    # Create token with extra claims for role and org_id
    import uuid
    jti = str(uuid.uuid4())
    token_data = {
        "role": user.role.value,
        "org_id": str(user.org_id) if user.org_id else None,
        "jti": jti
    }
    
    access_token = security.create_access_token(
        user.id, expires_delta=access_token_expires, extra_claims=token_data
    )
    refresh_token = security.create_refresh_token(
        user.id, expires_delta=refresh_token_expires, extra_claims={"jti": jti}
    )
    
    # Store session info in Redis (for fast lookups or invalidation)
    await redis_client.setex(
        f"session:{user.id}",
        int(refresh_token_expires.total_seconds()), # session lasts as long as refresh token
        jti
    )
    
    # Set cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    )

    return {
        "message": translator.t("login_success", lang=lang),
        "user_type": getattr(user, "user_type", "solo")
    }

@router.post("/login/org")
@limiter.limit("5/minute")
async def login_org_token(
    request: Request,
    response: Response,
    login_in: schemas.OrgLoginRequest,
    db: AsyncSession = Depends(deps.get_db), 
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Organization-specific login for company members.
    Requires email, password, and the organization slug.
    """
    user = await crud.user.authenticate(
        db, email=login_in.email, password=login_in.password
    )
    if not user:
        raise HTTPException(status_code=400, detail=translator.t("invalid_credentials", lang=lang))
    if getattr(user, "user_type", None) != models.UserType.company_member:
        raise HTTPException(
            status_code=403, 
            detail=translator.t("org_members_only", lang=lang)
        )

    if not user.org_id:
         raise HTTPException(status_code=403, detail=translator.t("no_org_assigned", lang=lang))

    org = await crud.org.get(db, id=user.org_id)
    requested_slug = login_in.org_slug.strip().lower()
    actual_slug = org.slug.strip().lower() if org and org.slug else None
    if not org or actual_slug != requested_slug:
        raise HTTPException(
            status_code=403, 
            detail=translator.t("org_access_denied", lang=lang)
        )

    if not user.is_active:
        raise HTTPException(status_code=400, detail=translator.t("inactive_user", lang=lang))
    elif not user.is_email_verified:
        raise HTTPException(status_code=400, detail=translator.t("verify_email_first", lang=lang))
    
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)
    
    import uuid
    jti = str(uuid.uuid4())
    token_data = {
        "role": user.role.value if hasattr(user.role, "value") else user.role,
        "org_id": str(user.org_id),
        "jti": jti
    }
    
    access_token = security.create_access_token(
        user.id, expires_delta=access_token_expires, extra_claims=token_data
    )
    refresh_token = security.create_refresh_token(
        user.id, expires_delta=refresh_token_expires, extra_claims={"jti": jti}
    )
    
    await redis_client.setex(
        f"session:{user.id}",
        int(refresh_token_expires.total_seconds()),
        jti
    )
    
    # Set cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,  # Set to True in production
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,  # Set to True in production
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    )

    return {
        "message": translator.t("login_success", lang=lang),
        "org_id": str(user.org_id)
    }

@router.post("/accept-invite")
async def accept_invite(
    *,
    response: Response,
    db: AsyncSession = Depends(deps.get_db),
    accept_in: schemas.UserInviteAccept,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Accept an invitation to join an organization.
    """
    # Verify token from Redis
    payload = await get_pending_registration(accept_in.token)
    if not payload:
        raise HTTPException(
            status_code=400, 
            detail=translator.t("invalid_invite_token", lang=lang)
        )
    
    email = payload.get("email")
    org_id = payload.get("org_id")
    role = payload.get("role", models.UserRole.rep.value)

    if not email or not org_id:
        raise HTTPException(status_code=400, detail=translator.t("invalid_token_content", lang=lang))
        
    # Professional Validation: Ensure the email in the request matches the invite
    if accept_in.email != email:
        raise HTTPException(
            status_code=400,
            detail=translator.t("email_mismatch_invite", lang=lang)
        )
        
    existing_user = await crud.user.get_by_email(db, email=email)
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail=translator.t("email_exists", lang=lang),
        )

    import uuid
    # Create the user linked to the organization
    user_data = schemas.UserCreate(
        email=email,
        password=accept_in.password,
        full_name=accept_in.full_name,
        role=models.UserRole(role),
        user_type=models.UserType.company_member,
        org_id=uuid.UUID(org_id),
        is_email_verified=True
    )
    new_user = await crud.user.create(db, obj_in=user_data)
    
    # Generate tokens for immediate login
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)
    
    import uuid
    jti = str(uuid.uuid4())
    token_claims = {
        "role": new_user.role.value if hasattr(new_user.role, "value") else new_user.role,
        "org_id": str(new_user.org_id),
        "jti": jti
    }
    
    access_token = security.create_access_token(
        new_user.id, expires_delta=access_token_expires, extra_claims=token_claims
    )
    refresh_token = security.create_refresh_token(
        new_user.id, expires_delta=refresh_token_expires
    )
    
    # Track session in Redis
    await redis_client.setex(
        f"session:{new_user.id}",
        int(refresh_token_expires.total_seconds()),
        jti
    )
    
    # Set cookies
    response.set_cookie(
        key="access_token",
        value=access_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    )
    
    # Clean up token
    await delete_pending_registration(accept_in.token)
    
    return {"message": translator.t("invite_accepted_success", lang=lang)}


@router.post("/refresh")
async def refresh_token(
    response: Response,
    db: AsyncSession = Depends(deps.get_db),
    refresh_token: Optional[str] = Cookie(None),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Exchange a valid refresh token for a new access token and refresh token.
    """
    if not refresh_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=translator.t("could_not_validate_credentials", lang=lang),
        )

    try:
        payload = jwt.decode(
            refresh_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        if payload.get("type") != "refresh":
            raise HTTPException(status_code=400, detail=translator.t("invalid_token_type", lang=lang))
        token_data = schemas.TokenPayload(**payload)
    except (jwt.JWTError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=translator.t("could_not_validate_credentials", lang=lang),
        )
        
    user = await crud.user.get(db, id=token_data.sub)
    if not user:
        raise HTTPException(status_code=400, detail=translator.t("inactive_user", lang=lang))

    # Check if a session exists in redis (handles remote logouts and single-session enforcement)
    active_jti = await redis_client.get(f"session:{user.id}")
    if not active_jti or active_jti != token_data.jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=translator.t("session_expired", lang=lang),
        )

    # Issue new tokens
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_token_expires = timedelta(minutes=settings.REFRESH_TOKEN_EXPIRE_MINUTES)
    
    import uuid
    jti = str(uuid.uuid4())
    new_token_data = {
        "role": user.role.value,
        "org_id": str(user.org_id) if user.org_id else None,
        "jti": jti
    }
    new_access_token = security.create_access_token(
        user.id, expires_delta=access_token_expires, extra_claims=new_token_data
    )
    new_refresh_token = security.create_refresh_token(
        user.id, expires_delta=refresh_token_expires
    )
    
    # Extend session in Redis
    await redis_client.setex(
        f"session:{user.id}",
        int(refresh_token_expires.total_seconds()),
        jti
    )
    
    # Set new cookies
    response.set_cookie(
        key="access_token",
        value=new_access_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    response.set_cookie(
        key="refresh_token",
        value=new_refresh_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.REFRESH_TOKEN_EXPIRE_MINUTES * 60,
    )
    
    return {"message": translator.t("token_refreshed", lang=lang)}

@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Logout by deleting the session from Redis and clearing cookies.
    """
    token = request.cookies.get("access_token")
    if token:
        try:
            # Decode without verification of expiration to get the sub even if expired
            payload = jwt.decode(
                token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM],
                options={"verify_exp": False}
            )
            user_id = payload.get("sub")
            jti = payload.get("jti")
            if user_id and jti:
                # ONLY delete if this token matches the CURRENT session in Redis.
                # If they don't match, it means another login already superseded this one.
                active_jti = await redis_client.get(f"session:{user_id}")
                if active_jti == jti:
                    await redis_client.delete(f"session:{user_id}")
        except Exception:
            pass # Ignore malformed tokens during logout
            
    response.delete_cookie("access_token")
    response.delete_cookie("refresh_token")
    return {"message": translator.t("logout_success", lang=lang)}

@router.post("/forgot-password")
@limiter.limit("2/hour")
async def forgot_password(
    *,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
    password_req: schemas.ForgotPassword,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Password Recovery. Generates a token and sends an email.
    """
    user = await crud.user.get_by_email(db, email=password_req.email)
    if not user:
        return {"message": translator.t("password_reset_sent", lang=lang)}
    
    token = generate_verification_token()
    await redis_client.setex(f"password_reset:{token}", 900, str(user.id)) # 15 minutes TTL
    
    await send_password_reset_email(password_req.email, token)
    return {"message": translator.t("password_reset_sent", lang=lang)}

@router.post("/reset-password")
async def reset_password(
    *,
    db: AsyncSession = Depends(deps.get_db),
    reset_req: schemas.PasswordReset,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Reset password with the token received in the email.
    """
    raw_user_id = await redis_client.get(f"password_reset:{reset_req.token}")
    if not raw_user_id:
        raise HTTPException(status_code=400, detail=translator.t("invalid_token", lang=lang))

    # Redis usually returns bytes; safely decode and cast to UUID
    try:
        if isinstance(raw_user_id, bytes):
            user_id_str = raw_user_id.decode("utf-8")
        else:
            user_id_str = str(raw_user_id)
        user_id = UUID(user_id_str)
    except Exception:
        # Treat malformed IDs as invalid tokens
        raise HTTPException(status_code=400, detail=translator.t("invalid_token", lang=lang))
    
    user = await crud.user.get(db, id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail=translator.t("user_not_found", lang=lang))
        
    # Update password
    await crud.user.update(db, db_obj=user, obj_in={"password": reset_req.new_password})
    
    # Invalidate token and session
    await redis_client.delete(f"password_reset:{reset_req.token}")
    await redis_client.delete(f"session:{user.id}") # Force relogin
    
    return {"message": translator.t("password_reset_success", lang=lang)}