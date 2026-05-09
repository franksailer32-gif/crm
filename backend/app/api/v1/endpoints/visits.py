from typing import Any, List, Optional
from uuid import UUID
from datetime import date
from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from app.core.i18n import translator
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app import crud, models, schemas
from app.api import deps
from app.models.visit import VisitStatus
from app.services.report_service import report_service
from app.core.audit import log_activity
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


#  helpers
def _check_visit_access(visit_data: dict, user: models.User, lang: str = "en") -> None:
    """Verify the current user is allowed to access this visit."""
    if user.role == models.UserRole.super_admin:
        return
    if user.org_id:
        if visit_data["org_id"] != user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
        if user.role == models.UserRole.rep and visit_data["rep_id"] != user.id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
    else:
        if visit_data["rep_id"] != user.id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))


#  STATIC ROUTES — must be registered BEFORE /{visit_id}

@router.get("/due-today", response_model=List[schemas.Customer])
async def get_due_today(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
) -> Any:
    """
    Get customers that are due for a visit today (or earlier).
    Reps see only their assigned customers; Org Admins see all in org.
    """
    customers = await crud.visit.get_due_today(db, user=current_user)
    return customers


@router.get("/overdue", response_model=List[schemas.Customer])
async def get_overdue(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
) -> Any:
    """
    Get overdue customers (next_due_at has passed).
    Returns customers with status 'neglected' or 'overdue'.
    """
    customers = await crud.visit.get_overdue(db, user=current_user)
    return customers


@router.get("/todos", response_model=List[dict])
async def get_todos(
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
) -> Any:
    """
    Get all customers with a non-null to-do status, with their next planned visit date.
    Scoped by role: reps see their own customers, org_admins see all in org.
    """
    return await crud.visit.get_todos(db, user=current_user)


@router.get("/calendar-summary", response_model=List[dict])
async def get_calendar_summary(
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
) -> Any:
    """
    Return [{date, count}] of planned visits for the given year/month.
    Used by the calendar widget to show dots on dates that have planned visits.
    """
    return await crud.visit.get_calendar_summary(db, user=current_user, year=year, month=month)


@router.get("/history/{customer_id}", response_model=List[schemas.VisitHistoryResponse])
async def get_visit_history(
    customer_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Get chronological visit history for a specific customer.
    Shows who visited, when, and notes — ordered newest first.
    """
    # Verify the customer belongs to the user's scope
    customer = await crud.customer.get(db, id=customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))

    if current_user.org_id:
        if customer.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
    else:
        if customer.assigned_to != current_user.id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))

    history = await crud.visit.get_history_by_customer(
        db, customer_id=customer_id, org_id=current_user.org_id
    )
    return history


@router.get("/report")
async def download_visit_report(
    date_filter: date = Query(..., alias="date"),
    format: str = Query("pdf", pattern="^(pdf|csv)$"),
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Download a report of completed visits for a specific date.
    Returns CSV or PDF file.
    """
    logger.info(f"Report Request: Date={date_filter}, Format={format}, User={current_user.email}")
    
    # Fetch report data using the scoped CRUD method
    visits = await crud.visit.get_report_data(
        db, user=current_user, target_date=date_filter
    )
    
    if not visits:
        raise HTTPException(
            status_code=404, 
            detail=translator.t("no_visits_found_date", lang=lang)
        )

    filename = f"Visit_Report_{date_filter}"
    
    if format == "csv":
        file_obj = report_service.generate_csv_report(visits)
        return Response(
            content=file_obj.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}.csv"}
        )
    else:
        # PDF
        org_name = "VisitPro"
        if current_user.org_id:
            org = await crud.org.get(db, id=current_user.org_id)
            if org:
                org_name = org.name

        file_obj = report_service.generate_pdf_report(
            visits, report_date=date_filter, org_name=org_name
        )
        return Response(
            content=file_obj.getvalue(),
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}.pdf"}
        )


#  MAIN CRUD ROUTES

