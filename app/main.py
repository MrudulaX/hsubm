"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

EpochMillis = Annotated[StrictInt, Field(ge=100000000000, le=4102444800000, strict=True)]
Presence = Literal["PRESENT", "WFH", "ON_DUTY"]
AnyStatus = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

PRESENT_SET = ("PRESENT", "WFH", "ON_DUTY")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])
    db.employees.create_index("joined_on")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    db.attendance_logs.create_index("date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def pymongo_handler(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=503, content={"detail": "mongo unavailable"})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def trunc_ms(ms: int) -> int:
    return (int(ms) // 1000) * 1000


def ms_to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(trunc_ms(ms) / 1000, tz=UTC)


def as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def trunc_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return as_utc(dt).replace(microsecond=0)


def dt_to_ms(dt) -> Optional[int]:
    if dt is None:
        return None
    if isinstance(dt, bool):
        return None
    if isinstance(dt, int):
        return (int(dt) // 1000) * 1000
    d = trunc_dt(dt)
    return int(d.timestamp()) * 1000


def err422(msg: str, loc: str = "body") -> HTTPException:
    return HTTPException(status_code=422, detail=[{"loc": [loc], "msg": msg, "type": "value_error"}])


def now_utc_trunc() -> datetime:
    n = datetime.now(tz=UTC)
    return n.replace(microsecond=0)


def parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    return (eh * 60 + em) <= (sh * 60 + sm)


def shift_start_dt(date_str: str, shift_start: str) -> datetime:
    h, m = parse_hhmm(shift_start)
    return datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)


def shift_end_dt(date_str: str, shift_start: str, shift_end: str) -> datetime:
    h, m = parse_hhmm(shift_end)
    d = datetime.fromisoformat(date_str).replace(hour=h, minute=m, second=0, microsecond=0, tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        d = d + timedelta(days=1)
    return d


def attendance_date(punch_utc: datetime, shift_start: str, shift_end: str) -> str:
    ist = trunc_dt(punch_utc).astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end):
        eh, em = parse_hhmm(shift_end)
        if (ist.hour * 3600 + ist.minute * 60 + ist.second) < (eh * 3600 + em * 60):
            d = d - timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    pi = trunc_dt(punch_in)
    start = shift_start_dt(date_str, shift_start)
    diff = int((pi - start).total_seconds())
    if diff > 600:
        return diff // 60
    return 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    pi = trunc_dt(punch_in)
    po = trunc_dt(punch_out)
    secs = int((po - pi).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    po = trunc_dt(punch_out)
    end = shift_end_dt(date_str, shift_start, shift_end)
    mins = int((po - end).total_seconds()) // 60
    return mins if mins >= 30 else 0


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False
    return work_hours < 4.50


def round2(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def round4(x: Optional[float]) -> Optional[float]:
    if x is None:
        return None
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def month_bounds(month: str) -> tuple[str, str]:
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def parse_iso_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError, AttributeError):
        raise err422(f"invalid date: {s}")


def check_month(s: str):
    if len(s) != 7 or s[4] != "-":
        raise err422(f"invalid month: {s}")
    try:
        y, m = int(s[:4]), int(s[5:7])
        if not (1 <= m <= 12):
            raise ValueError
        date(y, m, 1)
    except Exception:
        raise err422(f"invalid month: {s}")


def working_days(start_s: str, end_s: str) -> int:
    s = date.fromisoformat(start_s)
    e = date.fromisoformat(end_s)
    if s > e:
        return 0
    n = 0
    d = s
    while d <= e:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def serialize_history(h: list) -> list:
    out = []
    for e in h or []:
        out.append(
            {
                "at": dt_to_ms(e.get("at")),
                "by": e.get("by"),
                "reason": e.get("reason"),
                "changes": serialize_changes(e.get("changes") or {}),
            }
        )
    return out


def serialize_changes(ch: dict) -> dict:
    out = {}
    for k, v in ch.items():
        f = v.get("from") if isinstance(v, dict) else None
        t = v.get("to") if isinstance(v, dict) else None
        if k in ("punch_in", "punch_out"):
            out[k] = {
                "from": dt_to_ms(f),
                "to": dt_to_ms(t),
            }
        else:
            out[k] = {"from": f, "to": t}
    return out


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc.get("shift_start", "09:30"),
        "shift_end": doc.get("shift_end", "18:30"),
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def serialize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0) if doc.get("late_minutes") is not None else 0,
        "overtime_minutes": doc.get("overtime_minutes", 0) if doc.get("overtime_minutes") is not None else 0,
        "half_day": doc.get("half_day", False) if doc.get("half_day") is not None else False,
        "history": serialize_history(doc.get("history") or []),
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def check_all(self):
        try:
            parse_iso_date(self.joined_on)
        except HTTPException as e:
            raise ValueError(str(e.detail))
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Presence = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = {"extra": "ignore"}

    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeIn(BaseModel):
    model_config = {"extra": "ignore"}

    status: Optional[AnyStatus] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "mongo unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc_trunc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    q: dict = {}
    if department is not None:
        q["department"] = department
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    )
    return {"items": [serialize_employee(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise err422("punched_at must be epoch millis")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ms = body.punched_at if body.punched_at is not None else dt_to_ms(now_utc_trunc())
    pi = ms_to_utc(int(ms))
    d = attendance_date(pi, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": pi,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(pi, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    doc.pop("_id", None)
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    if "punched_at" in body.model_fields_set and body.punched_at is None:
        raise err422("punched_at must be epoch millis")
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    ms = body.punched_at if body.punched_at is not None else dt_to_ms(now_utc_trunc())
    po = ms_to_utc(int(ms))
    cand = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": po, "$ne": None}}, sort=[("punch_in", DESCENDING)]
    )
    if cand is None or cand.get("punch_in") is None:
        raise HTTPException(404, "no punch-in found")
    if cand.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pi = trunc_dt(cand["punch_in"])
    if po <= pi:
        raise err422("punched_at must be after punch_in")
    if (po - pi).total_seconds() > 24 * 3600:
        raise err422("punch-out more than 24h after punch-in")
    wh = compute_work_hours(pi, po)
    ot = compute_overtime(po, emp["shift_start"], emp["shift_end"], cand["date"])
    hd = compute_half_day(wh)
    hist_len = len(cand.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": cand["_id"],
            "punch_out": None,
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {"$set": {"punch_out": po, "work_hours": wh, "overtime_minutes": ot, "half_day": hd}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AnyStatus] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if date_from is not None:
        parse_iso_date(date_from)
    if date_to is not None:
        parse_iso_date(date_to)
    if date_from is not None and date_to is not None and date_from > date_to:
        raise err422("date_from > date_to")
    q = attendance_list_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [serialize_attendance(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_iso_date(date)
    fields_set = body.model_fields_set
    if "punch_in" in fields_set and body.punch_in is None:
        raise err422("punch_in must be epoch millis")
    if "punch_out" in fields_set and body.punch_out is None:
        raise err422("punch_out must be epoch millis")
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "no record for that date")

    old_status = rec.get("status")
    old_pi = trunc_dt(rec.get("punch_in")) if rec.get("punch_in") is not None else None
    old_po = trunc_dt(rec.get("punch_out")) if rec.get("punch_out") is not None else None
    old_wh = rec.get("work_hours")
    old_late = rec.get("late_minutes", 0) if rec.get("late_minutes") is not None else 0
    old_ot = rec.get("overtime_minutes", 0) if rec.get("overtime_minutes") is not None else 0
    old_hd = rec.get("half_day", False) if rec.get("half_day") is not None else False

    new_status = body.status if body.status is not None else old_status
    new_pi = ms_to_utc(int(body.punch_in)) if body.punch_in is not None else old_pi
    new_po = ms_to_utc(int(body.punch_out)) if body.punch_out is not None else old_po

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise err422("punch times not allowed with ABSENT/LEAVE")
        new_pi = None
        new_po = None
    else:
        if new_pi is None:
            raise err422("punch_in required for presence status")
        if attendance_date(new_pi, emp["shift_start"], emp["shift_end"]) != date:
            raise err422("punch_in must stay on attendance date")
        if new_po is not None:
            if new_po <= new_pi:
                raise err422("punch_out must be after punch_in")
            if (new_po - new_pi).total_seconds() > 24 * 3600:
                raise err422("punch_out more than 24h after punch_in")

    if new_status in ("ABSENT", "LEAVE"):
        new_wh = None
        new_late = 0
        new_ot = 0
        new_hd = False
    elif new_po is None:
        new_late = compute_late_minutes(new_pi, emp["shift_start"], date)
        new_wh = None
        new_ot = 0
        new_hd = False
    else:
        new_late = compute_late_minutes(new_pi, emp["shift_start"], date)
        new_wh = compute_work_hours(new_pi, new_po)
        new_ot = compute_overtime(new_po, emp["shift_start"], emp["shift_end"], date)
        new_hd = compute_half_day(new_wh)

    def ms_or_none(v):
        return dt_to_ms(v) if v is not None else None

    changes: dict = {}
    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if ms_or_none(new_pi) != ms_or_none(old_pi):
        changes["punch_in"] = {"from": ms_or_none(old_pi), "to": ms_or_none(new_pi)}
    if ms_or_none(new_po) != ms_or_none(old_po):
        changes["punch_out"] = {"from": ms_or_none(old_po), "to": ms_or_none(new_po)}
    if new_wh != old_wh:
        changes["work_hours"] = {"from": old_wh, "to": new_wh}
    if new_late != old_late:
        changes["late_minutes"] = {"from": old_late, "to": new_late}
    if new_ot != old_ot:
        changes["overtime_minutes"] = {"from": old_ot, "to": new_ot}
    if new_hd != old_hd:
        changes["half_day"] = {"from": old_hd, "to": new_hd}
    if not changes:
        raise err422("request changes nothing")

    entry = {"at": now_utc_trunc(), "by": body.regularized_by, "reason": body.reason, "changes": {}}
    for k, v in changes.items():
        if k in ("punch_in", "punch_out"):
            f = ms_to_utc(v["from"]) if v["from"] is not None else None
            t = ms_to_utc(v["to"]) if v["to"] is not None else None
            entry["changes"][k] = {"from": f, "to": t}
        else:
            entry["changes"][k] = {"from": v["from"], "to": v["to"]}

    hist_len = len(rec.get("history") or [])
    res = db.attendance_logs.update_one(
        {
            "_id": rec["_id"],
            "status": old_status,
            "punch_in": rec.get("punch_in"),
            "punch_out": rec.get("punch_out"),
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, hist_len]},
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_pi,
                "punch_out": new_po,
                "work_hours": new_wh,
                "late_minutes": new_late,
                "overtime_minutes": new_ot,
                "half_day": new_hd,
            },
            "$push": {"history": entry},
        },
    )
    if res.matched_count == 0:
        raise HTTPException(409, "concurrent modification")
    fresh = db.attendance_logs.find_one({"_id": rec["_id"]})
    return serialize_attendance(fresh)


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]):
    check_month(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "unknown employee")
    m_start, m_end = month_bounds(month)
    eff_start = max(m_start, emp["joined_on"]) if emp["joined_on"] <= m_end else None
    wd = working_days(eff_start, m_end) if eff_start is not None else 0
    pipe = monthly_pipe(emp_code, m_start, m_end)
    rows = list(db.attendance_logs.aggregate(pipe))
    r = rows[0] if rows else {}
    present = float(r.get("present_days", 0))
    pct = round2(present / wd * 100) if wd > 0 else None
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": int(r.get("leave_days", 0)),
        "late_count": int(r.get("late_count", 0)),
        "total_late_minutes": int(r.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(r.get("total_overtime_minutes", 0)),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    check_month(month)
    m_start, m_end = month_bounds(month)
    match: dict = {"joined_on": {"$lte": m_end}}
    if department is not None:
        match["department"] = department
    pipe = summary_pipe(match, m_start, m_end)
    out = []
    for g in db.employees.aggregate(pipe):
        if g["_id"] is None:
            continue
        n = g.get("work_n", 0)
        avg = round2(g.get("work_sum", 0) / n) if n > 0 else None
        out.append(
            {
                "department": g["_id"],
                "headcount": len(g.get("emps", [])),
                "present_days": float(g.get("present_days", 0)),
                "avg_work_hours": avg,
                "late_count": int(g.get("late_count", 0)),
                "total_late_minutes": int(g.get("total_late_minutes", 0)),
                "leave_count": int(g.get("leave_count", 0)),
                "on_duty_count": int(g.get("on_duty_count", 0)),
            }
        )
    return {"month": month, "items": out}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    check_month(month)
    m_start, m_end = month_bounds(month)
    pipe = leaderboard_pipe(m_start, m_end, department, limit)
    return {"month": month, "items": list(db.attendance_logs.aggregate(pipe))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query(alias="to")],
):
    f = parse_iso_date(from_)
    t = parse_iso_date(to)
    if to < from_:
        raise err422("to < from")
    if (t - f).days + 1 > 92:
        raise err422("range exceeds 92 days")
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(404, "unknown department")
    f_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
    t_dt = datetime(t.year, t.month, t.day, tzinfo=UTC)
    excl = t_dt + timedelta(days=1)
    joined = [
        d["joined_on"]
        for d in db.employees.find({"department": department}, {"_id": 0, "joined_on": 1})
    ]
    pipe = trend_pipe(department, from_, to, f_dt, excl, joined)
    items = []
    for r in db.attendance_logs.aggregate(pipe):
        items.append(
            {
                "date": r["date"],
                "is_working_day": bool(r["is_working_day"]),
                "headcount": int(r["headcount"]),
                "present_count": float(r.get("present_count", 0)),
                "late_count": int(r.get("late_count", 0)),
                "attendance_rate": round4(r.get("attendance_rate")),
                "moving_avg_7d": round4(r.get("moving_avg_7d")),
            }
        )
    return {"department": department, "items": items}


def attendance_list_filter(emp_code, date_from, date_to, status) -> dict:
    q: dict = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    if status is not None:
        q["status"] = status
    return q


def monthly_pipe(emp_code: str, m_start: str, m_end: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": m_start, "$lte": m_end}}},
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", list(PRESENT_SET)]},
                                    {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]},
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def summary_pipe(match: dict, m_start: str, m_end: str) -> list:
    in_month = {
        "$and": [
            {"$gte": [{"$ifNull": ["$logs.date", ""]}, m_start]},
            {"$lte": [{"$ifNull": ["$logs.date", ""]}, m_end]},
        ]
    }
    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs",
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "emps": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": [{"$ifNull": ["$logs.emp_code", None]}, None]},
                                    in_month,
                                    {"$in": ["$logs.status", list(PRESENT_SET)]},
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": {"$ifNull": ["$logs.date", m_start]}
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$ifNull": ["$logs.half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "work_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    in_month,
                                    {"$in": ["$logs.status", list(PRESENT_SET)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },
                "work_n": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    in_month,
                                    {"$in": ["$logs.status", list(PRESENT_SET)]},
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [in_month, {"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$cond": [in_month, {"$ifNull": ["$logs.late_minutes", 0]}, 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [in_month, {"$eq": ["$logs.status", "LEAVE"]}]},
                            1,
                            0,
                        ]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": [in_month, {"$eq": ["$logs.status", "ON_DUTY"]}]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipe(m_start: str, m_end: str, department: Optional[str], limit: int) -> list:
    pipe: list = [
        {"$match": {"date": {"$gte": m_start, "$lte": m_end}, "late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department is not None:
        pipe.append({"$match": {"emp.department": department}})
    pipe += [
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipe


def trend_pipe(department: str, from_: str, to: str, f_dt: datetime, excl: datetime, joined: Optional[list] = None) -> list:
    roster = list(joined or [])
    hc_expr = {
        "$size": {
            "$filter": {
                "input": {"$literal": roster},
                "as": "j",
                "cond": {"$lte": ["$$j", {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}}]},
            }
        }
    }
    return [
        {"$match": {"date": {"$gte": from_, "$lte": to}}},
        {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", list(PRESENT_SET)]},
                            {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
                },
            }
        },
        {"$project": {"_id": 0, "d": {"$dateFromString": {"dateString": "$_id"}}, "present_count": 1, "late_count": 1}},
        {"$sort": {"d": 1}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [f_dt, excl]}}},
        {
            "$project": {
                "d": 1,
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$project": {
                "d": 1,
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                "headcount": hc_expr,
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$project": {
                "d": 1,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"d": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"d": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[AnyStatus] = None,
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None, alias="to"),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    if endpoint == "attendance_list":
        if date_from is not None:
            parse_iso_date(date_from)
        if date_to is not None:
            parse_iso_date(date_to)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise err422("date_from > date_to")
        q = attendance_list_filter(emp_code, date_from, date_to, status)
        cmd = {
            "explain": {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            "verbosity": "executionStats",
        }
        out = db.command(cmd)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise err422("emp_code and month required")
        check_month(month)
        m_start, m_end = month_bounds(month)
        pipe = monthly_pipe(emp_code, m_start, m_end)
        out = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    if endpoint == "department_summary":
        if not month:
            raise err422("month required")
        check_month(month)
        m_start, m_end = month_bounds(month)
        match: dict = {"joined_on": {"$lte": m_end}}
        if department is not None:
            match["department"] = department
        pipe = summary_pipe(match, m_start, m_end)
        out = db.command({"explain": {"aggregate": "employees", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "employees", "explain": out}
    if endpoint == "late_leaderboard":
        if not month:
            raise err422("month required")
        check_month(month)
        m_start, m_end = month_bounds(month)
        pipe = leaderboard_pipe(m_start, m_end, department, limit)
        out = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise err422("department, from, to required")
        f = parse_iso_date(from_)
        t = parse_iso_date(to)
        if to < from_:
            raise err422("to < from")
        if (t - f).days + 1 > 92:
            raise err422("range exceeds 92 days")
        f_dt = datetime(f.year, f.month, f.day, tzinfo=UTC)
        t_dt = datetime(t.year, t.month, t.day, tzinfo=UTC)
        excl = t_dt + timedelta(days=1)
        joined = [d["joined_on"] for d in db.employees.find({"department": department}, {"_id": 0, "joined_on": 1})]
        pipe = trend_pipe(department, from_, to, f_dt, excl, joined)
        out = db.command({"explain": {"aggregate": "attendance_logs", "pipeline": pipe, "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": out}
    raise err422("unknown endpoint")
