import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import SessionLocal
from app import crud, schemas, models
from app.core.config import settings
from app.core.security import get_password_hash

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def init_db() -> None:
    async with SessionLocal() as db:
        # Check if any superuser exists
        result = await db.execute(select(models.User).where(models.User.role == models.UserRole.super_admin))
        user = result.scalars().first()
        if not user:
            logger.info(f"Creating initial superuser {settings.FIRST_SUPERADMIN_EMAIL}")
            # Directly create with super_admin role
            db_obj = models.User(
                email=settings.FIRST_SUPERADMIN_EMAIL,
                hashed_password=get_password_hash(settings.FIRST_SUPERADMIN_PASSWORD),
                full_name="Initial Super Admin",
                role=models.UserRole.super_admin,
                user_type=models.UserType.solo,
                is_active=True,
                is_email_verified=True,
            )
            db.add(db_obj)
            await db.commit()
            logger.info("Superuser created successfully.")
        else:
            logger.info(f"Superuser already exists: {user.email}")

if __name__ == "__main__":
    asyncio.run(init_db())