@router.get("/", response_model=List[schemas.Visit])
async def list_visits(
    db: AsyncSession = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    scheduled_date: Optional[date] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    visit_status: Optional[VisitStatus] = Query(None, alias="status"),
    customer_id: Optional[UUID] = None,
    completed_date: Optional[date] = None,
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    List visits with optional filters.
    - Reps see their own visits.
    - Org Admins see all visits in their organization.
    - Solo users see only their own visits.
    """
    visits = await crud.visit.get_multi_by_scope(
        db,
        user=current_user,
        skip=skip,
        limit=limit,
        scheduled_date=scheduled_date,
        date_from=date_from,
        date_to=date_to,
        status=visit_status,
        customer_id=customer_id,
        completed_date=completed_date,
    )
    return visits


@router.post("/quick-visit", response_model=schemas.VisitQuickCompleteResponse)
async def quick_visit(
    *,
    db: AsyncSession = Depends(deps.get_db),
    quick_in: schemas.VisitQuickCreate,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Record an unscheduled visit that happened today.
    - Creates or finds a visit record for today
    - Marks it as completed immediately
    - Updates customer status and schedules next visit
    """
    # 1. Scope check (customer access)
    customer = await crud.customer.get(db, id=quick_in.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))

    if current_user.org_id:
        if customer.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
    else:
        if customer.assigned_to != current_user.id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))

    # 2. Check lock
    if customer.is_locked:
        raise HTTPException(status_code=409, detail=translator.t("customer_locked_org", lang=lang))

    # 3. Perform quick visit
    result = await crud.visit.quick_visit(
        db, obj_in=quick_in, rep_id=current_user.id, org_id=current_user.org_id
    )

    # 4. Return formatted response
    completed_data = await crud.visit.get_with_names(db, id=result['completed'].id)
    next_visit_data = None
    if result.get('next_visit'):
        next_visit_data = await crud.visit.get_with_names(db, id=result['next_visit'].id)

    # 5. Audit
    await log_activity(
        db, 
        actor=current_user, 
        action="visit.quick_record", 
        target_type="visit", 
        target_id=result['completed'].id,
        details={"customer_name": completed_data['customer_name']}
    )

    return {
        'completed': completed_data,
        'next_visit': next_visit_data
    }


@router.post("/", response_model=schemas.Visit, status_code=status.HTTP_201_CREATED)
async def create_visit(
    *,
    db: AsyncSession = Depends(deps.get_db),
    visit_in: schemas.VisitCreate,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Schedule a new visit.
    - Checks if the customer is locked by an Org Admin.
    - Warns if another rep already has a visit scheduled for this customer on the same day.
    """
    # Verify customer exists and is in scope
    customer = await crud.customer.get(db, id=visit_in.customer_id)
    if not customer:
        raise HTTPException(status_code=404, detail=translator.t("customer_not_found", lang=lang))

    if current_user.org_id:
        if customer.org_id != current_user.org_id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))
    else:
        if customer.assigned_to != current_user.id:
            raise HTTPException(status_code=403, detail=translator.t("insufficient_privileges", lang=lang))

    # Check if customer is locked
    if customer.is_locked:
        raise HTTPException(
            status_code=409,
            detail=translator.t("customer_locked_org", lang=lang)
        )

    # Conflict detection
    conflict = await crud.visit.check_conflict(
        db, customer_id=visit_in.customer_id, scheduled_date=visit_in.scheduled_date
    )
    if conflict:
        # Fetch the rep's name for a helpful message
        conflicting_rep = await crud.user.get(db, id=conflict.rep_id)
        rep_name = conflicting_rep.full_name if conflicting_rep else "another rep"
        raise HTTPException(
            status_code=409,
            detail=translator.t("visit_conflict", lang=lang, rep_name=rep_name)
        )

    # Determine assigned rep
    target_rep_id = current_user.id
    if current_user.role == models.UserRole.org_admin and visit_in.rep_id:
        # Admin is assigning to someone else. 
        # Verify that the target rep exists and belongs to the same org.
        target_rep = await crud.user.get(db, id=visit_in.rep_id)
        if not target_rep or target_rep.org_id != current_user.org_id:
            raise HTTPException(
                status_code=400,
                detail=translator.t("invalid_rep_assignment", lang=lang)
            )
        target_rep_id = visit_in.rep_id

    try:
        visit = await crud.visit.create(
            db, obj_in=visit_in, rep_id=target_rep_id, org_id=current_user.org_id
        )
    except IntegrityError:
        # Database-level safeguard against race-condition double booking
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=translator.t("visit_conflict", lang=lang),
        )

    # Return with display names
    visit_data = await crud.visit.get_with_names(db, id=visit.id)
    return visit_data


@router.get("/{visit_id}", response_model=schemas.Visit)
async def get_visit(
    visit_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.get_current_user),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """Get a single visit by ID."""
    visit_data = await crud.visit.get_with_names(db, id=visit_id)
    if not visit_data:
        raise HTTPException(status_code=404, detail=translator.t("visit_not_found", lang=lang))

    _check_visit_access(visit_data, current_user, lang=lang)
    return visit_data


@router.patch("/{visit_id}", response_model=schemas.Visit)
async def update_visit(
    *,
    visit_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    visit_in: schemas.VisitUpdate,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Update a planned visit (reschedule date or edit notes).
    Cannot modify completed visits.
    """
    visit = await crud.visit.get(db, id=visit_id)
    if not visit:
        raise HTTPException(status_code=404, detail=translator.t("visit_not_found", lang=lang))

    if visit.status != VisitStatus.planned:
        raise HTTPException(
            status_code=400,
            detail=translator.t("cannot_modify_completed", lang=lang)
        )

    # Access check
    visit_data = await crud.visit.get_with_names(db, id=visit_id)
    _check_visit_access(visit_data, current_user, lang=lang)

    # If rescheduling, check for conflicts on the new date
    if visit_in.scheduled_date and visit_in.scheduled_date != visit.scheduled_date:
        conflict = await crud.visit.check_conflict(
            db,
            customer_id=visit.customer_id,
            scheduled_date=visit_in.scheduled_date,
            exclude_visit_id=visit_id,
        )
        if conflict and conflict.rep_id != current_user.id:
            conflicting_rep = await crud.user.get(db, id=conflict.rep_id)
            rep_name = conflicting_rep.full_name if conflicting_rep else "another rep"
            raise HTTPException(
                status_code=409,
                detail=translator.t("visit_conflict", lang=lang)
            )

    try:
        visit = await crud.visit.update(db, db_obj=visit, obj_in=visit_in)
    except IntegrityError:
        # Database-level safeguard against race-condition double booking
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=translator.t("visit_conflict", lang=lang),
        )

    # Return with display names
    updated = await crud.visit.get_with_names(db, id=visit.id)
    return updated


