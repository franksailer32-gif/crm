from fastapi import APIRouter
from app.api.v1.endpoints import auth, orgs, users, customers, visits, admin, routes, notifications, billing, admin_billing, webhooks

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(orgs.router, prefix="/orgs", tags=["organizations"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(customers.router, prefix="/customers", tags=["customers"])
api_router.include_router(visits.router, prefix="/visits", tags=["visits"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(routes.router, prefix="/routes", tags=["routes"])
api_router.include_router(notifications.router, prefix="/notifications", tags=["notifications"])
api_router.include_router(billing.router, prefix="/billing", tags=["billing"])
api_router.include_router(admin_billing.router, prefix="/admin/billing", tags=["admin-billing"])
api_router.include_router(webhooks.router, prefix="/webhooks", tags=["webhooks"])
