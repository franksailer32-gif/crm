import asyncio
from app.db.session import SessionLocal
from sqlalchemy import select, func
from app.models.customer import Customer

async def count_unmapped():
    async with SessionLocal() as db:
        stmt = select(func.count()).select_from(Customer).where(
            (Customer.latitude == None) | (Customer.longitude == None)
        )
        res = await db.execute(stmt)
        print(f"Total unmapped customers in DB: {res.scalar()}")
        
        # Also check how many have 'DE' country vs others
        stmt2 = select(func.count()).select_from(Customer).where(
            ((Customer.latitude == None) | (Customer.longitude == None)),
            Customer.country == 'DE'
        )
        res2 = await db.execute(stmt2)
        print(f"Total unmapped in Germany (DE): {res2.scalar()}")

if __name__ == "__main__":
    asyncio.run(count_unmapped())
