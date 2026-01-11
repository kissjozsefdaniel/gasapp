from datetime import date, timedelta

from typing import Optional, List
from sqlalchemy import select, desc
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, Float, Date, String, select, desc
from sqlalchemy.orm import declarative_base, Session

# --- Config (később DB-be is tehetjük) ---
MJ_PER_M3_DEFAULT = 33.91
PRICE_DISCOUNT_DEFAULT = 2.256
PRICE_MARKET_DEFAULT = 17.324
ANNUAL_QUOTA_MJ_DEFAULT = 63645.0
# Éves keret időszaka (állítható)
# Alap: naptári év. Ha "gázévet" akarsz, írd át pl. 10/1-re.
QUOTA_YEAR_START_MONTH = 1
QUOTA_YEAR_START_DAY = 1


DB_URL = "sqlite:///./gas.db"

engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
Base = declarative_base()

class Reading(Base):
    __tablename__ = "readings"
    id = Column(Integer, primary_key=True)
    reading_date = Column(Date, nullable=False)
    meter_m3 = Column(Float, nullable=False)
    note = Column(String, nullable=True)

class PeriodCalc(Base):
    __tablename__ = "period_calcs"
    id = Column(Integer, primary_key=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    days = Column(Integer, nullable=False)

    start_m3 = Column(Float, nullable=False)
    end_m3 = Column(Float, nullable=False)
    used_m3 = Column(Float, nullable=False)

    mj_per_m3 = Column(Float, nullable=False)
    used_mj = Column(Float, nullable=False)

    annual_quota_mj = Column(Float, nullable=False)
    discount_max_mj = Column(Float, nullable=False)
    discount_mj = Column(Float, nullable=False)
    market_mj = Column(Float, nullable=False)

    price_discount = Column(Float, nullable=False)
    price_market = Column(Float, nullable=False)

    discount_cost = Column(Float, nullable=False)
    market_cost = Column(Float, nullable=False)
    total_energy_cost = Column(Float, nullable=False)

Base.metadata.create_all(engine)

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

def quota_year_bounds(d: date) -> tuple[date, date]:
    """
    Visszaadja az éves keret időszakának (start, end) dátumát úgy,
    hogy d ebbe az időszakba essen.
    """
    start_this_year = date(d.year, QUOTA_YEAR_START_MONTH, QUOTA_YEAR_START_DAY)
    if d >= start_this_year:
        start = start_this_year
    else:
        start = date(d.year - 1, QUOTA_YEAR_START_MONTH, QUOTA_YEAR_START_DAY)

    end = date(start.year + 1, QUOTA_YEAR_START_MONTH, QUOTA_YEAR_START_DAY) - timedelta(days=1)
    return start, end


def used_discount_mj_in_quota_year(session: Session, d: date) -> float:
    """
    Mennyi kedvezményes MJ lett már felhasználva az adott éves keret-időszakban?
    Az eddig mentett period_calcs rekordok discount_mj mezőjét összegezzük.
    """
    start, end = quota_year_bounds(d)
    rows = session.execute(
        select(PeriodCalc.discount_mj)
        .where(PeriodCalc.end_date >= start)
        .where(PeriodCalc.end_date <= end)
    ).all()
    return float(sum(r[0] for r in rows))


def compute_period(
    start_date: date,
    end_date: date,
    start_m3: float,
    end_m3: float,
    mj_per_m3: float = MJ_PER_M3_DEFAULT,
    price_discount: float = PRICE_DISCOUNT_DEFAULT,
    price_market: float = PRICE_MARKET_DEFAULT,
    annual_quota_mj: float = ANNUAL_QUOTA_MJ_DEFAULT,
    remaining_quota_mj: Optional[float] = None,   
):
    if end_date < start_date:
        raise ValueError("A záró dátum nem lehet korábbi, mint a kezdő dátum.")
    if end_m3 < start_m3:
        raise ValueError("A záró óraállás nem lehet kisebb, mint a kezdő óraállás.")

    days = (end_date - start_date).days + 1
    used_m3 = end_m3 - start_m3
    used_mj = used_m3 * mj_per_m3

    discount_max_mj = annual_quota_mj / 365.0 * days
    if remaining_quota_mj is None:
        remaining_quota_mj = float("inf")

    discount_mj = min(used_mj, discount_max_mj, remaining_quota_mj)
    market_mj = max(0.0, used_mj - discount_mj)


    discount_cost = discount_mj * price_discount
    market_cost = market_mj * price_market
    total = discount_cost + market_cost

    return {
        "start_date": start_date,
        "end_date": end_date,
        "days": days,
        "start_m3": start_m3,
        "end_m3": end_m3,
        "used_m3": used_m3,
        "mj_per_m3": mj_per_m3,
        "used_mj": used_mj,
        "annual_quota_mj": annual_quota_mj,
        "discount_max_mj": discount_max_mj,
        "discount_mj": discount_mj,
        "market_mj": market_mj,
        "price_discount": price_discount,
        "price_market": price_market,
        "discount_cost": discount_cost,
        "market_cost": market_cost,
        "total_energy_cost": total,
    }


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    error = request.query_params.get("err")

    with Session(engine) as session:
        readings = session.execute(
            select(Reading).order_by(desc(Reading.reading_date))
        ).scalars().all()

        calcs = session.execute(
            select(PeriodCalc).order_by(desc(PeriodCalc.end_date))
        ).scalars().all()

        last_calc = session.execute(
            select(PeriodCalc).order_by(desc(PeriodCalc.id)).limit(1)
        ).scalars().first()
        used_discount_now = used_discount_mj_in_quota_year(session, date.today())
        remaining_now = max(0.0, ANNUAL_QUOTA_MJ_DEFAULT - used_discount_now)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "readings": readings,
            "calcs": calcs,
            "last_calc": last_calc,
            "error": error,
            "remaining_now": remaining_now,
            "used_discount_now": used_discount_now,
            "defaults": {
                "mj_per_m3": MJ_PER_M3_DEFAULT,
                "price_discount": PRICE_DISCOUNT_DEFAULT,
                "price_market": PRICE_MARKET_DEFAULT,
                "annual_quota_mj": ANNUAL_QUOTA_MJ_DEFAULT,
            },
        },
    )

