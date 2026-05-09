from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import desc
import uuid
from sqlalchemy import update
from sqlalchemy import delete



from app import models, schemas
from app.api import deps
from app.core.i18n import translator

router = APIRouter()

@router.get("/", response_model=List[schemas.Notification])
async def get_notifications(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    limit: int = 20,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get recent notifications for the current user.
    """
    query = select(models.Notification).where(
        models.Notification.user_id == current_user.id
    ).order_by(desc(models.Notification.created_at)).limit(limit)
    
    result = await db.execute(query)
    return result.scalars().all()

@router.patch("/{notification_id}/read", response_model=schemas.Notification)
async def mark_as_read(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Mark a notification as read.
    """
    notification = await db.get(models.Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail=translator.t("notification_not_found", lang=lang))
    if notification.user_id != current_user.id:
        raise HTTPException(status_code=403, detail=translator.t("not_authorized", lang=lang))
    
    notification.is_read = True
    await db.commit()
    await db.refresh(notification)
    return notification

@router.post("/read-all")
async def mark_all_as_read(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Mark all unread notifications for the user as read.
    """
    stmt = update(models.Notification).where(
        models.Notification.user_id == current_user.id,
        models.Notification.is_read == False
    ).values(is_read=True)
    
    await db.execute(stmt)
    await db.commit()
    return {"detail": translator.t("notifications_marked_read", lang=lang)}

@router.delete("/{notification_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
):
    """
    Delete a notification.
    """
    notification = await db.get(models.Notification, notification_id)
    if not notification:
        raise HTTPException(status_code=404, detail=translator.t("notification_not_found", lang=lang))
    if notification.user_id != current_user.id:
        raise HTTPException(status_code=403, detail=translator.t("not_authorized", lang=lang))
    
    await db.delete(notification)
    await db.commit()
    return None

@router.delete("/", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_all_notifications(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
):
    """
    Delete all notifications for the current user.
    """
    stmt = delete(models.Notification).where(models.Notification.user_id == current_user.id)
    await db.execute(stmt)
    await db.commit()
    return None
