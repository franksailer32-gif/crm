import asyncio
import uuid
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.user import User, UserRole, UserType
from app.models.org import Organization, OrgSettings
from app.models.subscription import Subscription, SubscriptionSeat, SubscriptionStatus, EntityType, PlanTier
from app.core.security import get_password_hash

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEMO_PASSWORD = "VisitPro2026!"
TRIAL_DAYS = 365

async def create_demo_users():
    async with SessionLocal() as db:
        logger.info("🚀 Starting Demo User Creation...")
        
        # 1. Password hash
        pwd_hash = get_password_hash(DEMO_PASSWORD)
        trial_end = datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)
        
        # ─── SOLO USER ───────────────────────────────────────────────────────
        solo_email = "demo_solo@visitpro.de"
        existing_solo = await db.execute(select(User).where(User.email == solo_email))
        if not existing_solo.scalars().first():
            solo_user = User(
                email=solo_email,
                hashed_password=pwd_hash,
                full_name="Demo Solo Professional",
                role=UserRole.rep,
                user_type=UserType.solo,
                is_active=True,
                is_email_verified=True,
                is_trial=True,
                trial_ends_at=trial_end
            )
            db.add(solo_user)
            await db.flush()
            
            # Create a mock subscription for the solo user so the UI looks "Active"
            solo_sub = Subscription(
                entity_type=EntityType.solo,
                entity_id=solo_user.id,
                status=SubscriptionStatus.active,
                plan_tier=PlanTier.starter,
                price_per_user=7.90,
                trial_ends_at=trial_end
            )
            db.add(solo_sub)
            await db.flush()
            
            solo_seat = SubscriptionSeat(
                subscription_id=solo_sub.id,
                user_id=solo_user.id,
                status=SubscriptionStatus.active,
                is_active=True,
                trial_ends_at=trial_end
            )
            db.add(solo_seat)
            logger.info(f"✅ Created Solo User: {solo_email}")
        else:
            logger.info(f"⏭️  Solo User {solo_email} already exists. Skipping.")

        # ─── ORG ADMIN & ORG ─────────────────────────────────────────────────
        admin_email = "demo_admin@visitpro.de"
        existing_admin = await db.execute(select(User).where(User.email == admin_email))
        admin_user = existing_admin.scalars().first()
        
        if not admin_user:
            # Create Organization
            demo_org = Organization(
                name="Demo Corp",
                slug="demo-corp",
                is_active=True
            )
            db.add(demo_org)
            await db.flush()
            
            # Create Org Settings
            org_settings = OrgSettings(org_id=demo_org.id)
            db.add(org_settings)
            
            # Create Admin User
            admin_user = User(
                email=admin_email,
                hashed_password=pwd_hash,
                full_name="Demo Org Administrator",
                role=UserRole.org_admin,
                user_type=UserType.company_member,
                org_id=demo_org.id,
                is_active=True,
                is_email_verified=True,
                is_trial=True,
                trial_ends_at=trial_end
            )
            db.add(admin_user)
            await db.flush()
            
            # Set owner
            demo_org.owner_id = admin_user.id
            
            # Create Org Subscription
            org_sub = Subscription(
                entity_type=EntityType.organization,
                entity_id=demo_org.id,
                status=SubscriptionStatus.active,
                plan_tier=PlanTier.starter,
                price_per_user=7.50,
                trial_ends_at=trial_end
            )
            db.add(org_sub)
            await db.flush()
            
            # Seat for Admin
            admin_seat = SubscriptionSeat(
                subscription_id=org_sub.id,
                user_id=admin_user.id,
                status=SubscriptionStatus.active,
                is_active=True,
                trial_ends_at=trial_end
            )
            db.add(admin_seat)
            
            logger.info(f"✅ Created Org Admin & Org: {admin_email}")

            # ─── ORG MEMBERS (REPS) ──────────────────────────────────────────
            for i in range(1, 3):
                rep_email = f"demo_rep{i}@visitpro.de"
                rep_user = User(
                    email=rep_email,
                    hashed_password=pwd_hash,
                    full_name=f"Demo Sales Rep {i}",
                    role=UserRole.rep,
                    user_type=UserType.company_member,
                    org_id=demo_org.id,
                    is_active=True,
                    is_email_verified=True,
                    is_trial=True,
                    trial_ends_at=trial_end
                )
                db.add(rep_user)
                await db.flush()
                
                # Seat for Rep
                rep_seat = SubscriptionSeat(
                    subscription_id=org_sub.id,
                    user_id=rep_user.id,
                    status=SubscriptionStatus.active,
                    is_active=True,
                    trial_ends_at=trial_end
                )
                db.add(rep_seat)
                logger.info(f"✅ Created Org Member: {rep_email}")
        else:
            logger.info(f"⏭️  Org Admin {admin_email} already exists. Skipping.")

        await db.commit()
        logger.info("✨ Demo User Creation Complete! ✨")
        logger.info(f"Solo Login: {solo_email} / {DEMO_PASSWORD}")
        logger.info(f"Org Login:  {admin_email} / {DEMO_PASSWORD}")

if __name__ == "__main__":
    asyncio.run(create_demo_users())
