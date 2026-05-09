import asyncio
import logging
from datetime import datetime, timezone
from app.db.session import SessionLocal
from app.models.user import User
from app.models.subscription import Subscription, SubscriptionSeat, SubscriptionStatus, EntityType
from sqlalchemy import select

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def force_activate_account(email: str):
    """
    Forcefully activates a user account in the database.
    Sets all trial and expiration dates to 2099 to avoid UI confusion,
    although the core code-level bypass (added separately) is the main safety net.
    """
    async with SessionLocal() as db:
        # 1. Find User
        u_res = await db.execute(select(User).where(User.email == email))
        user = u_res.scalars().first()
        
        if not user:
            logger.error(f"User {email} not found in database.")
            return

        # 2099-12-31 is the standard "perpetual" date
        future_date = datetime(2099, 12, 31, tzinfo=timezone.utc)
        
        # 2. Update User Profile
        user.is_trial = False
        user.trial_ends_at = future_date
        logger.info(f"Updated User {email}: is_trial=False, trial_ends_at=2099")
        
        # 3. Update Individual Subscription Seat
        s_res = await db.execute(select(SubscriptionSeat).where(SubscriptionSeat.user_id == user.id))
        seat = s_res.scalars().first()
        if seat:
            seat.status = SubscriptionStatus.active
            seat.trial_ends_at = None
            seat.next_billing_date = future_date
            logger.info(f"Updated SubscriptionSeat for {email}: status=active, next_billing=2099")

        # 4. Update Organization or Solo Master Subscription
        entity_id = user.org_id if user.org_id else user.id
        entity_type = EntityType.organization if user.org_id else EntityType.solo
        
        sub_res = await db.execute(
            select(Subscription).where(
                Subscription.entity_id == entity_id,
                Subscription.entity_type == entity_type
            )
        )
        subscription = sub_res.scalars().first()
        if subscription:
            subscription.status = SubscriptionStatus.active
            subscription.trial_ends_at = None
            subscription.current_period_end = future_date
            logger.info(f"Updated Master {entity_type} Subscription: status=active, end=2099")

        await db.commit()
        logger.info(f"SUCCESS: Account {email} is now fully sync-activated in the database.")

if __name__ == "__main__":
    # You can change the email here if needed for other demo accounts
    TARGET_EMAIL = "wpd@w-p-d.de"
    asyncio.run(force_activate_account(TARGET_EMAIL))