@router.patch("/{visit_id}/complete", response_model=schemas.VisitCompleteResponse)
async def complete_visit(
    *,
    visit_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    complete_in: schemas.VisitComplete,
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Mark a visit as completed.
    - Sets visited_at to now
    - Updates customer: last_visited_at, next_due_at (+8 weeks), status → active
    - Creates a VisitHistory entry
    - Optionally creates a new planned visit for next due date
    Only the assigned rep can complete a visit.
    """
    visit = await crud.visit.get(db, id=visit_id)
    if not visit:
        raise HTTPException(status_code=404, detail=translator.t("visit_not_found", lang=lang))

    if visit.status != VisitStatus.planned:
        raise HTTPException(
            status_code=400,
            detail=translator.t("cannot_complete_status", lang=lang)
        )

    # Only assigned rep can complete
    if visit.rep_id != current_user.id:
        raise HTTPException(status_code=403, detail=translator.t("rep_only_complete", lang=lang))

    result = await crud.visit.complete(db, visit=visit, obj_in=complete_in)

    completed = await crud.visit.get_with_names(db, id=result['completed'].id)
    next_visit = None
    if result.get('next_visit'):
        next_visit = await crud.visit.get_with_names(db, id=result['next_visit'].id)

    # Audit
    await log_activity(
        db, 
        actor=current_user, 
        action="visit.completed", 
        target_type="visit", 
        target_id=visit_id,
        details={"customer_name": completed['customer_name']}
    )
    await db.commit()

    return {
        'completed': completed,
        'next_visit': next_visit,
    }


@router.delete("/{visit_id}", response_model=schemas.Visit)
async def cancel_visit(
    visit_id: UUID,
    db: AsyncSession = Depends(deps.get_db),
    current_user: models.User = Depends(deps.check_active_subscription),
    lang: str = Depends(deps.get_lang),
) -> Any:
    """
    Cancel/delete a planned visit.
    Cannot delete completed visits.
    """
    visit_data = await crud.visit.get_with_names(db, id=visit_id)
    if not visit_data:
        raise HTTPException(status_code=404, detail=translator.t("visit_not_found", lang=lang))

    _check_visit_access(visit_data, current_user, lang=lang)

    if visit_data["status"] != VisitStatus.planned:
        raise HTTPException(
            status_code=400,
            detail=translator.t("cannot_modify_completed", lang=lang)
        )

    visit = await crud.visit.remove(db, id=visit_id)
    return visit_data
