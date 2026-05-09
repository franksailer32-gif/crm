import logging
from typing import Any, List, Optional
from uuid import UUID
import json
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta, timezone
from fastapi.encoders import jsonable_encoder


logger = logging.getLogger(__name__)

from app.api import deps
from app.core.i18n import translator
from app.models.user import User, UserRole, UserType
from app.models.subscription import Subscription, SubscriptionSeat, SubscriptionStatus, EntityType, PlanTier, BillingCycle, PaymentRecord
from app.services.gocardless_service import gocardless_service
from app.core.config import settings
from app.core.limiter import limiter
from app.core.redis import redis_client
from app.core.billing import calculate_price_per_user, get_org_tier_rate, BillingCycle as BillingCycleType

router = APIRouter()

REDIRECT_CTX_KEY_PREFIX = "gocardless_redirect_ctx:"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _trial_end_for_user(user: User) -> datetime:
    # Prefer stored trial_ends_at (set at user creation / activation flows),
    # but fall back to a 3-day trial computed from creation.
    if user.trial_ends_at:
        return user.trial_ends_at
    return user.created_at + timedelta(days=3)


async def _get_org_subscription(db: AsyncSession, *, org_id: UUID) -> Optional[Subscription]:
    res = await db.execute(
        select(Subscription).where(
            Subscription.entity_id == org_id,
            Subscription.entity_type == EntityType.organization,
        )
    )
    return res.scalars().first()

