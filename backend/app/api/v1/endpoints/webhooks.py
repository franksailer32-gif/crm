from typing import Any, List
import json
from fastapi import APIRouter, Depends, HTTPException, Request, Header, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime, timezone, timedelta

from app.api import deps
from app.core.i18n import translator
from app.core.config import settings
from app.services.gocardless_service import gocardless_service
from app.models.subscription import Subscription, SubscriptionSeat, SubscriptionStatus, PaymentRecord, EntityType
from app.models.user import UserRole, User
from app.core.limiter import limiter
from app.services.rep_deactivation_service import handle_rep_deactivation

router = APIRouter()

@router.post("/gocardless")
@limiter.limit("100/minute")
async def gocardless_webhook(
    request: Request,
    webhook_signature: str = Header(None, alias="Webhook-Signature"),
    db: AsyncSession = Depends(deps.get_db),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Handle GoCardless Webhook events.
    """
    if not webhook_signature:
        raise HTTPException(status_code=400, detail=translator.t("webhook_signature_missing", lang=lang))

    # 1. Get raw body for signature verification
    body_bytes = await request.body()
    
    # 2. Verify signature
    is_valid = gocardless_service.verify_webhook_signature(
        request_body=body_bytes,
        signature=webhook_signature,
        secret=settings.GOCARDLESS_WEBHOOK_SECRET
    )
    
    if not is_valid:
        raise HTTPException(status_code=498, detail=translator.t("webhook_invalid_signature", lang=lang))

    # 3. Parse events
    try:
        payload = json.loads(body_bytes)
        events = payload.get("events", [])
    except Exception:
        raise HTTPException(status_code=400, detail=translator.t("webhook_invalid_json", lang=lang))

    for event in events:
        resource_type = event.get("resource_type")
        action = event.get("action")
        links = event.get("links", {})
        
        # Handle Subscriptions
        if resource_type == "subscriptions":
            sub_id = links.get("subscription")
            if action == "cancelled":
                # Find the subscription or seat
                await handle_subscription_cancelled(db, sub_id, lang=lang)
            elif action == "created":
                # Maybe update local record if not already set
                pass
        
        # Handle Payments (Asynchronous confirmation)
        elif resource_type == "payments":
            payment_id = links.get("payment")
            # GoCardless payment success is typically emitted as `confirmed`
            # (and sometimes `paid_out`). Our old logic only handled `succeeded`,
            # which prevented PaymentRecord creation.
            success_actions = {"confirmed", "paid_out", "paid", "succeeded"}
            if action in success_actions:
                await handle_payment_succeeded(db, payment_id, event, lang=lang)
            elif action == "failed":
                await handle_payment_failed(db, payment_id, event, lang=lang)

        # Handle Mandates
        elif resource_type == "mandates":
            mandate_id = links.get("mandate")
            if action == "cancelled" or action == "expired":
                await handle_mandate_cancelled(db, mandate_id, lang=lang)

    await db.commit()
    return {"status": "ok"}

async def handle_subscription_cancelled(db: AsyncSession, gc_sub_id: str, lang: str = "en"):
    """
    Mark local subscription or seat as cancelled when GoCardless subscription ends.
    """
    # 1. Check if it's a main organization/solo subscription
    result = await db.execute(
        select(Subscription).where(Subscription.gocardless_subscription_id == gc_sub_id)
    )
    db_sub = result.scalars().first()
    if db_sub:
        db_sub.status = SubscriptionStatus.cancelled
    
    # 2. Check if it's an individual seat subscription
    result = await db.execute(
        select(SubscriptionSeat).where(SubscriptionSeat.gocardless_subscription_id == gc_sub_id)
    )
    db_seat = result.scalars().first()
    if db_seat:
        db_seat.status = SubscriptionStatus.cancelled
        db_seat.is_active = False
        db_seat.next_billing_date = None

        # Seat cancellation implies the user should no longer have access.
        # Also clean up their assigned customers / planned visits so the org
        # doesn't end up with "ghost" ownership.
        user = await db.get(User, db_seat.user_id)
        if user:
            user.is_active = False
            # Only reps have customers/planned visits to reassign/cancel.
            if user.role == UserRole.rep:
                await handle_rep_deactivation(
                    db,
                    rep_id=user.id,
                    org_id=user.org_id,
                    reassign_to=None,
                )

async def handle_payment_succeeded(db: AsyncSession, gc_payment_id: str, event: dict, lang: str = "en"):
    """
    Confirmed that money has been collected. Update the audit log.
    Refactored to fix UnboundLocalError and eliminate dead code paths.
    """
    now = datetime.now(timezone.utc)
    links = event.get("links", {})
    gc_sub_id = links.get("subscription")
    gc_mandate_id = links.get("mandate")

    # ── Helper: activate a seat + user after confirmed payment ──────────
    async def _activate_seat(seat: SubscriptionSeat):
        if seat.status == SubscriptionStatus.cancelled:
            return  # Don't reactivate a seat that was explicitly cancelled
        seat.status = SubscriptionStatus.active
        seat.is_active = True
        
        # Determine renewal interval from master subscription if available
        interval_days = 30
        sub_res = await db.execute(
            select(Subscription).where(Subscription.id == seat.subscription_id)
        )
        master_sub = sub_res.scalars().first()
        if master_sub and master_sub.billing_cycle == "yearly":
            interval_days = 365
            
        seat.next_billing_date = now + timedelta(days=interval_days)
        user = await db.get(User, seat.user_id)
        if user:
            user.is_active = True
            user.is_trial = False
            if hasattr(user, "trial_ends_at"):
                user.trial_ends_at = None

    # ── 1. Check if we already have a PaymentRecord for this GC payment ─
    existing_res = await db.execute(
        select(PaymentRecord).where(PaymentRecord.gocardless_payment_id == gc_payment_id)
    )
    record = existing_res.scalars().first()

    if record:
        # Record already exists — just mark it succeeded and sync billing windows
        record.status = "succeeded"
        if gc_sub_id:
            seat_res = await db.execute(
                select(SubscriptionSeat).where(SubscriptionSeat.gocardless_subscription_id == gc_sub_id)
            )
            seat = seat_res.scalars().first()
            if seat:
                await _activate_seat(seat)
            else:
                sub_res = await db.execute(
                    select(Subscription).where(Subscription.gocardless_subscription_id == gc_sub_id)
                )
                sub = sub_res.scalars().first()
                if sub:
                    sub.status = SubscriptionStatus.active
                    sub.current_period_start = now
                    sub.current_period_end = now + timedelta(days=30)
        return

    # ── 2. No existing record — try to match a "processing" record ──────
    seat = None
    if gc_sub_id:
        seat_res = await db.execute(
            select(SubscriptionSeat).where(SubscriptionSeat.gocardless_subscription_id == gc_sub_id)
        )
        seat = seat_res.scalars().first()

    if seat:
        # 2a. Look for a "processing" record we created during seat activation
        proc_res = await db.execute(
            select(PaymentRecord).where(
                PaymentRecord.user_id == seat.user_id,
                PaymentRecord.status == "processing"
            ).order_by(PaymentRecord.created_at.desc())
        )
        processing_record = proc_res.scalars().first()

        if processing_record:
            # Update the existing processing record
            processing_record.status = "succeeded"
            processing_record.gocardless_payment_id = gc_payment_id
            processing_record.description = translator.t("payment_seat_activation_success", lang=lang)
        else:
            # Create a new payment record for this seat renewal
            sub = await db.get(Subscription, seat.subscription_id)
            new_record = PaymentRecord(
                entity_id=sub.entity_id if sub else seat.subscription_id,
                user_id=seat.user_id,
                amount=float(sub.price_per_user) if sub else 7.50,
                currency=sub.currency if sub else "EUR",
                status="succeeded",
                gocardless_payment_id=gc_payment_id,
                description=translator.t("payment_seat_renewal", lang=lang),
            )
            db.add(new_record)

        # Activate the seat
        await _activate_seat(seat)
        return

    # ── 3. No seat found — check master/solo subscription ───────────────
    if gc_sub_id:
        sub_res = await db.execute(
            select(Subscription).where(Subscription.gocardless_subscription_id == gc_sub_id)
        )
        sub = sub_res.scalars().first()
        if sub:
            new_record = PaymentRecord(
                entity_id=sub.entity_id,
                user_id=None,
                amount=float(sub.price_per_user),
                currency=sub.currency,
                status="succeeded",
                gocardless_payment_id=gc_payment_id,
                description=translator.t("payment_renewal", lang=lang),
            )
            db.add(new_record)

            sub.status = SubscriptionStatus.active
            sub.current_period_start = now
            interval_days = 365 if sub.billing_cycle == "yearly" else 30
            sub.current_period_end = now + timedelta(days=interval_days)

            # If this is a solo user, reactivate their account
            if sub.entity_type == EntityType.solo:
                solo_user = await db.get(User, sub.entity_id)
                if solo_user:
                    solo_user.is_active = True
                    solo_user.is_trial = False
                    if hasattr(solo_user, "trial_ends_at"):
                        solo_user.trial_ends_at = None
            return

    # ── 4. Last resort: match by mandate (e.g. ad-hoc payment) ──────────
    if gc_mandate_id:
        mandate_res = await db.execute(
            select(Subscription).where(Subscription.gocardless_mandate_id == gc_mandate_id)
        )
        sub = mandate_res.scalars().first()
        if sub:
            new_record = PaymentRecord(
                entity_id=sub.entity_id,
                amount=float(sub.price_per_user),
                currency=sub.currency,
                status="succeeded",
                gocardless_payment_id=gc_payment_id,
                description=translator.t("payment_confirmed", lang=lang),
            )
            db.add(new_record)

async def handle_payment_failed(db: AsyncSession, gc_payment_id: str, event: dict, lang: str = "en"):
    """
    Handle failed payment collection.
    """
    links = event.get("links", {})
    gc_sub_id = links.get("subscription")

    result = await db.execute(
        select(PaymentRecord).where(PaymentRecord.gocardless_payment_id == gc_payment_id)
    )
    record = result.scalars().first()

    if record:
        record.status = "failed"
    else:
        # Create a payment record if the failure arrives before we created one.
        if gc_sub_id:
            # Seat renewal failure?
            seat_res = await db.execute(
                select(SubscriptionSeat).where(SubscriptionSeat.gocardless_subscription_id == gc_sub_id)
            )
            seat = seat_res.scalars().first()
            if seat:
                sub = await db.get(Subscription, seat.subscription_id)
                if sub:
                    record = PaymentRecord(
                        entity_id=sub.entity_id,
                        user_id=seat.user_id,
                        amount=float(sub.price_per_user),
                        currency=sub.currency,
                        status="failed",
                        gocardless_payment_id=gc_payment_id,
                        description=translator.t("payment_seat_renewal_failed", lang=lang),
                    )
                    db.add(record)
            else:
                # Master subscription renewal failure?
                sub_res = await db.execute(
                    select(Subscription).where(Subscription.gocardless_subscription_id == gc_sub_id)
                )
                sub = sub_res.scalars().first()
                if sub:
                    record = PaymentRecord(
                        entity_id=sub.entity_id,
                        user_id=None,
                        amount=float(sub.price_per_user),
                        currency=sub.currency,
                        status="failed",
                        gocardless_payment_id=gc_payment_id,
                        description=translator.t("payment_renewal_failed", lang=lang),
                    )
                    db.add(record)

    # Deactivate seat/user entitlement on payment failure.
    if gc_sub_id:
        seat_res = await db.execute(
            select(SubscriptionSeat).where(SubscriptionSeat.gocardless_subscription_id == gc_sub_id)
        )
        seat = seat_res.scalars().first()
        if seat:
            seat.status = SubscriptionStatus.past_due
            seat.is_active = False
            seat.next_billing_date = None
            # Allow the admin to try activating again later.
            seat.gocardless_subscription_id = None

            user = await db.get(User, seat.user_id)
            if user:
                user.is_active = False
            return

        # If it isn't a seat subscription, it's likely the master subscription.
        sub_res = await db.execute(
            select(Subscription).where(Subscription.gocardless_subscription_id == gc_sub_id)
        )
        sub = sub_res.scalars().first()
        if sub:
            sub.status = SubscriptionStatus.past_due

async def handle_mandate_cancelled(db: AsyncSession, gc_mandate_id: str, lang: str = "en"):
    """
    If a mandate is cancelled, all associated subscriptions will eventually fail.
    We mark the main subscription as past_due or cancelled.
    """
    result = await db.execute(
        select(Subscription).where(Subscription.gocardless_mandate_id == gc_mandate_id)
    )
    db_sub = result.scalars().first()
    if db_sub:
        db_sub.status = SubscriptionStatus.cancelled

    # Cancel all seat subscriptions that belong to subscriptions using this mandate.
    # (Seat has only gocardless_subscription_id, so we join via subscription_id.)
    seat_res = await db.execute(
        select(SubscriptionSeat)
        .join(Subscription, SubscriptionSeat.subscription_id == Subscription.id)
        .where(Subscription.gocardless_mandate_id == gc_mandate_id)
    )
    seats = seat_res.scalars().all()
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
