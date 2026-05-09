from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload


from app import crud, models, schemas
from app.api import deps
from app.core.i18n import translator
from app.services.verification_service import generate_verification_token, store_pending_registration
from app.services.email_service import send_invite_email

router = APIRouter()

@router.get("/me", response_model=schemas.Organization)
async def read_org_me(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get current organization details.
    """
    if not current_user.org_id:
        raise HTTPException(status_code=400, detail=translator.t("user_no_org", lang=lang))
    org = await crud.org.get(db, id=current_user.org_id)
    if not org:
        raise HTTPException(status_code=404, detail=translator.t("org_not_found", lang=lang))
    return org

@router.patch("/me", response_model=schemas.Organization)
async def update_org_me(
    *,
    db: AsyncSession = Depends(deps.get_db),
    org_in: schemas.OrgUpdate,
    current_user: models.User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Update organization details. Only for Org Admins.
    Professional-grade validation:
    - Ensures slug uniqueness across the system.
    - Prevents critical identity fields from being cleared.
    """
    if not current_user.org_id:
        raise HTTPException(status_code=400, detail=translator.t("user_no_org", lang=lang))
        
    org = await crud.org.get(db, id=current_user.org_id)
    if not org:
        raise HTTPException(status_code=404, detail=translator.t("org_not_found", lang=lang))
        
    # Security Check: Handle Slug Changes
    if org_in.slug is not None:
        if not org_in.slug.strip():
            raise HTTPException(status_code=400, detail=translator.t("slug_empty", lang=lang))
            
        if org_in.slug != org.slug:
            existing_org = await crud.org.get_by_slug(db, slug=org_in.slug)
            if existing_org:
                raise HTTPException(
                    status_code=400, 
                    detail=translator.t("slug_exists", lang=lang)
                )
    
    # Validation: Prevent empty name
    if org_in.name is not None and not org_in.name.strip():
        raise HTTPException(status_code=400, detail=translator.t("name_empty", lang=lang))
        
    org = await crud.org.update(db, db_obj=org, obj_in=org_in)
    return org

@router.post("/invites")
async def send_user_invite(
    *,
    db: AsyncSession = Depends(deps.get_db),
    invite_in: schemas.UserInviteRequest,
    current_user: models.User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Send an email invitation for a new user to join the organization.
    Only for Org Admins.
    """
    if not current_user.org_id:
        raise HTTPException(status_code=400, detail=translator.t("user_no_org", lang=lang))
        
    org = await crud.org.get(db, id=current_user.org_id)
    if not org:
        raise HTTPException(status_code=404, detail=translator.t("org_not_found", lang=lang))

    existing_user = await crud.user.get_by_email(db, email=invite_in.email)
    if existing_user:
        raise HTTPException(
            status_code=400,
            detail=translator.t("email_exists", lang=lang)
        )

    # Generate token
    token = generate_verification_token()
    payload = {
        "email": invite_in.email,
        "org_id": str(org.id),
        "role": invite_in.role.value if hasattr(invite_in.role, "value") else invite_in.role
    }
    
    # Store in Redis (expires in 48 hours for invites)
    await store_pending_registration(token, payload)
    
    # Send email
    await send_invite_email(invite_in.email, token, org.name)
    
    return {"message": translator.t("invite_sent", lang=lang)}

@router.get("/users", response_model=List[schemas.User])
async def read_org_users(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    List all users in the current organization.
    """
    stmt = (
        select(models.User)
        .where(models.User.org_id == current_user.org_id)
        .options(selectinload(models.User.subscription_seat))
    )
    result = await db.execute(stmt)
    users = result.scalars().all()
    return users

@router.get("/settings", response_model=schemas.OrgSettings)
async def read_org_settings(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get organization settings. Org Admin only.
    """
    if not current_user.org_id:
        raise HTTPException(status_code=400, detail=translator.t("user_no_org", lang=lang))
    
    result = await db.execute(select(models.OrgSettings).where(models.OrgSettings.org_id == current_user.org_id))
    settings = result.scalars().first()
    
    if not settings:
        # Create default settings if not exists
        settings = models.OrgSettings(org_id=current_user.org_id)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
        
    return settings

@router.patch("/settings", response_model=schemas.OrgSettings)
async def update_org_settings(
    *,
    db: AsyncSession = Depends(deps.get_db),
    settings_in: schemas.OrgSettingsUpdate,
    current_user: models.User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Update organization settings. Org Admin only.
    """
    if not current_user.org_id:
        raise HTTPException(status_code=400, detail=translator.t("user_no_org", lang=lang))
        
    result = await db.execute(select(models.OrgSettings).where(models.OrgSettings.org_id == current_user.org_id))
    settings = result.scalars().first()
    
    if not settings:
        settings = models.OrgSettings(org_id=current_user.org_id)
        db.add(settings)
    
    update_data = settings_in.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(settings, field, value)
        
    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    return settings