@router.get("/me")
async def get_my_subscription(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get current user or organization subscription status.
    """
    # Also fetch individual seat for members
    res = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id == current_user.id)
    )
    seat = res.scalars().first()

    entity_id = current_user.org_id if current_user.org_id else current_user.id
    entity_type = EntityType.organization if current_user.org_id else EntityType.solo

    # Fetch entity-level subscription
    res = await db.execute(
        select(Subscription).where(
            Subscription.entity_id == entity_id,
            Subscription.entity_type == entity_type,
        )
    )
    subscription = res.scalars().first()

    trial_end = _trial_end_for_user(current_user)
    now = _utc_now()

    if not subscription:
        # Normalize "expired" -> past_due to keep API status consistent.
        in_trial = now.replace(tzinfo=None) < trial_end.replace(tzinfo=None)
        
        # New users getting status check before setup will see the new rates
        default_price = calculate_price_per_user(
            entity_type=entity_type,
            user_count=1,
            use_new_pricing=True
        )
        
        return {
            "status": "trial" if in_trial else SubscriptionStatus.past_due.value,
            "trial_ends_at": trial_end,
            "entity_type": entity_type.value,
            "plan_tier": PlanTier.starter.value,
            "price_per_user": default_price,
            "seat": seat,
        }

    # Convert to dict to add seat
    sub_data = jsonable_encoder(subscription)
    sub_data["seat"] = jsonable_encoder(seat) if seat else None

    # Sync: If user is a member, the primary status should be their seat status
    from app.models.user import UserRole
    if current_user.org_id and current_user.role != UserRole.org_admin:
        sub_data["status"] = seat.status.value if seat else "trial"
        
    return sub_data

@router.post("/setup")
@limiter.limit("10/hour")
async def setup_billing(
    request: Request,
    billing_cycle: Optional[str] = Query("monthly"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Create a GoCardless redirect flow to set up a mandate.
    """
    # Only Org Admins or Solo users can manage billing
    if current_user.org_id and current_user.role != UserRole.org_admin:
        raise HTTPException(status_code=403, detail=translator.t("org_admin_billing_only", lang=lang))

    description = f"VisitPro Subscription for {current_user.email}"
    session_token = str(current_user.id) # Simple session token
    
    try:
        flow = gocardless_service.create_redirect_flow(
            session_token=session_token,
            description=description
        )
        
        # Save flow ID to subscription record (create if not exists)
        entity_id = current_user.org_id if current_user.org_id else current_user.id
        entity_type = EntityType.organization if current_user.org_id else EntityType.solo
        
        result = await db.execute(
            select(Subscription).where(Subscription.entity_id == entity_id)
        )
        subscription = result.scalars().first()
        
        if not subscription:
            price = calculate_price_per_user(
                entity_type=entity_type,
                user_count=1,
                use_new_pricing=True
            )
            subscription = Subscription(
                entity_type=entity_type,
                entity_id=entity_id,
                status=SubscriptionStatus.trial,
                price_per_user=price,
                use_new_pricing=True,
                gocardless_redirect_flow_id=flow.id
            )
            db.add(subscription)
        else:
            subscription.gocardless_redirect_flow_id = flow.id
            subscription.billing_cycle = billing_cycle
            
        await db.commit()

        # Store context for completion
        ctx = {
            "type": "main_setup",
            "billing_cycle": billing_cycle,
            "entity_type": entity_type.value
        }
        await redis_client.setex(
            f"{REDIRECT_CTX_KEY_PREFIX}{flow.id}",
            60 * 60 * 24, # 24h
            json.dumps(ctx)
        )

        return {"redirect_url": flow.redirect_url}
        
    except Exception as e:
        logger.exception("Billing setup failed:")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/complete")
@limiter.limit("5/hour")
async def complete_billing(
    request: Request,
    redirect_flow_id: str = Query(..., alias="redirect_flow_id"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Complete the redirect flow and create the subscription.
    """
    session_token = str(current_user.id)

    try:
        # 1. Complete flow in GoCardless (returns mandate + customer IDs)
        flow = gocardless_service.complete_redirect_flow(redirect_flow_id, session_token)

        # 2. Determine whether this completion was for a seat activation
        ctx_raw = await redis_client.get(f"{REDIRECT_CTX_KEY_PREFIX}{redirect_flow_id}")
        ctx = json.loads(ctx_raw) if ctx_raw else None
        if ctx_raw:
            await redis_client.delete(f"{REDIRECT_CTX_KEY_PREFIX}{redirect_flow_id}")

        now = _utc_now()

        if ctx and ctx.get("type") == "seat_activation":
            # ------------------------------------------------ seat completion
            target_user_id = UUID(str(ctx["target_user_id"]))
            org_id = UUID(str(ctx["org_id"]))

            # Ensure the current user is an org admin for the same org.
            if current_user.org_id != org_id:
                raise HTTPException(status_code=403, detail=translator.t("not_authorized_seat_activation", lang=lang))

            # Ensure org subscription exists (or create it)
            org_sub = await _get_org_subscription(db, org_id=org_id)
            if not org_sub:
                org_sub = Subscription(
                    entity_type=EntityType.organization,
                    entity_id=org_id,
                    status=SubscriptionStatus.active,
                    plan_tier=PlanTier.starter,
                    price_per_user=7.50,
                    gocardless_mandate_id=flow.links.mandate,
                    gocardless_customer_id=flow.links.customer,
                    gocardless_redirect_flow_id=redirect_flow_id,
                )
                db.add(org_sub)
                await db.flush()
            else:
                org_sub.gocardless_mandate_id = flow.links.mandate
                org_sub.gocardless_customer_id = flow.links.customer
                org_sub.status = SubscriptionStatus.active

            # Determine price based on current tier
            seat_count_res = await db.execute(
                select(func.count(SubscriptionSeat.id)).where(
                    SubscriptionSeat.subscription_id == org_sub.id,
                    SubscriptionSeat.is_active == True
                )
            )
            active_seats = seat_count_res.scalar() or 0
            
            price_val = calculate_price_per_user(
                entity_type=EntityType.organization,
                user_count=active_seats + 1,
                use_new_pricing=org_sub.use_new_pricing
            )

            # Create the actual GoCardless recurring subscription for this seat.
            gc_sub = gocardless_service.create_subscription(
                mandate_id=flow.links.mandate,
                amount=int(price_val * 100),
                currency="EUR",
                interval="monthly",
            )

            seat_res = await db.execute(
                select(SubscriptionSeat).where(SubscriptionSeat.user_id == target_user_id)
            )
            seat = seat_res.scalars().first()
            if not seat:
                seat = SubscriptionSeat(
                    subscription_id=org_sub.id,
                    user_id=target_user_id,
                    # Strict flow: do NOT activate access until we receive
                    # GoCardless `payments.succeeded` webhook for the seat.
                    status=SubscriptionStatus.past_due,
                    gocardless_subscription_id=gc_sub.id,
                    is_active=False,
                    next_billing_date=None,
                )
                db.add(seat)
            else:
                seat.subscription_id = org_sub.id
                seat.status = SubscriptionStatus.past_due
                seat.gocardless_subscription_id = gc_sub.id
                seat.next_billing_date = None
                seat.is_active = False
            await db.commit()

            # Record initial "Processing" payment in the audit log
            processing_record = PaymentRecord(
                entity_id=org_sub.entity_id,
                user_id=target_user_id,
                amount=7.50, # Price per user
                currency="EUR",
                status="processing",
                gocardless_payment_id=None,
                description=translator.t("payment_seat_activation_processing", lang=lang),
            )
            db.add(processing_record)
            await db.commit()

            return {
                "status": "pending",
                "message": translator.t("seat_authorization_pending", lang=lang),
            }

        # --------------------------------------------------------- main completion
        entity_id = current_user.org_id if current_user.org_id else current_user.id
        entity_type = EntityType.organization if current_user.org_id else EntityType.solo

        result = await db.execute(
            select(Subscription).where(
                Subscription.entity_id == entity_id,
                Subscription.entity_type == entity_type,
            )
        )
        subscription = result.scalars().first()
        if not subscription:
            raise HTTPException(status_code=404, detail=translator.t("subscription_record_not_found", lang=lang))

        subscription.gocardless_mandate_id = flow.links.mandate
        subscription.gocardless_customer_id = flow.links.customer
        subscription.status = SubscriptionStatus.active

        # Dynamic Price Calculation
        seat_count_res = await db.execute(
            select(func.count(SubscriptionSeat.id)).where(
                SubscriptionSeat.subscription_id == subscription.id,
                SubscriptionSeat.is_active == True
            )
        )
        active_seats = seat_count_res.scalar() or 0
        
        # Check context for interval (Solo Annual support)
        billing_cycle = ctx.get("billing_cycle", "monthly") if ctx else "monthly"
        if subscription.entity_type == EntityType.solo:
            subscription.billing_cycle = billing_cycle

        price_val = calculate_price_per_user(
            entity_type=subscription.entity_type,
            user_count=active_seats + 1,
            billing_cycle=subscription.billing_cycle,
            use_new_pricing=subscription.use_new_pricing
        )
        
        subscription.price_per_user = price_val

        gc_sub = gocardless_service.create_subscription(
            mandate_id=flow.links.mandate,
            amount=int(price_val * 100),
            currency="EUR",
            interval=subscription.billing_cycle,
        )

        subscription.gocardless_subscription_id = gc_sub.id
        subscription.current_period_start = now
        subscription.current_period_end = now + timedelta(days=30)

        # Restore access on the user profile
        current_user.is_trial = False
        current_user.is_active = True
        db.add(current_user)

        # Create/Update Subscription Seat for access middleware
        seat_res = await db.execute(
            select(SubscriptionSeat).where(SubscriptionSeat.user_id == current_user.id)
        )
        seat = seat_res.scalars().first()
        if not seat:
            seat = SubscriptionSeat(
                subscription_id=subscription.id,
                user_id=current_user.id,
                status=SubscriptionStatus.active,
                gocardless_subscription_id=gc_sub.id,
                next_billing_date=now + timedelta(days=30),
                is_active=True,
            )
            db.add(seat)
        else:
            seat.status = SubscriptionStatus.active
            seat.subscription_id = subscription.id
            seat.gocardless_subscription_id = gc_sub.id
            seat.next_billing_date = now + timedelta(days=30)
            seat.is_active = True

        await db.commit()

        # Record initial "Processing" payment for master subscription
        processing_record = PaymentRecord(
            entity_id=subscription.entity_id,
            user_id=None,
            amount=subscription.price_per_user,
            currency=subscription.currency,
            status="processing",
            gocardless_payment_id=None,
            description=translator.t("payment_setup_processing", lang=lang),
        )
        db.add(processing_record)
        await db.commit()

        return {"status": "active", "message": translator.t("subscription_activation_success", lang=lang)}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Billing completion failed:")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/members")
async def get_billing_members(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get all organization members and their current seat status.
    """
    from app.models.user import User as UserModel
    
    # 1. Get all users in the org
    result = await db.execute(
        select(UserModel).where(UserModel.org_id == current_user.org_id)
    )
    members = result.scalars().all()
    
    # 2. Get all existing seats
    user_ids = [m.id for m in members]
    if not user_ids:
        return []
        
    result = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id.in_(user_ids))
    )
    seats = {s.user_id: s for s in result.scalars().all()}
    
    # 3. Combine
    output = []
    for m in members:
        seat = seats.get(m.id)
        # Default status if no seat record
        trial_end = _trial_end_for_user(m)
        now = _utc_now()
        status = "trial" if now.replace(tzinfo=None) < trial_end.replace(tzinfo=None) else SubscriptionStatus.past_due.value

        output.append({
            "user_id": m.id,
            "full_name": m.full_name,
            "email": m.email,
            "role": m.role,
            "joined_at": seat.joined_at if seat else m.created_at,
            "seat_status": seat.status if seat else status,
            "trial_ends_at": seat.trial_ends_at if seat else trial_end,
            "next_billing": seat.next_billing_date if seat else None,
            "has_gocardless_subscription_id": bool(getattr(seat, "gocardless_subscription_id", None)),
            "activation_pending": (
                bool(getattr(seat, "gocardless_subscription_id", None))
                and seat.status != SubscriptionStatus.active
                and (not seat.is_active if seat else False)
                and (not seat.next_billing_date if seat else True)
            ),
            "seat_is_active": seat.is_active if seat else False,
        })
        
    return output

@router.post("/activate-seat/{user_id}")
@limiter.limit("20/hour")
async def activate_member_seat(
    request: Request,
    user_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Manually activate a member's seat. 
    Always opens a GoCardless window for authorization.
    """
    # 1. Verify user belongs to org
    from app.models.user import User as UserModel
    target_user_res = await db.execute(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.org_id == current_user.org_id,
        )
    )
    target_user = target_user_res.scalars().first()
    if not target_user:
        raise HTTPException(status_code=404, detail=translator.t("org_member_not_found", lang=lang))

    # 2. Verify Admin is ACTIVE or in Grace Period (Direct Debit window)
    admin_seat_res = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id == current_user.id)
    )
    admin_seat = admin_seat_res.scalars().first()
    
    is_admin_ready = False
    if admin_seat:
        if admin_seat.status == SubscriptionStatus.active:
            is_admin_ready = True
        elif admin_seat.status == SubscriptionStatus.past_due:
            # Allow 7 day grace period for DD processing
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            created_at = admin_seat.created_at.replace(tzinfo=None) if hasattr(admin_seat, "created_at") and admin_seat.created_at else current_user.created_at.replace(tzinfo=None)
            if now < (created_at + timedelta(days=7)):
                is_admin_ready = True

    if not is_admin_ready:
        raise HTTPException(
            status_code=403,
            detail=translator.t("admin_seat_required_activation", lang=lang),
        )

    # 3. Check if the Organization already has a Mandate ID on file
    from app.models.subscription import Subscription, EntityType
    res = await db.execute(
        select(Subscription).where(
            Subscription.entity_id == current_user.org_id,
            Subscription.entity_type == EntityType.organization,
        )
    )
    master_sub = res.scalars().first()

    if master_sub and master_sub.gocardless_mandate_id:
        # ONE-CLICK ACTIVATION: Reuse existing mandate to create a new subscription
        try:
            # Determine price based on current tier
            seat_count_res = await db.execute(
                select(func.count(SubscriptionSeat.id)).where(
                    SubscriptionSeat.subscription_id == master_sub.id,
                    SubscriptionSeat.is_active == True
                )
            )
            active_seats = seat_count_res.scalar() or 0
            
            price_val = calculate_price_per_user(
                entity_type=EntityType.organization,
                user_count=active_seats + 1,
                use_new_pricing=master_sub.use_new_pricing
            )

            gc_sub = gocardless_service.create_subscription(
                mandate_id=master_sub.gocardless_mandate_id,
                amount=int(price_val * 100),
                currency=master_sub.currency,
                interval="monthly", # Default for seats
            )
            
            # Find or create seat (upsert pattern to avoid duplicate rows)
            seat_res = await db.execute(
                select(SubscriptionSeat).where(SubscriptionSeat.user_id == user_id)
            )
            seat = seat_res.scalars().first()
            if not seat:
                seat = SubscriptionSeat(
                    subscription_id=master_sub.id,
                    user_id=user_id,
                )
                db.add(seat)
            else:
                seat.subscription_id = master_sub.id
            
            seat.gocardless_subscription_id = gc_sub.id
            # Set to past_due until GoCardless confirms payment (consistent with redirect flow)
            seat.status = SubscriptionStatus.past_due
            seat.is_active = False
            seat.next_billing_date = None
            
            # Update master sub price_per_user if it changed due to tier shift
            if master_sub.use_new_pricing:
                master_sub.price_per_user = price_val

            await db.commit()

            # Record initial "Processing" payment in the audit log for transparency
            processing_record = PaymentRecord(
                entity_id=master_sub.entity_id,
                user_id=user_id,
                amount=price_val,
                currency=master_sub.currency,
                status="processing",
                gocardless_payment_id=None, # Not known until GoCardless creates the payment resource
                description=translator.t("payment_seat_activation_processing", lang=lang),
            )
            db.add(processing_record)
            await db.commit()

            return {"status": "activated", "message": translator.t("seat_activated_success", lang=lang)}
            
        except Exception as e:
            # If mandate fails, fall back to the redirect flow
            logger.warning("Mandate reuse failed for user %s: %s", user_id, e)

    # 4. Fallback: Start GoCardless redirect flow for mandate authorization.
    # IMPORTANT:
    # BillingTab always calls POST /billing/complete, which completes the redirect
    # using `session_token=str(current_user.id)`. Therefore the redirect flow must
    # be created with the same session_token so completion succeeds.
    redirect_flow = gocardless_service.create_redirect_flow(
        session_token=str(current_user.id),
        description=f"Seat activation for {target_user.email}",
    )

    # 5. Store redirect context so completion can activate the correct seat.
    ctx = {
        "type": "seat_activation",
        "target_user_id": str(user_id),
        "org_id": str(current_user.org_id),
    }
    await redis_client.setex(
        f"{REDIRECT_CTX_KEY_PREFIX}{redirect_flow.id}",
        60 * 60 * 24,  # 24h
        json.dumps(ctx),
    )

    return {"redirect_url": redirect_flow.redirect_url}
    
@router.post("/cancel-seat/{user_id}")
async def cancel_member_seat(
    user_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_org_admin),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Deactivate a member's seat and cancel their recurring subscription in GoCardless.
    """
    # 0. Prevent admin from cancelling their own seat (would lock out entire org)
    if user_id == current_user.id:
        raise HTTPException(
            status_code=400,
            detail=translator.t("cannot_cancel_own_seat", lang=lang),
        )

    # 1. Verify user belongs to org
    from app.models.user import User as UserModel
    target_user_res = await db.execute(
        select(UserModel).where(
            UserModel.id == user_id,
            UserModel.org_id == current_user.org_id,
        )
    )
    target_user = target_user_res.scalars().first()
    if not target_user:
        raise HTTPException(status_code=404, detail=translator.t("org_member_not_found", lang=lang))

    # 2. Get the seat
    result = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.user_id == user_id)
    )
    seat = result.scalars().first()
    if not seat:
        # If no seat record exists, we still deactivate the user as requested.
        target_user.is_active = False
        await db.commit()
        return {"status": "cancelled"}

    # 3. Cancel GoCardless subscription if it exists
    if seat.gocardless_subscription_id:
        from fastapi.concurrency import run_in_threadpool
        try:
            await run_in_threadpool(gocardless_service.cancel_subscription, seat.gocardless_subscription_id)
        except Exception as e:
            # Mandate/Subscription might already be cancelled or connection failed
            logger.warning("GoCardless seat cancellation warning for user %s: %s", user_id, e)

    # 4. Local deactivation
    seat.status = SubscriptionStatus.cancelled
    seat.is_active = False
    seat.next_billing_date = None
    seat.gocardless_subscription_id = None # Clear this so they can be re-activated with a new sub later if needed

    target_user.is_active = False
    
    # Trigger rep deactivation service for CRM data cleanup
    if target_user.role == UserRole.rep:
        from app.services.rep_deactivation_service import handle_rep_deactivation
        await handle_rep_deactivation(
            db,
            rep_id=target_user.id,
            org_id=target_user.org_id,
            reassign_to=None,
        )

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to commit seat cancellation for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=translator.t("cancellation_failed", lang=lang))
    return {"status": "cancelled"}

