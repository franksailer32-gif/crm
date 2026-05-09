from typing import Any, List, Optional, Tuple
from uuid import UUID
from datetime import date, datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
import httpx

from app import crud, models
from app.api import deps
from app.core import security
from app.core.i18n import translator
from app.core.redis import redis_client
from app.services.route_service import nearest_neighbour_sort, build_google_maps_url

router = APIRouter()


# Geocoding helper
async def _geocode_text(text: str) -> Optional[Tuple[float, float]]:
    """
    Convert a free-text address to (lat, lng) using Nominatim.
    Results are cached in Redis for 24 hours to avoid redundant API calls.
    Returns None if geocoding fails or text is empty.
    """
    if not text or not text.strip():
        return None

    cache_key = f"geocode:{text.strip().lower()[:200]}"
    try:
        cached = await redis_client.get(cache_key)
        if cached:
            parts = str(cached).split(",")
            if len(parts) == 2:
                return (float(parts[0]), float(parts[1]))
    except Exception:
        pass

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": text.strip(), "format": "json", "limit": 1},
                headers={"User-Agent": "AgriCRM/1.0 (route-planner-geocoder)"},
            )
            if res.status_code == 200:
                data = res.json()
                if data:
                    lat = float(data[0]["lat"])
                    lng = float(data[0]["lon"])
                    # Cache for 24 hours
                    try:
                        await redis_client.setex(cache_key, 86400, f"{lat},{lng}")
                    except Exception:
                        pass
                    return (lat, lng)
    except Exception:
        pass

    return None


#  Schemas (inline — simple enough not to need a separate file) 
class RouteOptimiseRequest(BaseModel):
    customer_ids: List[UUID]
    route_date: date
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    save: bool = True  # Whether to persist the route plan


class RouteResponse(BaseModel):
    id: Optional[UUID] = None
    route_date: date
    waypoints: Optional[list] = None
    start_location: Optional[str] = None
    end_location: Optional[str] = None
    status: Optional[str] = None
    maps_url: Optional[str] = None

    model_config = {"from_attributes": True}


# Helpers 
def _customer_to_dict(c: models.Customer) -> dict:
    return {
        "id": str(c.id),
        "company_name": c.company_name,
        "city": c.city,
        "country": c.country,
        "latitude": c.latitude,
        "longitude": c.longitude,
        "status": c.status.value if c.status else None,
        "contact_person": c.contact_person,
        "phone": c.phone,
    }


#  Endpoints 

@router.post("/optimise", response_model=RouteResponse)
async def optimise_route(
    *,
    db: AsyncSession = Depends(deps.get_db),
    payload: RouteOptimiseRequest,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Take a list of customer IDs, sort them using nearest-neighbour,
    optionally save the plan, and return the optimised waypoints + Google Maps URL.
    """
    # Fetch customers (respecting org/rep scope)
    customers = []
    for cid in payload.customer_ids:
        c = await crud.customer.get(db, id=cid)
        if c is None:
            continue
        # Scope check
        if current_user.org_id:
            if c.org_id != current_user.org_id:
                raise HTTPException(status_code=403, detail=translator.t("customer_not_org", lang=lang))
        else:
            if c.assigned_to != current_user.id:
                raise HTTPException(status_code=403, detail=translator.t("customer_not_assigned", lang=lang))
        customers.append(c)

    if not customers:
        raise HTTPException(status_code=400, detail=translator.t("route_invalid_customers", lang=lang))

    customer_dicts = [_customer_to_dict(c) for c in customers]
    
    # Resolve start/end locations
    start_loc = payload.start_location or current_user.start_location
    end_loc   = payload.end_location   or current_user.end_location

    # Determine start coords from user's saved start_location (geocoded text → coords)
    # Professional geocoding with Redis caching
    start_coords = await _geocode_text(start_loc)

    sorted_customers = nearest_neighbour_sort(customer_dicts, start_coords=start_coords)

    maps_url = build_google_maps_url(sorted_customers, start_loc, end_loc)

    # Persist if requested
    saved_id = None
    if payload.save:
        existing = await crud.route.get_by_date(
            db, rep_id=current_user.id, route_date=payload.route_date
        )
        if existing:
            updated = await crud.route.update(
                db,
                db_obj=existing,
                obj_in={
                    "waypoints": sorted_customers,
                    "start_location": start_loc,
                    "end_location": end_loc,
                    "status": "planned",
                },
            )
            saved_id = updated.id
        else:
            created = await crud.route.create(
                db,
                rep_id=current_user.id,
                org_id=current_user.org_id,
                route_date=payload.route_date,
                waypoints=sorted_customers,
                start_location=start_loc,
                end_location=end_loc,
            )
            saved_id = created.id

    return RouteResponse(
        id=saved_id,
        route_date=payload.route_date,
        waypoints=sorted_customers,
        start_location=start_loc,
        end_location=end_loc,
        status="planned",
        maps_url=maps_url,
    )


@router.get("/my", response_model=List[RouteResponse])
async def get_my_routes(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    limit: int = Query(default=20, le=100),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Get the current user's saved route plans, most recent first."""
    routes = await crud.route.get_multi_by_rep(db, rep_id=current_user.id, limit=limit)
    result = []
    for r in routes:
        url = build_google_maps_url(
            r.waypoints or [],
            r.start_location,
            r.end_location,
        )
        result.append(RouteResponse(
            id=r.id,
            route_date=r.route_date,
            waypoints=r.waypoints,
            start_location=r.start_location,
            end_location=r.end_location,
            status=r.status.value,
            maps_url=url,
        ))
    return result


@router.get("/date/{route_date}", response_model=RouteResponse)
async def get_route_for_date(
    route_date: date,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Fetch the saved route plan for a specific date (current user)."""
    r = await crud.route.get_by_date(db, rep_id=current_user.id, route_date=route_date)
    if not r:
        raise HTTPException(status_code=404, detail=translator.t("route_not_found_date", lang=lang))
    url = build_google_maps_url(r.waypoints or [], r.start_location, r.end_location)
    return RouteResponse(
        id=r.id,
        route_date=r.route_date,
        waypoints=r.waypoints,
        start_location=r.start_location,
        end_location=r.end_location,
        status=r.status.value,
        maps_url=url,
    )


@router.delete("/{route_id}", status_code=204)
async def delete_route(
    route_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> None:
    """Delete a route plan. Only the owner or an org admin can delete."""
    r = await crud.route.get(db, id=route_id)
    if not r:
        raise HTTPException(status_code=404, detail=translator.t("route_not_found_single", lang=lang))
    if r.rep_id != current_user.id and current_user.role not in [
        models.UserRole.org_admin, models.UserRole.super_admin
    ]:
        raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
    await crud.route.remove(db, id=route_id)
