import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi.staticfiles import StaticFiles

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.limiter import limiter
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Scheduler ──────────────────────────────────────────────────────────────
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the background scheduler on startup, shut it down on exit."""
    # Import here to avoid circular imports at module level
    from app.tasks.reports import recalculate_customer_statuses
    from app.tasks.notifications import check_overdue_notifications, generate_daily_summaries

    # 1. Schedule nightly customer status recalculation at 02:00 UTC
    scheduler.add_job(
        recalculate_customer_statuses,
        CronTrigger(hour=2, minute=0, timezone="UTC"),
        id="nightly_status_recalc",
        replace_existing=True,
    )
    
    # 2. Schedule nightly overdue alerts at 03:00 UTC
    scheduler.add_job(
        check_overdue_notifications,
        CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="nightly_overdue_alerts",
        replace_existing=True,
    )

    # 3. Schedule daily visit summaries at 08:00 Local/Server time
    scheduler.add_job(
        generate_daily_summaries,
        CronTrigger(hour=8, minute=0),
        id="daily_visit_reminders",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("APScheduler started — jobs (Status, Overdue, Daily) scheduled.")

    # [Fix BUG 1] Validate critical billing settings
    if not settings.GOCARDLESS_WEBHOOK_SECRET:
        logger.warning("CRITICAL: GOCARDLESS_WEBHOOK_SECRET is not set! Webhook signature verification will fail, and payments will not be confirmed automatically.")
    
    # Run once immediately on startup (5-second delay to let DB settle)
    async def _startup_run():
        await asyncio.sleep(5)
        logger.info("Running initial status recalculation on startup...")
        try:
            await recalculate_customer_statuses()
            await check_overdue_notifications()
            await generate_daily_summaries()
        except Exception as e:
            logger.error(f"Startup tasks failed: {e}")

    asyncio.create_task(_startup_run())

    yield  # App is running

    scheduler.shutdown(wait=False)
    logger.info(" APScheduler stopped.")


# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global exception caught: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal Server Error"},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=[str(origin).rstrip("/") for origin in settings.BACKEND_CORS_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Trust proxy headers from Nginx
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
async def root():
    return {"message": f"Welcome to {settings.PROJECT_NAME} API"}
