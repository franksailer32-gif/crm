from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from sqlalchemy.orm import selectinload
import uuid
from datetime import datetime, timezone, timedelta

from app import models, schemas, crud
from app.api import deps
from app.core.i18n import translator
from app.tasks.reports import recalculate_customer_statuses
from app.models.subscription import Subscription, EntityType, SubscriptionStatus, PlanTier, SubscriptionSeat
from app.services.rep_deactivation_service import handle_rep_deactivation
from app.models.audit_log import AuditLog
from app.core.redis import redis_client

router = APIRouter()

# Helpers 
async def _write_audit_log(
    db: AsyncSession,
    *,
    actor: models.User,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    details: Optional[dict] = None,
) -> None:
    """Write an entry to the audit_logs table."""
    log = AuditLog(
        user_id=actor.id,
        org_id=getattr(actor, "org_id", None),
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=details or {},
    )
    db.add(log)
    # Don't commit here — caller commits


async def get_entity_subscription_details(
    db: AsyncSession,
    entity_id: uuid.UUID,
    entity_type: EntityType,
) -> dict:
    """
    Helper to get subscription details from Subscription table.
    Falls back to defaults when no subscription exists yet.
    """
    query = select(Subscription).where(
        Subscription.entity_id == entity_id,
        Subscription.entity_type == entity_type
    )
    result = await db.execute(query)
    sub = result.scalars().first()
    if not sub:
        return {
            "status": "trial",
            "plan_tier": PlanTier.starter,
            "price_per_user": 7.50 if entity_type == EntityType.organization else 7.90,
        }

    return {
        "status": sub.status.value,
        "plan_tier": sub.plan_tier,
        "price_per_user": float(sub.price_per_user) if float(sub.price_per_user) > 0 else (7.50 if entity_type == EntityType.organization else 7.90),
    }


# Overview 

