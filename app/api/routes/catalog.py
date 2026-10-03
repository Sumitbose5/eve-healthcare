"""Diagnostic tests + centres. Reads are public and cached; writes are admin-only and
invalidate the cache namespace."""
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.api.deps import Pagination, pagination, require_admin
from app.core.cache import Cache, get_cache
from app.core.errors import AppError
from app.db.session import get_db
from app.models import Centre, CentreTest, DiagnosticTest
from app.schemas import (
    CentreCreate, CentreOut, CentreTestOut, CentreTestPrice, Page, TestCreate, TestOut,
)

tests_router = APIRouter(prefix="/tests", tags=["Diagnostic tests"])
centres_router = APIRouter(prefix="/centres", tags=["Centres"])

NS = "catalog"


def _get_centre(db: Session, centre_id: int) -> Centre:
    centre = db.scalar(
        select(Centre).where(Centre.id == centre_id)
        .options(selectinload(Centre.centre_tests).selectinload(CentreTest.test))
    )
    if centre is None:
        raise AppError(404, "centre_not_found", "Centre not found")
    return centre


# ---- diagnostic tests --------------------------------------------------------
@tests_router.post("", response_model=TestOut, status_code=201,
                   dependencies=[Depends(require_admin)])
def create_test(data: TestCreate, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    test = DiagnosticTest(code=data.code.upper(), name=data.name.strip(), description=data.description)
    db.add(test)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "test_exists", "A test with this code or name already exists")
    cache.bump(NS)
    return test


@tests_router.get("", response_model=Page[TestOut])
def list_tests(
    q: str | None = Query(None, description="Case-insensitive name search"),
    page: Pagination = Depends(pagination),
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
):
    key = cache.key(NS, "tests", q, page.limit, page.offset)
    if (hit := cache.get_json(key)) is not None:
        return hit
    stmt = select(DiagnosticTest)
    if q:
        stmt = stmt.where(DiagnosticTest.name.icontains(q, autoescape=True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(DiagnosticTest.name).limit(page.limit).offset(page.offset)).all()
    result = Page[TestOut](items=rows, total=total, limit=page.limit, offset=page.offset)
    payload = result.model_dump(mode="json")
    cache.set_json(key, payload)
    return payload


@tests_router.get("/{test_id}", response_model=TestOut)
def get_test(test_id: int, db: Session = Depends(get_db)):
    test = db.get(DiagnosticTest, test_id)
    if test is None:
        raise AppError(404, "test_not_found", "Test not found")
    return test


# ---- centres -----------------------------------------------------------------
@centres_router.post("", response_model=CentreOut, status_code=201,
                     dependencies=[Depends(require_admin)])
def create_centre(data: CentreCreate, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    centre = Centre(**data.model_dump())
    db.add(centre)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "centre_exists", "A centre with this name already exists in that city")
    cache.bump(NS)
    return centre


@centres_router.get("", response_model=Page[CentreOut])
def list_centres(
    q: str | None = Query(None, description="Search centre name"),
    city: str | None = Query(None, description="Exact city (case-insensitive)"),
    test_id: int | None = Query(None, gt=0, description="Only centres offering this test"),
    page: Pagination = Depends(pagination),
    db: Session = Depends(get_db),
    cache: Cache = Depends(get_cache),
):
    key = cache.key(NS, "centres", q, city, test_id, page.limit, page.offset)
    if (hit := cache.get_json(key)) is not None:
        return hit
    stmt = select(Centre)
    if q:
        stmt = stmt.where(Centre.name.icontains(q, autoescape=True))
    if city:
        stmt = stmt.where(func.lower(Centre.city) == city.lower())
    if test_id:
        stmt = stmt.where(exists().where(
            CentreTest.centre_id == Centre.id,
            CentreTest.test_id == test_id,
            CentreTest.is_active.is_(True),
        ))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.options(selectinload(Centre.centre_tests).selectinload(CentreTest.test))
        .order_by(Centre.name, Centre.id).limit(page.limit).offset(page.offset)
    ).all()
    result = Page[CentreOut](items=rows, total=total, limit=page.limit, offset=page.offset)
    payload = result.model_dump(mode="json")
    cache.set_json(key, payload)
    return payload


@centres_router.get("/{centre_id}", response_model=CentreOut)
def get_centre(centre_id: int, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)):
    key = cache.key(NS, "centre", centre_id)
    if (hit := cache.get_json(key)) is not None:
        return hit
    payload = CentreOut.model_validate(_get_centre(db, centre_id)).model_dump(mode="json")
    cache.set_json(key, payload)
    return payload


@centres_router.put("/{centre_id}/tests/{test_id}", response_model=CentreTestOut,
                    dependencies=[Depends(require_admin)])
def set_centre_test_price(
    centre_id: int, test_id: int, data: CentreTestPrice,
    db: Session = Depends(get_db), cache: Cache = Depends(get_cache),
):
    """Offer a test at a centre (or update its price / re-activate it). Idempotent upsert.
    Existing bookings keep the price they were made at."""
    _get_centre(db, centre_id)
    if db.get(DiagnosticTest, test_id) is None:
        raise AppError(404, "test_not_found", "Test not found")
    link = db.scalar(select(CentreTest).where(
        CentreTest.centre_id == centre_id, CentreTest.test_id == test_id))
    if link is None:
        link = CentreTest(centre_id=centre_id, test_id=test_id, price=data.price)
        db.add(link)
    else:
        link.price, link.is_active = data.price, True
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError(409, "conflict", "Concurrent update, please retry")
    cache.bump(NS)
    return link


@centres_router.delete("/{centre_id}/tests/{test_id}", status_code=204,
                       dependencies=[Depends(require_admin)])
def remove_centre_test(
    centre_id: int, test_id: int, db: Session = Depends(get_db), cache: Cache = Depends(get_cache)
):
    """Stop offering a test at a centre (soft delete, so old bookings stay valid)."""
    link = db.scalar(select(CentreTest).where(
        CentreTest.centre_id == centre_id, CentreTest.test_id == test_id,
        CentreTest.is_active.is_(True)))
    if link is None:
        raise AppError(404, "test_not_offered", "This centre does not offer that test")
    link.is_active = False
    db.commit()
    cache.bump(NS)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
