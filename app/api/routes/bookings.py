from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Pagination, get_current_user, pagination
from app.db.session import get_db
from app.models import BookingStatus, User
from app.schemas import BookingCreate, BookingOut, Page
from app.services import bookings as service

router = APIRouter(prefix="/bookings", tags=["Bookings"])


@router.post("", response_model=BookingOut, status_code=status.HTTP_201_CREATED)
def create_booking(
    data: BookingCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    """Book a test at a centre. Starts as PENDING; pay via `POST /payments/` to confirm."""
    return service.create_booking(db, user, data)


@router.get("", response_model=Page[BookingOut])
def list_my_bookings(
    status_: BookingStatus | None = Query(None, alias="status"),
    page: Pagination = Depends(pagination),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows, total = service.list_bookings(db, user, status=status_, limit=page.limit, offset=page.offset)
    return Page[BookingOut](items=rows, total=total, limit=page.limit, offset=page.offset)


@router.get("/{booking_id}", response_model=BookingOut)
def get_booking(
    booking_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return service.get_booking(db, user, booking_id)


@router.post("/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(
    booking_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
):
    return service.cancel_booking(db, user, booking_id)
