#!/usr/bin/env python3
"""
create_test_user.py
-------------------
Creates a solo test user with a paid/active subscription directly in the database.
Credentials created:
    Email   : test@test.de
    Password: 1234
    Type    : Solo user
    Status  : Email verified, paid/active subscription
"""

import asyncio
import uuid
from datetime import datetime, timezone, timedelta

# ── Database imports ─────────────────────────────────────────────────────────
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import select

# ── App imports ───────────────────────────────────────────────────────────────
import sys, os
# Allow running from project root or backend/ directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.core.config import settings
from app.core.security import get_password_hash
from app.models.user import User, UserRole, UserType
from app.models.subscription import (
    Subscription, SubscriptionSeat, EntityType,
    PlanTier, BillingCycle, SubscriptionStatus
)

# ── Config ────────────────────────────────────────────────────────────────────
TEST_EMAIL    = "test@test.de"
TEST_PASSWORD = "1234"
TEST_NAME     = "Test User"

# ── Database URL (async) ──────────────────────────────────────────────────────
DATABASE_URL = (
    f"postgresql+asyncpg://{settings.POSTGRES_USER}:{settings.POSTGRES_PASSWORD}"
    f"@{settings.POSTGRES_SERVER}/{settings.POSTGRES_DB}"
)

# ─────────────────────────────────────────────────────────────────────────────

async def create_test_user():
    engine = create_async_engine(DATABASE_URL, echo=False)
    AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with AsyncSessionLocal() as session:
        # ── 1. Check if user already exists ───────────────────────────────────
        result = await session.execute(select(User).where(User.email == TEST_EMAIL))
        existing = result.scalar_one_or_none()
        if existing:
            print(f" User '{TEST_EMAIL}' already exists (id={existing.id}). No changes made.")
            await engine.dispose()
            return

        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=365)   # subscription valid for 1 year

        # ── 2. Create the User ────────────────────────────────────────────────
        user = User(
            id=uuid.uuid4(),
            email=TEST_EMAIL,
            hashed_password=get_password_hash(TEST_PASSWORD),
            full_name=TEST_NAME,
            role=UserRole.rep,
            user_type=UserType.solo,
            is_active=True,
            is_email_verified=True,   
            is_trial=False,          
            trial_ends_at=None,
            org_id=None,
        )
        session.add(user)
        await session.flush()  

        # ── 3. Create a Subscription (master billing record) ──────────────────
        subscription = Subscription(
            id=uuid.uuid4(),
            entity_type=EntityType.solo,
            entity_id=user.id,
            plan_tier=PlanTier.starter,
            billing_cycle=BillingCycle.monthly,
            price_per_user=9.99,
            currency="EUR",
            status=SubscriptionStatus.active,   
            trial_ends_at=None,
            current_period_start=now,
            current_period_end=period_end,
            gocardless_subscription_id="MANUAL_TEST",
            gocardless_mandate_id="MANUAL_TEST",
        )
        session.add(subscription)
        await session.flush()   # get subscription.id

        # ── 4. Create a SubscriptionSeat (per-user billing link) ──────────────
        seat = SubscriptionSeat(
            id=uuid.uuid4(),
            subscription_id=subscription.id,
            user_id=user.id,
            status=SubscriptionStatus.active,   
            trial_ends_at=None,
            next_billing_date=period_end,
            is_active=True,
        )
        session.add(seat)

        # ── 5. Commit everything ──────────────────────────────────────────────
        await session.commit()

        print("Test user created successfully!")
        print(f"   Email   : {TEST_EMAIL}")
        print(f"   Password: {TEST_PASSWORD}")
        print(f"   User ID : {user.id}")
        print(f"   Sub ID  : {subscription.id}")
        print(f"   Status  : PAID / ACTIVE (until {period_end.strftime('%Y-%m-%d')})")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(create_test_user())