@router.get("/history")
async def get_payment_history(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get payment history for the current organization or solo user.
    """
    entity_id = current_user.org_id if current_user.org_id else current_user.id
    
    result = await db.execute(
        select(PaymentRecord)
        .options(selectinload(PaymentRecord.user))
        .where(PaymentRecord.entity_id == entity_id)
        .order_by(PaymentRecord.created_at.desc())
    )
    records = result.scalars().all()
    
    output = []
    for r in records:
        output.append({
            "id": r.id,
            "amount": float(r.amount),
            "date": r.created_at,
            "description": r.description,
            "status": r.status,
            # Present for seat-billing records so admins can see "which member" was billed.
            "user_id": r.user_id,
            "user_full_name": (r.user.full_name if r.user else None),
            "user_email": (r.user.email if r.user else None),
        })
        
    return output


@router.post("/cancel-mandate")
async def cancel_current_mandate(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Cancel the current GoCardless mandate for the current user/org.
    This stops future Direct Debit collections.
    """
    # Only admins/super_admin can cancel org billing; solo users can cancel their own.
    if current_user.org_id:
        if current_user.role not in [UserRole.org_admin, UserRole.super_admin]:
            raise HTTPException(status_code=403, detail=translator.t("org_admin_only_cancel", lang=lang))
        entity_id = current_user.org_id
        entity_type = EntityType.organization
    else:
        entity_id = current_user.id
        entity_type = EntityType.solo

    sub_res = await db.execute(
        select(Subscription).where(
            Subscription.entity_id == entity_id,
            Subscription.entity_type == entity_type,
        )
    )
    subscription = sub_res.scalars().first()
    if not subscription or not subscription.gocardless_mandate_id:
        raise HTTPException(status_code=400, detail=translator.t("no_mandate_found", lang=lang))

    # Cancel in GoCardless (Synchronous library call, run in threadpool)
    from fastapi.concurrency import run_in_threadpool
    try:
        await run_in_threadpool(gocardless_service.cancel_mandate, subscription.gocardless_mandate_id)
    except Exception as e:
        # We log and continue if mandate is already cancelled or connection fails,
        # as we want to ensure local state is updated to stop usage.
        logger.warning("GoCardless mandate cancellation warning for entity %s: %s", entity_id, e)

    # Reflect cancellation locally immediately (webhooks will also reconcile)
    subscription.status = SubscriptionStatus.cancelled

    seat_res = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.subscription_id == subscription.id)
    )
    seats = seat_res.scalars().all()

    from app.services.rep_deactivation_service import handle_rep_deactivation

    for seat in seats:
        seat.status = SubscriptionStatus.cancelled
        seat.is_active = False
        seat.next_billing_date = None

        user = await db.get(User, seat.user_id)
        if user:
            user.is_active = False
            if user.role == UserRole.rep:
                await handle_rep_deactivation(
                    db,
                    rep_id=user.id,
                    org_id=user.org_id,
                    reassign_to=None,
                )

    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        logger.error(f"Failed to commit mandate cancellation for entity {entity_id}: {e}")
        raise HTTPException(status_code=500, detail=translator.t("cancellation_failed", lang=lang))
    return {"status": "cancelled"}
