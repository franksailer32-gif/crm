from datetime import datetime, timezone, timedelta
from typing import Generator, Optional
from fastapi import Depends, HTTPException, status, Request, Header
from app.core.i18n import translator
from fastapi.security import OAuth2PasswordBearer
from jose import jwt
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.core import security
from app.core.config import settings
from app.core.redis import redis_client
from app.models.subscription import Subscription, EntityType
from app.db.session import SessionLocal

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login/access-token",
    auto_error=False
)

async def get_db() -> Generator:
    async with SessionLocal() as session:
        yield session

def get_lang(accept_language: Optional[str] = Header(None)) -> str:
    if not accept_language:
        return "en"
    # Simple parsing: take the first language in the list
    return accept_language.split(",")[0].split(";")[0].strip() or "en"

async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db), 
    token: Optional[str] = Depends(reusable_oauth2)
) -> models.user.User:
    if not token:
        token = request.cookies.get("access_token")
        
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=translator.t("credentials_error", lang=get_lang(request.headers.get("accept-language"))),
        )
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        token_data = schemas.TokenPayload(**payload)
        
        # Ensure it's not a refresh token being used for data access
        if payload.get("type") == "refresh":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=translator.t("refresh_token_error", lang=get_lang(request.headers.get("accept-language"))),
            )
    except (jwt.JWTError, ValidationError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=translator.t("credentials_error", lang=get_lang(request.headers.get("accept-language"))),
        )
        
    # Check if a session exists in redis (handles logout and single-session enforcement)
    active_jti = await redis_client.get(f"session:{token_data.sub}")
    if not active_jti or active_jti != token_data.jti:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=translator.t("session_expired", lang=get_lang(request.headers.get("accept-language"))),
        )
        
    user = await crud.user.get(db, id=token_data.sub)
    if not user:
        raise HTTPException(status_code=404, detail=translator.t("user_not_found", lang=get_lang(request.headers.get("accept-language"))))

    # [Mod] Allow inactive users to be returned so the frontend can still load
    # restricted profiles (allowing them to reach the Billing page to reactivate).
    # Actual access control is handled by check_active_subscription and frontend guards.
        
    if user.org_id:
        org = await crud.org.get(db, id=user.org_id)
        if org and not org.is_active:
            raise HTTPException(status_code=403, detail=translator.t("org_suspended", lang=get_lang(request.headers.get("accept-language"))))

    # Manually load subscription for user response (polymorphic relationship)
    entity_id = user.org_id if user.org_id else user.id
    entity_type = EntityType.organization if user.org_id else EntityType.solo
    
    sub_res = await db.execute(
        select(Subscription).where(
            Subscription.entity_id == entity_id,
            Subscription.entity_type == entity_type
        )
    )
    user.subscription = sub_res.scalars().first()
    
    # NEW: Sync individual seat status for non-admin members
    from app.models.user import UserRole
    if user.org_id and user.role != UserRole.org_admin:
        # Use the already-prefetched subscription_seat from CRUDUser.get
        if user.subscription:
            # Note: We are modifying the ephemeral 'status' for the response.
            # SA won't persist this unless we commit, and we don't commit in get_current_user.
            if user.subscription_seat:
                user.subscription.status = user.subscription_seat.status
            else:
                # If they belong to an org but have no seat record, they are technically in trial
                user.subscription.status = "trial"

    return user

async def get_current_user_optional(
    request: Request,
    db: AsyncSession = Depends(get_db), 
    token: Optional[str] = Depends(reusable_oauth2)
) -> Optional[models.user.User]:
    try:
        return await get_current_user(request, db, token)
    except HTTPException:
        return None

async def get_current_active_superuser(
    current_user: models.User = Depends(get_current_user),
    lang: str = Depends(get_lang),
) -> models.User:
    if current_user.role != models.UserRole.super_admin:
        raise HTTPException(
            status_code=403, detail=translator.t("insufficient_privileges", lang=lang)
        )
    return current_user

async def get_current_org_admin(
    current_user: models.User = Depends(get_current_user),
    lang: str = Depends(get_lang),
) -> models.User:
    if current_user.role not in [models.UserRole.org_admin, models.UserRole.super_admin]:
        raise HTTPException(
            status_code=403, detail=translator.t("insufficient_privileges", lang=lang)
        )
    return current_user

async def check_active_subscription(
    db: AsyncSession = Depends(get_db),
    current_user: models.User = Depends(get_current_user),
    lang: str = Depends(get_lang),
) -> models.User:
    """
    Dependency to enforce read-only access for expired/inactive users.
    Should be used on all POST, PUT, PATCH, DELETE endpoints.
    """
    if current_user.role == models.UserRole.super_admin or current_user.email == "wpd@w-p-d.de":
        return current_user

    # 1. Check seat subscription (source of truth for user entitlement)
    from app.models.subscription import Subscription, SubscriptionSeat, SubscriptionStatus, EntityType
    
    # Check individual seat status (The definitive source for access)
    result = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id == current_user.id)
    )
    seat = result.scalars().first()
    
    

    if not seat:
        # If no seat record exists, check user's trial from profile (fallback: 3 days)
        trial_end = current_user.trial_ends_at or (current_user.created_at + timedelta(days=3))
        if datetime.now(timezone.utc).replace(tzinfo=None) > trial_end.replace(tzinfo=None):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=translator.t("trial_expired", lang=lang)
            )
        return current_user

    # 2. Check Trial Expiration for Seat
    if seat.status == SubscriptionStatus.trial:
        trial_end = seat.trial_ends_at or current_user.trial_ends_at or (current_user.created_at + timedelta(days=3))
        if datetime.now(timezone.utc).replace(tzinfo=None) > trial_end.replace(tzinfo=None):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=translator.t("trial_expired", lang=lang)
            )
            
    # 3. Check Active Status & Expiration
    if seat.status == SubscriptionStatus.active:
        # Enforce that the org entity subscription is active for all org users.
        if current_user.org_id:
            res = await db.execute(
                select(Subscription).where(
                    Subscription.entity_id == current_user.org_id,
                    Subscription.entity_type == EntityType.organization,
                )
            )
            org_sub = res.scalars().first()
            if not org_sub or org_sub.status != SubscriptionStatus.active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=translator.t("org_past_due", lang=lang)
                )

        # Strict Date Check for the individual seat
        if seat.next_billing_date:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            grace_cutoff = seat.next_billing_date.replace(tzinfo=None) + timedelta(days=1)
            
            if now > grace_cutoff:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=translator.t("subscription_expired", lang=lang)
                )
        return current_user
    
    # Allow grace period for past_due status (Direct Debit takes 3-5 days)
    if seat.status == SubscriptionStatus.past_due:
        # Check if the seat was created/updated within the last 7 days as a grace period
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        # Use seat.created_at or a recent update to allow the initial window
        created_at = seat.created_at.replace(tzinfo=None) if hasattr(seat, "created_at") and seat.created_at else current_user.created_at.replace(tzinfo=None)
        if now < (created_at + timedelta(days=7)):
            return current_user
        
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, 
            detail=translator.t("subscription_past_due", lang=lang)
        )

    if seat.status == SubscriptionStatus.cancelled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=translator.t("subscription_cancelled", lang=lang)
        )
    
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=translator.t("subscription_inactive", lang=lang)
    )