@router.get("/overview", response_model=schemas.SystemOverview)
async def get_system_overview(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get global aggregated stats for the super admin dashboard.
    Uses a single round-trip per stat via scalar queries — minimal overhead.
    """
    total_orgs, total_users, total_customers, total_visits = (
        await db.scalar(select(func.count()).select_from(models.Organization)),
        await db.scalar(select(func.count()).select_from(models.User)),
        await db.scalar(select(func.count()).select_from(models.Customer)),
        await db.scalar(select(func.count()).select_from(models.Visit)),
    )

    return {
        "total_orgs": total_orgs or 0,
        "total_users": total_users or 0,
        "total_customers": total_customers or 0,
        "total_visits": total_visits or 0,
    }


# Organizations 

@router.get("/orgs", response_model=List[schemas.OrgStatItem])
async def get_organizations_stats(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get stats for all organizations including billing status.
    Uses batch queries to avoid N+1.
    """
    # 1. Load all orgs
    result = await db.execute(select(models.Organization))
    orgs = result.scalars().all()
    if not orgs:
        return []

    org_ids = [o.id for o in orgs]
    org_by_id = {o.id: o for o in orgs}

    # 2. Batch: user counts grouped by org_id
    user_count_rows = await db.execute(
        select(models.User.org_id, func.count().label("cnt"))
        .where(models.User.org_id.in_(org_ids))
        .group_by(models.User.org_id)
    )
    user_counts = {row.org_id: row.cnt for row in user_count_rows}

    # 3. Batch: customer counts grouped by org_id
    cust_count_rows = await db.execute(
        select(models.Customer.org_id, func.count().label("cnt"))
        .where(models.Customer.org_id.in_(org_ids))
        .group_by(models.Customer.org_id)
    )
    cust_counts = {row.org_id: row.cnt for row in cust_count_rows}

    # 4. Batch: all org-level subscriptions
    subs_rows = await db.execute(
        select(Subscription).where(
            Subscription.entity_id.in_(org_ids),
            Subscription.entity_type == EntityType.organization,
        )
    )
    subs_by_org = {s.entity_id: s for s in subs_rows.scalars().all()}

    # 5. Batch: all owner users in one query
    owner_ids = [o.owner_id for o in orgs if o.owner_id]
    owners_rows = await db.execute(
        select(models.User).where(models.User.id.in_(owner_ids))
    )
    owners_by_id = {u.id: u for u in owners_rows.scalars().all()}

    # 6. Assemble
    org_stats = []
    for org in orgs:
        sub = subs_by_org.get(org.id)
        if sub:
            billing_status = sub.status.value
            plan_tier = sub.plan_tier
            price_per_user = float(sub.price_per_user) if float(sub.price_per_user) > 0 else 7.50
        else:
            billing_status = "trial"
            plan_tier = PlanTier.starter
            price_per_user = 7.50

        owner = owners_by_id.get(org.owner_id) if org.owner_id else None
        owner_name = (owner.full_name or owner.email) if owner else None

        org_stats.append({
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "is_active": org.is_active,
            "billing_status": billing_status,
            "plan_tier": plan_tier,
            "price_per_user": price_per_user,
            "created_at": org.created_at,
            "user_count": user_counts.get(org.id, 0),
            "customer_count": cust_counts.get(org.id, 0),
            "owner_name": owner_name,
        })

    return org_stats


# Users 

@router.get("/users", response_model=List[schemas.UserStatItem])
async def get_users_stats(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get stats for all non-super-admin users.
    Uses batch queries to avoid N+1.
    """
    # 1. Load all non-super-admin users
    result = await db.execute(
        select(models.User).where(models.User.role != models.UserRole.super_admin)
    )
    users = result.scalars().all()
    if not users:
        return []

    user_ids = [u.id for u in users]
    solo_user_ids = [u.id for u in users if u.user_type == models.UserType.solo]
    org_ids_set = {u.org_id for u in users if u.org_id}

    # 2. Batch: all solo subscriptions
    solo_subs_rows = await db.execute(
        select(Subscription).where(
            Subscription.entity_id.in_(solo_user_ids),
            Subscription.entity_type == EntityType.solo,
        )
    )
    solo_subs_by_user = {s.entity_id: s for s in solo_subs_rows.scalars().all()}

    # 3. Batch: all org subscriptions
    org_subs_by_id = {}
    if org_ids_set:
        org_subs_rows = await db.execute(
            select(Subscription).where(
                Subscription.entity_id.in_(list(org_ids_set)),
                Subscription.entity_type == EntityType.organization,
            )
        )
        org_subs_by_id = {s.entity_id: s for s in org_subs_rows.scalars().all()}

    # 4. Batch: all subscription seats
    seats_rows = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id.in_(user_ids))
    )
    seats_by_user = {s.user_id: s for s in seats_rows.scalars().all()}

    # 5. Batch: all orgs for name lookup
    orgs_by_id = {}
    if org_ids_set:
        orgs_rows = await db.execute(
            select(models.Organization).where(models.Organization.id.in_(list(org_ids_set)))
        )
        orgs_by_id = {o.id: o for o in orgs_rows.scalars().all()}

    # 6. Assemble
    user_stats = []
    for user in users:
        if user.user_type == models.UserType.solo:
            sub = solo_subs_by_user.get(user.id)
        else:
            sub = org_subs_by_id.get(user.org_id) if user.org_id else None

        if sub:
            billing_status = sub.status.value
            plan_tier = sub.plan_tier
            price_per_user = float(sub.price_per_user) if float(sub.price_per_user) > 0 else 7.50
        else:
            billing_status = "trial"
            plan_tier = PlanTier.starter
            price_per_user = 7.50 if user.org_id else 7.90

        org = orgs_by_id.get(user.org_id) if user.org_id else None
        seat = seats_by_user.get(user.id)

        user_stats.append({
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "role": user.role,
            "user_type": user.user_type,
            "is_active": user.is_active,
            "is_trial": user.is_trial,
            "billing_status": billing_status,
            "plan_tier": plan_tier,
            "price_per_user": price_per_user,
            "org_name": org.name if org else None,
            "created_at": user.created_at,
            "seat_status": seat.status.value if seat else None,
            "seat_is_active": seat.is_active if seat else None,
            "activation_pending": (
                bool(getattr(seat, "gocardless_subscription_id", None))
                and seat.status != SubscriptionStatus.active
                and not seat.is_active
                and not seat.next_billing_date
            ) if seat else False,
        })

    return user_stats


# Org Actions 

@router.put("/orgs/{org_id}/status", response_model=schemas.OrgStatItem)
async def toggle_organization_status(
    org_id: uuid.UUID,
    status_update: schemas.OrgStatusUpdate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Toggle an organization's active status."""
    org = await crud.org.get(db, id=org_id)
    if not org:
        raise HTTPException(status_code=404, detail=translator.t("org_not_found", lang=lang))

    org = await crud.org.update(db, db_obj=org, obj_in={"is_active": status_update.is_active})

    # Audit
    await _write_audit_log(
        db, actor=current_user,
        action="org.status_changed",
        target_type="organization",
        target_id=org_id,
        details={"is_active": status_update.is_active, "org_name": org.name},
    )

    subscription_details = await get_entity_subscription_details(db, org.id, EntityType.organization)
    billing_status = subscription_details["status"]
    ucount = await db.scalar(select(func.count()).select_from(models.User).filter(models.User.org_id == org.id))
    ccount = await db.scalar(select(func.count()).select_from(models.Customer).filter(models.Customer.org_id == org.id))
    owner_name = None
    if org.owner_id:
        owner = await db.get(models.User, org.owner_id)
        if owner:
            owner_name = owner.full_name or owner.email

    await db.commit()
    return {
        "id": org.id,
        "name": org.name,
        "slug": org.slug,
        "is_active": org.is_active,
        "billing_status": billing_status,
        "plan_tier": subscription_details.get("plan_tier"),
        "price_per_user": subscription_details.get("price_per_user"),
        "created_at": org.created_at,
        "user_count": ucount or 0,
        "customer_count": ccount or 0,
        "owner_name": owner_name,
    }


@router.put("/orgs/{org_id}/billing")
async def update_org_billing(
    org_id: uuid.UUID,
    billing_data: schemas.BillingStatusUpdate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Set billing status for an organization.
    Updates org-level Subscription and cascades to all member seats.
    """
    now = datetime.now(timezone.utc)

    query = select(Subscription).where(
        Subscription.entity_id == org_id,
        Subscription.entity_type == EntityType.organization
    )
    result = await db.execute(query)
    sub = result.scalars().first()

    if sub:
        sub.status = billing_data.status
        if float(sub.price_per_user) <= 0:
            sub.price_per_user = 7.50
    else:
        sub = Subscription(
            entity_id=org_id,
            entity_type=EntityType.organization,
            status=billing_data.status,
            plan_tier=billing_data.plan_tier or PlanTier.starter,
            price_per_user=7.50,
        )
        db.add(sub)

    if billing_data.plan_tier:
        sub.plan_tier = billing_data.plan_tier
        if float(sub.price_per_user) <= 0:
            sub.price_per_user = 7.50

    org_users_res = await db.execute(select(models.User).where(models.User.org_id == org_id))
    org_users = org_users_res.scalars().all()
    user_ids = [u.id for u in org_users]
    seats_res = await db.execute(select(SubscriptionSeat).where(SubscriptionSeat.user_id.in_(user_ids)))
    seats_by_user = {s.user_id: s for s in seats_res.scalars().all()}

    await db.flush()  # ensure sub.id is populated before creating seats

    for u in org_users:
        seat = seats_by_user.get(u.id)
        if billing_data.status == SubscriptionStatus.active:
            if not seat:
                seat = SubscriptionSeat(
                    subscription_id=sub.id,
                    user_id=u.id,
                    status=SubscriptionStatus.active,
                    gocardless_subscription_id=None,
                    next_billing_date=now + timedelta(days=30),
                    is_active=True,
                )
                db.add(seat)
            else:
                seat.subscription_id = sub.id
                seat.status = SubscriptionStatus.active
                seat.next_billing_date = now + timedelta(days=30)
                seat.is_active = True
            u.is_active = True
            u.is_trial = False
            if hasattr(u, "trial_ends_at"):
                u.trial_ends_at = None

        elif billing_data.status == SubscriptionStatus.trial:
            if not seat:
                seat = SubscriptionSeat(
                    subscription_id=sub.id,
                    user_id=u.id,
                    status=SubscriptionStatus.trial,
                    gocardless_subscription_id=None,
                    trial_ends_at=u.created_at + timedelta(days=3),
                    is_active=True,
                )
                db.add(seat)
            else:
                seat.status = SubscriptionStatus.trial
                seat.trial_ends_at = u.created_at + timedelta(days=3)
                seat.is_active = True
            u.is_active = True
            u.is_trial = True

        elif billing_data.status in [SubscriptionStatus.past_due, SubscriptionStatus.cancelled]:
            if seat:
                seat.status = billing_data.status
                seat.is_active = False
                seat.next_billing_date = None
                seat.trial_ends_at = None
            u.is_active = False
            u.is_trial = False
            if u.role == models.UserRole.rep:
                await handle_rep_deactivation(db, rep_id=u.id, org_id=u.org_id, reassign_to=None)

    # Audit
    await _write_audit_log(
        db, actor=current_user,
        action="org.billing_changed",
        target_type="organization",
        target_id=org_id,
        details={
            "new_status": billing_data.status.value,
            "plan_tier": billing_data.plan_tier.value if billing_data.plan_tier else None,
            "members_affected": len(org_users),
        },
    )

    await db.commit()
    return {"detail": translator.t("billing_status_updated", lang=lang)}


# User Billing

@router.put("/users/{user_id}/billing")
async def update_user_billing(
    user_id: uuid.UUID,
    billing_data: schemas.BillingStatusUpdate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Set billing status for a Solo User."""
    now = datetime.now(timezone.utc)
    user = await crud.user.get(db, id=user_id)
    if not user or user.user_type != models.UserType.solo:
        raise HTTPException(status_code=400, detail=translator.t("solo_user_billing_only", lang=lang))

    query = select(Subscription).where(
        Subscription.entity_id == user_id,
        Subscription.entity_type == EntityType.solo
    )
    result = await db.execute(query)
    sub = result.scalars().first()

    if sub:
        sub.status = billing_data.status
        if float(sub.price_per_user) <= 0:
            sub.price_per_user = 7.90
    else:
        sub = Subscription(
            entity_id=user_id,
            entity_type=EntityType.solo,
            status=billing_data.status,
            plan_tier=billing_data.plan_tier or PlanTier.starter,
            price_per_user=7.90,
        )
        db.add(sub)

    if billing_data.plan_tier:
        sub.plan_tier = billing_data.plan_tier
        if float(sub.price_per_user) <= 0:
            sub.price_per_user = 7.90

    seat_res = await db.execute(select(SubscriptionSeat).where(SubscriptionSeat.user_id == user_id))
    seat = seat_res.scalars().first()

    await db.flush()

    if billing_data.status == SubscriptionStatus.active:
        if not seat:
            seat = SubscriptionSeat(
                subscription_id=sub.id,
                user_id=user.id,
                status=SubscriptionStatus.active,
                gocardless_subscription_id=None,
                next_billing_date=now + timedelta(days=30),
                is_active=True,
            )
            db.add(seat)
        else:
            seat.subscription_id = sub.id
            seat.status = SubscriptionStatus.active
            seat.is_active = True
            seat.next_billing_date = now + timedelta(days=30)
        user.is_active = True
        user.is_trial = False
        if hasattr(user, "trial_ends_at"):
            user.trial_ends_at = None

    elif billing_data.status == SubscriptionStatus.trial:
        if not seat:
            seat = SubscriptionSeat(
                subscription_id=sub.id,
                user_id=user.id,
                status=SubscriptionStatus.trial,
                gocardless_subscription_id=None,
                trial_ends_at=user.created_at + timedelta(days=3),
                is_active=True,
            )
            db.add(seat)
        else:
            seat.status = SubscriptionStatus.trial
            seat.trial_ends_at = user.created_at + timedelta(days=3)
            seat.is_active = True
        user.is_active = True
        user.is_trial = True
    else:
        if seat:
            seat.status = billing_data.status
            seat.is_active = False
            seat.next_billing_date = None
            seat.trial_ends_at = None
        user.is_active = False
        user.is_trial = False

    # Audit
    await _write_audit_log(
        db, actor=current_user,
        action="user.billing_changed",
        target_type="user",
        target_id=user_id,
        details={"new_status": billing_data.status.value, "email": user.email},
    )

    await db.commit()
    return {"detail": translator.t("billing_status_updated", lang=lang)}


# System Health 

@router.get("/health", response_model=schemas.SystemHealth)
async def get_system_health(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Get system health metrics — DB, Redis, scheduler."""
    # 1. DB check
    try:
        await db.execute(select(1))
        db_status = "Connected"
    except Exception:
        db_status = "Disconnected"

    # 2. Redis check
    try:
        pong = await redis_client.ping()
        redis_status = "Connected" if pong else "No Response"
    except Exception:
        redis_status = "Disconnected"

    # 3. Scheduler check (APScheduler running in main.py lifespan)
    try:
        from app.main import scheduler
        scheduler_status = "Running" if scheduler.running else "Stopped"
    except Exception:
        scheduler_status = "Unknown"

    overall = "Operational" if db_status == "Connected" and redis_status == "Connected" else "Degraded"

    return {
        "status": overall,
        "database": db_status,
        "redis": redis_status,
        "scheduler": scheduler_status,
        "version": "1.0.0",
    }


#  Maintenance 

@router.post("/recalculate-status", summary="Manually trigger customer status recalculation")
async def trigger_status_recalculation(
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Manually trigger the nightly status recalculation job."""
    changed = await recalculate_customer_statuses()
    return {"detail": translator.t("recalculation_complete", lang=lang), "customers_updated": changed}


# Audit Logs

@router.get("/audit-logs", response_model=List[Any])
async def get_audit_logs(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    skip: int = 0,
    limit: int = 50,
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Super Admin endpoint to view audit logs stored in the database."""
    res = await db.execute(
        select(AuditLog)
        .options(
            selectinload(AuditLog.user),
            selectinload(AuditLog.organization),
        )
        .order_by(AuditLog.created_at.desc())
        .offset(skip)
        .limit(limit)
    )
    logs = res.scalars().all()

    return [
        {
            "id": l.id,
            "action": l.action,
            "target_type": l.target_type,
            "target_id": l.target_id,
            "details": l.details,
            "ip_address": l.ip_address,
            "created_at": l.created_at,
            "org_name": l.organization.name if l.organization else None,
            "user_email": l.user.email if l.user else None,
            "user_full_name": l.user.full_name if l.user else None,
        }
        for l in logs
    ]

@router.get("/analytics/visitors", response_model=schemas.VisitorAnalytics)
async def get_visitor_analytics(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_active_superuser),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Super Admin endpoint to get visitor analytics for the last 30 days."""
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    
    res = await db.execute(
        select(AuditLog)
        .options(selectinload(AuditLog.user))
        .where(AuditLog.action == "site.visit")
        .where(AuditLog.created_at >= thirty_days_ago)
        .order_by(AuditLog.created_at.desc())
    )
    logs = res.scalars().all()
    
    total_visits = len(logs)
    unique_ips = set()
    auth_visits = 0
    anon_visits = 0
    
    daily_counts = {}
    location_counts = {}
    
    recent_visits = []
    
    for i, log in enumerate(logs):
        if log.ip_address:
            unique_ips.add(log.ip_address)
            
        if log.user_id:
            auth_visits += 1
        else:
            anon_visits += 1
            
        # Daily trend
        day_str = log.created_at.strftime("%Y-%m-%d")
        daily_counts[day_str] = daily_counts.get(day_str, 0) + 1
        
        # Geo distribution
        geo = log.details.get("geo", {}) if log.details else {}
        if geo and geo.get("status") == "success":
            loc_key = f"{geo.get('country', 'Unknown')}|{geo.get('city', 'Unknown')}"
            location_counts[loc_key] = location_counts.get(loc_key, 0) + 1
            
        # Keep top 50 recent
        if i < 50:
            recent_visits.append({
                "id": str(log.id),
                "ip_address": log.ip_address,
                "created_at": log.created_at.isoformat(),
                "is_anonymous": log.user_id is None,
                "user_email": log.user.email if log.user else None,
                "city": geo.get("city", "Unknown") if geo else "Unknown",
                "country": geo.get("country", "Unknown") if geo else "Unknown",
                "path": log.details.get("path", "/") if log.details else "/"
            })
            
    # Format daily trend
    daily_trend = [{"date": k, "count": v} for k, v in sorted(daily_counts.items())]
    
    # Format top locations
    top_locations = []
    for loc_key, count in sorted(location_counts.items(), key=lambda x: x[1], reverse=True)[:10]:
        country, city = loc_key.split("|")
        top_locations.append({"country": country, "city": city, "count": count})
        
    return {
        "total_visits": total_visits,
        "unique_ips": len(unique_ips),
        "authenticated_visits": auth_visits,
        "anonymous_visits": anon_visits,
        "top_locations": top_locations,
        "daily_trend": daily_trend,
        "recent_visits": recent_visits
    }