@app.post("/add-reading")
def add_reading(
    reading_date: str = Form(...),
    meter_m3: float = Form(...),
    note: Optional[str] = Form(None),
):
    d = date.fromisoformat(reading_date)
    with Session(engine) as session:
        session.add(Reading(reading_date=d, meter_m3=meter_m3, note=note))
        session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete-calc")
def delete_calc(calc_id: int = Form(...)):
    with Session(engine) as session:
        obj = session.get(PeriodCalc, calc_id)
        if obj:
            session.delete(obj)
            session.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/compute-latest")
def compute_latest():
    with Session(engine) as session:
        latest_two = session.execute(
            select(Reading).order_by(desc(Reading.reading_date)).limit(2)
        ).scalars().all()

        if len(latest_two) < 2:
            return RedirectResponse(url="/?err=Nincs elég leolvasás (legalább 2 kell).", status_code=303)

        # order_by desc -> [legújabb, előző]
        end_r = latest_two[0]
        start_r = latest_two[1]
        used_discount = used_discount_mj_in_quota_year(session, end_r.reading_date)
        remaining = max(0.0, ANNUAL_QUOTA_MJ_DEFAULT - used_discount)
        try:
            res = compute_period(
            start_date=start_r.reading_date,
            end_date=end_r.reading_date,
            start_m3=start_r.meter_m3,
            end_m3=end_r.meter_m3,
            mj_per_m3=MJ_PER_M3_DEFAULT,
            price_discount=PRICE_DISCOUNT_DEFAULT,
            price_market=PRICE_MARKET_DEFAULT,
            annual_quota_mj=ANNUAL_QUOTA_MJ_DEFAULT,
            remaining_quota_mj=remaining,
            )
        except ValueError as e:
            return RedirectResponse(url=f"/?err={str(e)}", status_code=303)

        session.add(PeriodCalc(**res))
        session.commit()

    return RedirectResponse(url="/", status_code=303)


@app.post("/compute")
def compute_from_form(
    start_date: str = Form(...),
    end_date: str = Form(...),
    start_m3: float = Form(...),
    end_m3: float = Form(...),
    mj_per_m3: float = Form(MJ_PER_M3_DEFAULT),
    price_discount: float = Form(PRICE_DISCOUNT_DEFAULT),
    price_market: float = Form(PRICE_MARKET_DEFAULT),
    annual_quota_mj: float = Form(ANNUAL_QUOTA_MJ_DEFAULT),
):
    sd = date.fromisoformat(start_date)
    ed = date.fromisoformat(end_date)
    with Session(engine) as session:
        used_discount = used_discount_mj_in_quota_year(session, ed)
        remaining = max(0.0, annual_quota_mj - used_discount)

    try:
        res = compute_period(
            sd, ed, start_m3, end_m3,
            mj_per_m3, price_discount, price_market, annual_quota_mj,
            remaining_quota_mj=remaining
        )

    
    except ValueError:
        # egyszerűség kedvéért: visszadobunk a főoldalra (később lehet szépen kiírni)
        return RedirectResponse(url="/", status_code=303)

    with Session(engine) as session:
        session.add(PeriodCalc(**res))
        session.commit()

    return RedirectResponse(url="/", status_code=303)


# ---------------- API (grafikonhoz, exporthoz) ----------------
@app.get("/api/readings")
def api_readings():
    with Session(engine) as session:
        rows = session.execute(select(Reading).order_by(Reading.reading_date)).scalars().all()
    return [
        {"date": r.reading_date.isoformat(), "m3": r.meter_m3, "note": r.note}
        for r in rows
    ]


@app.get("/api/calcs")
def api_calcs():
    with Session(engine) as session:
        rows = session.execute(select(PeriodCalc).order_by(PeriodCalc.end_date)).scalars().all()
    return [
        {
            "id": c.id,
            "start_date": c.start_date.isoformat(),
            "end_date": c.end_date.isoformat(),
            "days": c.days,
            "used_m3": c.used_m3,
            "used_mj": c.used_mj,
            "discount_mj": c.discount_mj,
            "market_mj": c.market_mj,
            "total_energy_cost": c.total_energy_cost,
        }
        for c in rows
    ]
