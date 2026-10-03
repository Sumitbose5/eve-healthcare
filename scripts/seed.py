"""Seed sample tests, centres and prices (idempotent):  python -m scripts.seed"""
from decimal import Decimal

from sqlalchemy import select

from app.db.session import SessionLocal, engine
from app.models import Base, Centre, CentreTest, DiagnosticTest

TESTS = [
    ("CBC", "Complete Blood Count", "Overall blood health screen"),
    ("LFT", "Liver Function Test", "Liver enzymes and proteins"),
    ("KFT", "Kidney Function Test", "Creatinine, urea, electrolytes"),
    ("HBA1C", "HbA1c", "3-month average blood sugar"),
    ("TSH", "Thyroid Stimulating Hormone", "Thyroid screening"),
    ("LIPID", "Lipid Profile", "Cholesterol and triglycerides"),
]
CENTRES = {
    ("EVE Diagnostics Koramangala", "Bengaluru", "80 Feet Rd, Koramangala"):
        {"CBC": "350", "LFT": "700", "HBA1C": "550", "LIPID": "650"},
    ("EVE Diagnostics Indiranagar", "Bengaluru", "100 Feet Rd, Indiranagar"):
        {"CBC": "400", "KFT": "750", "TSH": "450"},
    ("EVE Diagnostics Andheri", "Mumbai", "Link Rd, Andheri West"):
        {"CBC": "380", "LFT": "720", "KFT": "780", "TSH": "430", "LIPID": "680"},
}


def main() -> None:
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        by_code = {}
        for code, name, desc in TESTS:
            t = db.scalar(select(DiagnosticTest).where(DiagnosticTest.code == code))
            if not t:
                t = DiagnosticTest(code=code, name=name, description=desc)
                db.add(t)
            by_code[code] = t
        db.flush()
        for (name, city, address), prices in CENTRES.items():
            c = db.scalar(select(Centre).where(Centre.name == name, Centre.city == city))
            if not c:
                c = Centre(name=name, city=city, address=address)
                db.add(c)
                db.flush()
            for code, price in prices.items():
                exists = db.scalar(select(CentreTest).where(
                    CentreTest.centre_id == c.id, CentreTest.test_id == by_code[code].id))
                if not exists:
                    db.add(CentreTest(centre_id=c.id, test_id=by_code[code].id,
                                      price=Decimal(price)))
        db.commit()
    print("Seeded.")


if __name__ == "__main__":
    main()
