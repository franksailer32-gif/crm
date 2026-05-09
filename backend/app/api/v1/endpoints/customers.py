from typing import Any, List, Optional
import logging

logger = logging.getLogger(__name__)

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, Query, status, Body
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from datetime import date

from app import crud, models, schemas
from app.models.visit import Visit, VisitStatus
from app.api import deps
from app.core.i18n import translator
import io
import asyncio
from app.core.redis import redis_client
from fastapi import BackgroundTasks, UploadFile, File
import httpx
from app.core.audit import log_activity

router = APIRouter()

@router.get("/", response_model=schemas.PaginatedCustomerResponse)
async def read_customers(
    db: AsyncSession = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    search: Optional[str] = None,
    status: Optional[models.customer.CustomerStatus] = None,
    assigned_to: Optional[UUID] = None,
    due_only: bool = Query(False),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Retrieve customers with pagination and total count.
    """
    if current_user.role == models.UserRole.super_admin:
         raise HTTPException(
            status_code=403, 
            detail=translator.t("super_admin_customer_access", lang=lang)
        )
    
    if current_user.org_id is None:
        assigned_to = current_user.id
        
    # Get total count for pagination
    total = await crud.customer.get_count_by_org(
        db, 
        org_id=current_user.org_id,
        search=search,
        status=status,
        assigned_to=assigned_to,
        due_only=due_only
    )

    customers = await crud.customer.get_multi_by_org(
        db, 
        org_id=current_user.org_id, 
        skip=skip, 
        limit=limit,
        search=search,
        status=status,
        assigned_to=assigned_to,
        due_only=due_only
    )

    if customers:
        customer_ids = [c.id for c in customers]
        today = date.today()
        stmt = (
            select(Visit.customer_id, func.min(Visit.scheduled_date).label("next_date"))
            .where(
                Visit.customer_id.in_(customer_ids),
                Visit.status == VisitStatus.planned,
                Visit.scheduled_date >= today
            )
            .group_by(Visit.customer_id)
        )
        res = await db.execute(stmt)
        planned_map = {row.customer_id: row.next_date for row in res}
        
        for c in customers:
            c.next_planned_visit_date = planned_map.get(c.id)

    return {"items": customers, "total": total}

@router.post("/", response_model=schemas.Customer)
async def create_customer(
    *,
    db: AsyncSession = Depends(deps.get_db),
    customer_in: schemas.CustomerCreate,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Create a new customer linked to the current user's organization.
    """
        
    customer = await crud.customer.create_with_org(
        db, obj_in=customer_in, org_id=current_user.org_id, user_id=current_user.id
    )
    
    await log_activity(
        db, 
        actor=current_user, 
        action="customer.created", 
        target_type="customer", 
        target_id=customer.id,
        details={"company_name": customer.company_name}
    )
    await db.commit()
    
    return customer

@router.put("/{id}", response_model=schemas.Customer)
async def update_customer(
    *,
    db: AsyncSession = Depends(deps.get_db),
    id: UUID,
    customer_in: schemas.CustomerUpdate,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Update a customer.
    """
    customer = await crud.customer.get(db, id=id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))
        
    if current_user.role != models.UserRole.super_admin:
        if current_user.org_id is None:
            if customer.assigned_to != current_user.id:
                raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        else:
            if customer.org_id != current_user.org_id:
                raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        
    customer = await crud.customer.update(db, db_obj=customer, obj_in=customer_in)
    return customer

@router.get("/{id}", response_model=schemas.Customer)
async def read_customer(
    *,
    db: AsyncSession = Depends(deps.get_db),
    id: UUID,
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get customer by ID.
    """
    customer = await crud.customer.get(db, id=id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))
        
    if current_user.role != models.UserRole.super_admin:
        if current_user.org_id is None:
            if customer.assigned_to != current_user.id:
                raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        else:
            if customer.org_id != current_user.org_id:
                raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
    
    # NEW: Fetch next planned visit for this specific customer
    today = date.today()
    stmt = (
        select(func.min(Visit.scheduled_date))
        .where(
            Visit.customer_id == customer.id,
            Visit.status == VisitStatus.planned,
            Visit.scheduled_date >= today
        )
    )
    next_date = await db.scalar(stmt)
    customer.next_planned_visit_date = next_date
        
    return customer

@router.delete("/bulk", status_code=200)
async def bulk_delete_customers(
    *,
    db: AsyncSession = Depends(deps.get_db),
    bulk_in: schemas.CustomerBulkDelete = Body(...),
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Delete multiple customers at once.
    Accepts a JSON body: { "ids": ["uuid1", "uuid2", ...] }
    Only deletes customers that belong to the current user/org.
    """
    ids = bulk_in.ids
    if not ids:
        raise HTTPException(status_code=422, detail=translator.t("no_ids_provided", lang=lang))

    deleted_count = 0
    skipped_count = 0

    for customer_id in ids:
        customer = await crud.customer.get(db, id=customer_id)
        if not customer:
            skipped_count += 1
            continue

        # Ownership check
        if current_user.role != models.UserRole.super_admin:
            if current_user.org_id is None:
                if customer.assigned_to != current_user.id:
                    skipped_count += 1
                    continue
            else:
                if customer.org_id != current_user.org_id:
                    skipped_count += 1
                    continue

        await crud.customer.remove(db, id=customer_id)
        await log_activity(
            db,
            actor=current_user,
            action="customer.deleted",
            target_type="customer",
            target_id=customer_id,
            details={"company_name": customer.company_name, "bulk": True}
        )
        deleted_count += 1

    await db.commit()

    return {
        "deleted": deleted_count,
        "skipped": skipped_count,
        "message": f"{deleted_count} customer(s) deleted successfully."
    }


@router.delete("/{id}", response_model=schemas.Customer)
async def delete_customer(
    *,
    db: AsyncSession = Depends(deps.get_db),
    id: UUID,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Delete a customer.
    """
    customer = await crud.customer.get(db, id=id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))
        
    if current_user.role != models.UserRole.super_admin:
        if current_user.org_id is None:
            if customer.assigned_to != current_user.id:
                raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        else:
            if customer.org_id != current_user.org_id:
                raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        
    customer = await crud.customer.remove(db, id=id)
    
    if customer:
        await log_activity(
            db, 
            actor=current_user, 
            action="customer.deleted", 
            target_type="customer", 
            target_id=id,
            details={"company_name": customer.company_name}
        )
        await db.commit()
        
    return customer


from app.services.import_service import import_service

@router.post("/import", status_code=201)
async def import_customers(
    *,
    db: AsyncSession = Depends(deps.get_db),
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Import customers from a CSV file.
    Supports smart mapping, auto-delimiter detection, and historical data preservation.
    """
    if not file.filename.lower().endswith('.csv'):
        raise HTTPException(status_code=400, detail=translator.t("invalid_file_type_csv", lang=lang))

    # Guard against oversized uploads
    MAX_CSV_BYTES = 10 * 1024 * 1024  # Increased to 10 MB for larger imports
    content = await file.read()
    if len(content) > MAX_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=translator.t("csv_file_too_large", lang=lang),
        )
    
    try:
        decoded = content.decode('utf-8')
    except UnicodeDecodeError:
        try:
            decoded = content.decode('latin-1')
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail=translator.t("csv_decode_error", lang=lang))

    # Use the smart service to normalize data
    normalized_data = import_service.process_csv(decoded)
    
    new_customers = []
    skipped = 0
    count = 0

    for obj_in in normalized_data:
        try:
            # Smart Check: Does this customer already exist in the organization?
            existing_c = await crud.customer.get_by_identity(
                db, 
                org_id=current_user.org_id, 
                company_name=obj_in.get("company_name"),
                postal_code=obj_in.get("postal_code"),
                email=obj_in.get("email")
            )

            if existing_c:
                # UPDATE existing customer
                # Check if address changed to decide if we need re-geocoding
                address_fields = ["street", "city", "postal_code", "country"]
                changed = any(obj_in.get(f) != getattr(existing_c, f) for f in address_fields if f in obj_in)
                
                c = await crud.customer.update(db, db_obj=existing_c, obj_in=obj_in)
                if changed:
                    new_customers.append(c) # Add to geocode queue
                logger.info(f"Updated existing customer: {c.company_name}")
            else:
                # CREATE new customer
                c = await crud.customer.create_with_org(
                    db, 
                    obj_in=schemas.CustomerCreate(**obj_in), 
                    org_id=current_user.org_id, 
                    user_id=current_user.id
                )
                new_customers.append(c)
                logger.info(f"Created new customer: {c.company_name}")
            
            count += 1

            # Create or update Visit record for the calendar
            if c.next_due_at:
                # Safeguard: only create if no visit already exists for this day (prevents DB unique constraint errors)
                conflict = await crud.visit.check_conflict(
                    db, customer_id=c.id, scheduled_date=c.next_due_at.date()
                )
                if not conflict:
                    await crud.visit.create(
                        db,
                        obj_in=schemas.VisitCreate(
                            customer_id=c.id,
                            scheduled_date=c.next_due_at.date()
                        ),
                        rep_id=current_user.id,
                        org_id=current_user.org_id
                    )
        except Exception as e:
            logger.error(f"Failed to process import row: {str(e)}")
            skipped += 1
            continue

    if new_customers:
        customer_ids = [c.id for c in new_customers]
        background_tasks.add_task(bulk_geocode_customers, customer_ids, current_user.org_id)

    return {
        "status": "success",
        "processed": count,
        "skipped": skipped,
        "message": translator.t("customer_import_success", lang=lang, count=count)
    }




@router.post("/regeocode-unmapped")
async def trigger_bulk_regeocode(
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Find all customers in the current organization without coordinates and queue them for geocoding.
    """
    # Use scalar_subquery or just select IDs
    stmt = select(models.Customer.id).where(
        models.Customer.org_id == current_user.org_id,
        (models.Customer.latitude == None) | (models.Customer.longitude == None)
    )
    res = await db.execute(stmt)
    ids = [row[0] for row in res.all()]
    
    if not ids:
        return {"message": "No unmapped customers found.", "count": 0}
        
    # Prevent multiple concurrent tasks for the same organization
    lock_key = f"geocoding_lock:{current_user.org_id}"
    is_locked = await redis_client.get(lock_key)
    if is_locked:
        return {"message": "Geocoding task already in progress for your organization.", "count": 0}
        
    # Set lock with TTL (e.g., 30 minutes)
    await redis_client.set(lock_key, "true", ex=1800)
    
    background_tasks.add_task(bulk_geocode_customers, ids, current_user.org_id)
    return {"message": f"Queued {len(ids)} customers for geocoding.", "count": len(ids)}

@router.post("/{id}/geocode", response_model=schemas.Customer)
async def geocode_single_customer(
    id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Force a geocoding attempt for a single customer.
    """
    customer = await crud.customer.get(db, id=id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))
    
    # Ownership check
    if current_user.role != models.UserRole.super_admin:
        if customer.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))

    address_parts = [customer.street, customer.postal_code, customer.city, customer.country]
    # Sanitize: remove None (object), empty strings, and the literal string "None"
    clean_parts = [str(p) for p in address_parts if p and str(p).lower() != "none"]
    q = ", ".join(clean_parts)
    
    if not q:
         raise HTTPException(status_code=400, detail="Customer has no valid address fields set.")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": q, "format": "json", "limit": 1},
                headers={"User-Agent": "VisitPro/1.0 (manual-geocoder)"}
            )
            
            if res.status_code == 429:
                raise HTTPException(status_code=429, detail="The map service is currently busy. Please wait a few minutes before trying again.")

            if res.status_code == 200:
                data = res.json()
                if data:
                    customer = await crud.customer.update(db, db_obj=customer, obj_in={
                        "latitude": float(data[0]["lat"]),
                        "longitude": float(data[0]["lon"])
                    })
                    await db.commit()
                else:
                    raise HTTPException(status_code=404, detail="No coordinates found for this address. Please check for typos.")
            else:
                 raise HTTPException(status_code=res.status_code, detail="Mapping service error. Please try again later.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Manual geocoding error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Geocoding failed: {str(e)}")
            
    return customer


async def bulk_geocode_customers(customer_ids: List[UUID], org_id: Optional[UUID] = None):
    """Background task to geocode imported customers sequentially."""
    from app.db.session import SessionLocal
    async with SessionLocal() as db:
        for cid in customer_ids:
            c = await crud.customer.get(db, id=cid)
            if not c or (c.latitude and c.longitude):
                continue

            address_parts = [c.street, c.postal_code, c.city, c.country]
            # Sanitize: remove None (object), empty strings, and the literal string "None"
            clean_parts = [str(p) for p in address_parts if p and str(p).lower() != "none"]
            q = ", ".join(clean_parts)
            
            if not q:
                continue

            try:
                async with httpx.AsyncClient() as client:
                    res = await client.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={"q": q, "format": "json", "limit": 1},
                        headers={"User-Agent": "VisitPro/1.0 (bulk-geocoder)"}
                    )
                    
                    if res.status_code == 429:
                        logger.error("Nominatim 429: Rate limited. Sleeping for 60s.")
                        await asyncio.sleep(60) # Polite backoff
                        continue

                    if res.status_code == 200:
                        data = res.json()
                        if data:
                            await crud.customer.update(db, db_obj=c, obj_in={
                                "latitude": float(data[0]["lat"]),
                                "longitude": float(data[0]["lon"])
                            })
                            await db.commit()
                            logger.info(f"Geocoded: {c.company_name}")
                        else:
                            logger.warning(f"OSM: No results for {q}")
            except Exception as e:
                logger.error(f"Geocoding error for {c.company_name}: {str(e)}")
            
            await asyncio.sleep(1.2)
        
        # Cleanup lock when done
        if org_id:
            await redis_client.delete(f"geocoding_lock:{org_id}")
