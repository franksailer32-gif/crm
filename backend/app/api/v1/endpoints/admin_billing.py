from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
import uuid

from app.api import deps
from app.models.user import User, UserRole
from app.models.subscription import PaymentRecord, SubscriptionSeat, Subscription, EntityType
from app.models.org import Organization
from app.models.subscription import SubscriptionStatus

router = APIRouter()

@router.get("/debug/payment-records")
async def debug_payment_records(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    """
    Debug helper for Super Admin: shows whether PaymentRecord rows exist.
    """
    total = await db.scalar(select(func.count()).select_from(PaymentRecord))
    latest_res = await db.execute(
        select(PaymentRecord)
        .order_by(PaymentRecord.created_at.desc())
        .limit(10)
    )
    latest = latest_res.scalars().all()
    return {
        "payment_records_count": int(total or 0),
        "latest": [
            {
                "id": r.id,
                "entity_id": str(r.entity_id),
                "user_id": str(r.user_id) if r.user_id else None,
                "amount": float(r.amount),
                "status": r.status,
                "date": r.created_at,
                "gocardless_payment_id": r.gocardless_payment_id,
                "description": r.description,
            }
            for r in latest
        ],
    }

@router.get("/history", response_model=List[Any])
async def get_global_payment_history(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_superuser),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """
    Super Admin endpoint to view ALL payments across the system.
    """
    records_res = await db.execute(
        select(PaymentRecord)
        .order_by(PaymentRecord.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    records: List[PaymentRecord] = records_res.scalars().all()
    if not records:
        return []

    entity_ids = list({r.entity_id for r in records})
    member_user_ids = list({(r.user_id or r.entity_id) for r in records})

    # Batch-load orgs and users to avoid N+1 queries.
    orgs_res = await db.execute(select(Organization).where(Organization.id.in_(entity_ids)))
    orgs_by_id = {o.id: o for o in orgs_res.scalars().all()}

    users_res = await db.execute(select(User).where(User.id.in_(member_user_ids)))
    users_by_id = {u.id: u for u in users_res.scalars().all()}

    # SubscriptionSeat info (for next billing date + seat status)
    seat_res = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id.in_(member_user_ids))
    )
    seat_by_user_id = {s.user_id: s for s in seat_res.scalars().all()}

    # Master Subscription info (for org-level renewals / solo next dates)
    subs_res = await db.execute(
        select(Subscription).where(Subscription.entity_id.in_(entity_ids))
    )
    subs_by_entity = {}
    for s in subs_res.scalars().all():
        subs_by_entity[(s.entity_type, s.entity_id)] = s

    # Determine payer per org: prefer organization.owner_id, else first org_admin in org
    org_ids = list(orgs_by_id.keys())
    payer_by_org_id: dict[uuid.UUID, Optional[User]] = {}
    for org_id in org_ids:
        org = orgs_by_id.get(org_id)
        if org and org.owner_id:
            payer_by_org_id[org_id] = users_by_id.get(org.owner_id)
        else:
            payer_by_org_id[org_id] = None

    if org_ids:
        admins_res = await db.execute(
            select(User).where(User.org_id.in_(org_ids), User.role == UserRole.org_admin)
        )
        for admin in admins_res.scalars().all():
            if admin.org_id and payer_by_org_id.get(admin.org_id) is None:
                payer_by_org_id[admin.org_id] = admin

    output: List[dict] = []
    for r in records:
        org = orgs_by_id.get(r.entity_id)
        member_user_id = r.user_id or r.entity_id
        member = users_by_id.get(member_user_id)

        payer: Optional[User] = payer_by_org_id.get(r.entity_id) if org else None
        if org and (payer is None and org.owner_id):
            payer = users_by_id.get(org.owner_id)

        seat = seat_by_user_id.get(member_user_id)
        next_payment_date = None
        seat_status = None
        if seat:
            next_payment_date = seat.next_billing_date
            seat_status = seat.status.value if hasattr(seat.status, "value") else seat.status
        else:
            # Fallback for cases where PaymentRecord.user_id is None (org/solo master renewal)
            if org:
                sub = subs_by_entity.get((EntityType.organization, org.id))
            else:
                # solo: entity_id is the solo user's id
                sub = subs_by_entity.get((EntityType.solo, r.entity_id))
            if sub:
                next_payment_date = sub.current_period_end
                seat_status = sub.status.value if hasattr(sub.status, "value") else sub.status

        entity_name = (
            f"Org: {org.name}" if org else f"Solo: {member.email if member else 'N/A'}"
        )
        member_email = member.email if member else "N/A"
        member_full_name = member.full_name if member else None
        payer_email = payer.email if payer else "N/A"
        payer_full_name = payer.full_name if payer else None

        output.append(
            {
                "id": r.id,
                "entity": entity_name,
                "user_email": member_email,  # kept for backward compatibility with UI
                "user_full_name": member_full_name,
                "payer_email": payer_email,
                "payer_full_name": payer_full_name,
                "billed_member_id": member_user_id if r.user_id else None,
                "amount": float(r.amount),
                "currency": r.currency,
                "date": r.created_at,
                "description": r.description,
                "status": r.status,
                "gocardless_id": r.gocardless_payment_id,
                "seat_status": seat_status,
                "next_payment_date": next_payment_date,
            }
        )

    return output
