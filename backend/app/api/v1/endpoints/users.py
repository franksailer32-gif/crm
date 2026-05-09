import uuid
from typing import Any
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app import crud, models, schemas
from app.api import deps
from app.services import verification_service, email_service
from app.core.i18n import translator

router = APIRouter()

@router.get("/me", response_model=schemas.User)
async def read_user_me(
    current_user: models.User = Depends(deps.get_current_user)
) -> Any:
    """
    Get current user.
    """
    return current_user

@router.patch("/me", response_model=schemas.User)
async def update_user_me(
    *,
    db: AsyncSession = Depends(deps.get_db),
    user_in: schemas.UserUpdateMe,
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Update own user profile.
    Restricted to specific fields; resets verification if email changes.
    """
    update_data = user_in.model_dump(exclude_unset=True)
    
    # 1. Secure Password Update
    if "password" in update_data:
        if not user_in.current_password:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=translator.t("current_password_required", lang=lang)
            )
        # Verify current password
        user = await crud.user.authenticate(
            db, email=current_user.email, password=user_in.current_password
        )
        if not user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=translator.t("incorrect_current_password", lang=lang)
            )
        # Hashing is handled by crud.user.update — remove current_password, it's not a DB field
        update_data.pop("current_password", None)
    
    # Always ensure current_password is stripped (even if no new password was sent)
    update_data.pop("current_password", None)
    
    # 2. Secure Email Update (Verification Flow)
    if "email" in update_data and update_data["email"] != current_user.email:
        new_email = update_data["email"]
        # Check if email is already taken
        user = await crud.user.get_by_email(db, email=new_email)
        if user:
            raise HTTPException(
                status_code=400,
                detail=translator.t("email_exists", lang=lang)
            )
            
        # Initiate verification flow for the NEW email
        token = verification_service.generate_verification_token()
        payload = {
            "type": "email_change",
            "user_id": str(current_user.id),
            "new_email": new_email
        }
        await verification_service.store_pending_registration(token, payload, ttl=3600) # 1 hour
        
        # Send verification to the NEW email
        await email_service.send_email_change_verification_email(new_email, token)
        
        # Remove from update_data so it's NOT changed in DB yet
        del update_data["email"]
        
    user = await crud.user.update(db, db_obj=current_user, obj_in=update_data)
    return user

@router.get("/{user_id}", response_model=schemas.User)
async def read_user_by_id(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get a specific user by id.
    Org Admins can get users in their organization.
    Users can get themselves.
    """
    user = await crud.user.get(db, id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail=translator.t("user_not_found", lang=lang))
        
    if user.id == current_user.id:
        return user
        
    if current_user.role in [models.UserRole.org_admin, models.UserRole.super_admin]:
        if current_user.role == models.UserRole.org_admin and user.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        return user
        
    raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))

@router.patch("/{user_id}", response_model=schemas.User)
async def update_user(
    *,
    db: AsyncSession = Depends(deps.get_db),
    user_id: uuid.UUID,
    user_in: schemas.UserUpdate,
    current_user: models.User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Update a user. 
    Only Org Admins can update users in their organization.
    """
    user = await crud.user.get(db, id=user_id)
    if not user:
        raise HTTPException(status_code=404, detail=translator.t("user_not_found", lang=lang))
        
    if current_user.role == models.UserRole.org_admin and user.org_id != current_user.org_id:
        raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        
    update_data = user_in.model_dump(exclude_unset=True)

    # Validate reassignment target only when explicitly deactivating a rep.
    if update_data.get("is_active") is False and update_data.get("reassign_to") is not None:
        target_id = update_data["reassign_to"]
        if target_id == user.id:
            raise HTTPException(status_code=400, detail=translator.t("reassign_to_self", lang=lang))

        target = await crud.user.get(db, id=target_id)
        if not target:
            raise HTTPException(status_code=404, detail=translator.t("reassign_target_not_found", lang=lang))
        if target.role != models.UserRole.rep:
            raise HTTPException(status_code=400, detail=translator.t("reassign_target_role_rep", lang=lang))

        # Enforce same org scope for org reps.
        if user.org_id is not None:
            if target.org_id != user.org_id:
                raise HTTPException(status_code=403, detail=translator.t("reassign_same_org", lang=lang))
        else:
            # Solo user: target must also be solo.
            if target.org_id is not None:
                raise HTTPException(status_code=403, detail=translator.t("reassign_target_solo", lang=lang))

        # Org admins can only assign within their own org.
        if current_user.role == models.UserRole.org_admin and target.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))

    user = await crud.user.update(db, db_obj=user, obj_in=user_in)
    return user
