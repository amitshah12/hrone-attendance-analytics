"""
Employee Attendance & Analytics API

Implemented:
- UTC/IST timestamp handling and attendance calculations
- Employee creation and listing
- Race-safe punch-in and punch-out
- Attendance listing
- Atomic, auditable attendance regularization
- MongoDB index creation at startup

Implemented:
- Employee monthly analytics
- Department summary
- Late leaderboard
- Department trend
- Admin explain endpoint
"""

import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError
import calendar
import re


# =========================================================
# Configuration
# =========================================================

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# =========================================================
# Timestamp helpers
# =========================================================

def epoch_ms_to_utc(value: int) -> datetime:
    """Convert an integer epoch-millisecond timestamp to UTC."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Timestamp must be an integer")

    if not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        raise ValueError("Timestamp is outside the permitted range")

    return datetime.fromtimestamp(value / 1000, tz=UTC).replace(
        microsecond=0
    )


def utc_now_seconds() -> datetime:
    """Return the current UTC time truncated to whole seconds."""
    return datetime.now(UTC).replace(microsecond=0)


def normalize_utc(value: datetime) -> datetime:
    """Normalize a known datetime instant to UTC whole seconds."""
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)

    return value.astimezone(UTC).replace(microsecond=0)


def utc_to_epoch_ms(value: datetime | None) -> int | None:
    """Convert a datetime to epoch milliseconds."""
    if value is None:
        return None

    return int(normalize_utc(value).timestamp()) * 1000


def utc_to_ist(value: datetime) -> datetime:
    return normalize_utc(value).astimezone(IST)


def attendance_date_for_punch_in(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    """Determine the attendance date for a shift, including overnight shifts."""
    local = utc_to_ist(punch_in)
    start = time.fromisoformat(shift_start)
    end = time.fromisoformat(shift_end)

    if end <= start and local.time() < end:
        return (local.date() - timedelta(days=1)).isoformat()

    return local.date().isoformat()


# =========================================================
# Shift and derived-field calculations
# =========================================================

def shift_start_instant(
    attendance_date: str,
    shift_start: str,
) -> datetime:
    local_start = datetime.combine(
        date.fromisoformat(attendance_date),
        time.fromisoformat(shift_start),
        tzinfo=IST,
    )
    return local_start.astimezone(UTC)


def shift_end_instant(
    attendance_date: str,
    shift_start: str,
    shift_end: str,
) -> datetime:
    local_date = date.fromisoformat(attendance_date)
    start = time.fromisoformat(shift_start)
    end = time.fromisoformat(shift_end)

    if end <= start:
        local_date += timedelta(days=1)

    return datetime.combine(
        local_date,
        end,
        tzinfo=IST,
    ).astimezone(UTC)


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str,
) -> int:
    actual = normalize_utc(punch_in)
    scheduled = shift_start_instant(attendance_date, shift_start)
    elapsed_seconds = (actual - scheduled).total_seconds()

    if elapsed_seconds <= 600:
        return 0

    return int(elapsed_seconds // 60)


def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime,
) -> float:
    start = normalize_utc(punch_in)
    end = normalize_utc(punch_out)

    elapsed_seconds = Decimal(
        str((end - start).total_seconds())
    )
    hours = elapsed_seconds / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    attendance_date: str,
) -> int:
    actual = normalize_utc(punch_out)
    scheduled_end = shift_end_instant(
        attendance_date,
        shift_start,
        shift_end,
    )
    elapsed_seconds = (actual - scheduled_end).total_seconds()

    if elapsed_seconds < 1800:
        return 0

    return int(elapsed_seconds // 60)


def compute_half_day(work_hours: float | None) -> bool:
    return work_hours is not None and work_hours < 4.50


# =========================================================
# Request models
# =========================================================

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: StrictInt | None = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str
    punched_at: StrictInt | None = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )


class RegularizationIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    status: Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"] | None = None
    punch_in: StrictInt | None = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )
    punch_out: StrictInt | None = Field(
        default=None,
        ge=MIN_EPOCH_MS,
        le=MAX_EPOCH_MS,
    )

    @field_validator("reason", "regularized_by")
    @classmethod
    def reject_whitespace_only(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Value must not be blank")
        return value


# =========================================================
# Response serialization
# =========================================================

def serialize_attendance(doc: dict) -> dict:
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": utc_to_epoch_ms(doc.get("punch_in")),
        "punch_out": utc_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }

    for entry in doc.get("history", []):
        item = dict(entry)

        if item.get("at") is not None:
            item["at"] = utc_to_epoch_ms(item["at"])

        normalized_changes = {}

        for field_name, change in item.get("changes", {}).items():
            normalized_change = dict(change)

            for direction in ("from", "to"):
                value = normalized_change.get(direction)
                if isinstance(value, datetime):
                    normalized_change[direction] = utc_to_epoch_ms(value)

            normalized_changes[field_name] = normalized_change

        item["changes"] = normalized_changes
        result["history"].append(item)

    return result


# =========================================================
# Indexes
# =========================================================

def create_indexes() -> None:
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("emp_code", 1)],
        name="ix_employees_department_emp_code",
    )
    db.employees.create_index(
        [("department", 1), ("joined_on", 1)],
        name="ix_employees_department_joined_on",
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_emp_code",
    )
    db.attendance_logs.create_index(
        [("status", 1), ("date", -1), ("emp_code", 1)],
        name="ix_attendance_status_date_emp_code",
    )


@app.on_event("startup")
def initialize_database_indexes() -> None:
    create_indexes()


# =========================================================
# Health
# =========================================================

@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )


# =========================================================
# Employee endpoints
# =========================================================

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    document = body.model_dump()
    document["created_at"] = utc_now_seconds()

    try:
        db.employees.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists",
        )

    document.pop("_id", None)
    return document


@app.get("/employees")
def list_employees(
    department: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    items = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", 1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# =========================================================
# Punch-in
# =========================================================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        punched_at = utc_now_seconds()
    else:
        try:
            punched_at = epoch_ms_to_utc(body.punched_at)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(status_code=422, detail="Invalid punch timestamp")

    attendance_date = attendance_date_for_punch_in(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
    )

    document = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            punched_at,
            employee["shift_start"],
            attendance_date,
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(document)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this employee and date",
        )

    return serialize_attendance(document)


# =========================================================
# Punch-out
# =========================================================

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is None:
        punched_at = utc_now_seconds()
    else:
        try:
            punched_at = epoch_ms_to_utc(body.punched_at)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(status_code=422, detail="Invalid punch timestamp")

    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$type": "date", "$lte": punched_at},
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No matching punch-in record found",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record is already closed",
        )

    punch_in_time = normalize_utc(record["punch_in"])
    elapsed_seconds = (punched_at - punch_in_time).total_seconds()

    if elapsed_seconds <= 0:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be later than punch-in",
        )

    if elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot be more than 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, punched_at)
    overtime_minutes = compute_overtime(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )
    half_day = compute_half_day(work_hours)

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record was already closed by another request",
        )

    return serialize_attendance(updated)


# =========================================================
# Attendance listing
# =========================================================

@app.get("/attendance")
def list_attendance(
    emp_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be on or before date_to",
        )

    query = {}

    if emp_code is not None:
        query["emp_code"] = emp_code

    if date_from or date_to:
        date_filter = {}

        if date_from:
            date_filter["$gte"] = date_from.isoformat()

        if date_to:
            date_filter["$lte"] = date_to.isoformat()

        query["date"] = date_filter

    if status is not None:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)

    docs = list(
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [serialize_attendance(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# =========================================================
# Manual attendance regularization
# =========================================================

@app.patch("/attendance/{emp_code}/{attendance_date}")
def regularize_attendance(
    emp_code: str,
    attendance_date: date,
    body: RegularizationIn,
):
    date_str = attendance_date.isoformat()

    employee = db.employees.find_one({"emp_code": emp_code})

    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    current = db.attendance_logs.find_one(
        {
            "emp_code": emp_code,
            "date": date_str,
        }
    )

    if current is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    # Pydantic's model_fields_set distinguishes an omitted field
    # from a field explicitly supplied as null.
    supplied = body.model_fields_set

    if "punch_in" in supplied and body.punch_in is None:
        raise HTTPException(
            status_code=422,
            detail="punch_in cannot be null when explicitly supplied",
        )

    if "punch_out" in supplied and body.punch_out is None:
        raise HTTPException(
            status_code=422,
            detail="punch_out cannot be null when explicitly supplied",
        )

    old_status = current["status"]
    new_status = body.status if body.status is not None else old_status

    old_punch_in = current.get("punch_in")
    old_punch_out = current.get("punch_out")

    # ABSENT and LEAVE cannot be submitted together with punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot include punch times",
            )

        new_punch_in = None
        new_punch_out = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False

    else:
        # Omitted times retain the currently stored values.
        if "punch_in" in supplied:
            try:
                new_punch_in = epoch_ms_to_utc(body.punch_in)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(
                    status_code=422,
                    detail="Invalid punch_in timestamp",
                )
        else:
            new_punch_in = (
                normalize_utc(old_punch_in)
                if old_punch_in is not None
                else None
            )

        if "punch_out" in supplied:
            try:
                new_punch_out = epoch_ms_to_utc(body.punch_out)
            except (ValueError, OverflowError, OSError):
                raise HTTPException(
                    status_code=422,
                    detail="Invalid punch_out timestamp",
                )
        else:
            new_punch_out = (
                normalize_utc(old_punch_out)
                if old_punch_out is not None
                else None
            )

        # A presence status requires a punch-in time.
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in timestamp",
            )

        # A corrected punch-in must still belong to this attendance date.
        computed_date = attendance_date_for_punch_in(
            new_punch_in,
            employee["shift_start"],
            employee["shift_end"],
        )

        if computed_date != date_str:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date",
            )

        if new_punch_out is not None:
            elapsed_seconds = (
                new_punch_out - new_punch_in
            ).total_seconds()

            if elapsed_seconds <= 0:
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be later than punch_in",
                )

            if elapsed_seconds > 24 * 60 * 60:
                raise HTTPException(
                    status_code=422,
                    detail="Punch-out cannot be more than 24 hours after punch-in",
                )

            new_work_hours = compute_work_hours(
                new_punch_in,
                new_punch_out,
            )
            new_overtime_minutes = compute_overtime(
                new_punch_out,
                employee["shift_start"],
                employee["shift_end"],
                date_str,
            )
            new_half_day = compute_half_day(new_work_hours)

        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

        new_late_minutes = compute_late_minutes(
            new_punch_in,
            employee["shift_start"],
            date_str,
        )

    # Compare normalized instants and legacy defaults.
    old_punch_in_normalized = (
        normalize_utc(old_punch_in) if old_punch_in is not None else None
    )
    old_punch_out_normalized = (
        normalize_utc(old_punch_out) if old_punch_out is not None else None
    )

    old_values = {
        "status": old_status,
        "punch_in": old_punch_in_normalized,
        "punch_out": old_punch_out_normalized,
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }

    new_values = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late_minutes,
        "overtime_minutes": new_overtime_minutes,
        "half_day": new_half_day,
    }

    changes = {}

    for field_name, new_value in new_values.items():
        old_value = old_values[field_name]

        if old_value != new_value:
            changes[field_name] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Regularization does not change any fields",
        )

    history_entry = {
        "at": utc_now_seconds(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency control:
    # the update succeeds only if the record's state and history
    # still match what we read. A competing regularization changes
    # history, so this filter prevents a lost update.
    filter_query = {
        "_id": current["_id"],
        "status": old_status,
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
        "history": current.get("history", []),
    }

    if "history" in current:
        filter_query["history"] = current["history"]
    else:
        filter_query["history"] = {"$exists": False}

    updated = db.attendance_logs.find_one_and_update(
        filter_query,
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry regularization",
        )

    return serialize_attendance(updated)


# =========================================================
# Employee monthly analytics
# =========================================================

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    # Validate the requested month and calculate its date boundaries.
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    last_day = calendar.monthrange(
        month_start.year,
        month_start.month,
    )[1]

    month_end = month_start.replace(day=last_day)

    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1},
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    try:
        joined_on = date.fromisoformat(employee["joined_on"])
    except (ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=500,
            detail="Employee has an invalid joined_on value",
        )

    # Employees who joined after the requested month have no
    # working days or attendance for that month.
    effective_start = max(month_start, joined_on)

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end.isoformat()]},
                                ]
                            }
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": 1,
                            "overtime_minutes": 1,
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "working_dates": {
                    "$cond": [
                        {"$gt": [effective_start.isoformat(), month_end.isoformat()]},
                        [],
                        {
                            "$filter": {
                                "input": {
                                    "$map": {
                                        "input": {
                                            "$range": [
                                                0,
                                                {
                                                    "$add": [
                                                        {
                                                            "$dateDiff": {
                                                                "startDate": {
                                                                    "$dateFromString": {
                                                                        "dateString": effective_start.isoformat(),
                                                                    }
                                                                },
                                                                "endDate": {
                                                                    "$dateFromString": {
                                                                        "dateString": month_end.isoformat(),
                                                                    }
                                                                },
                                                                "unit": "day",
                                                            }
                                                        },
                                                        1,
                                                    ]
                                                },
                                            ]
                                        },
                                        "as": "offset",
                                        "in": {
                                            "$dateAdd": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": effective_start.isoformat(),
                                                    }
                                                },
                                                "unit": "day",
                                                "amount": "$$offset",
                                            }
                                        },
                                    }
                                },
                                "as": "working_date",
                                "cond": {
                                    "$lte": [
                                        {"$isoDayOfWeek": "$$working_date"},
                                        5,
                                    ]
                                },
                            }
                        },
                    ]
                }
            }
        },
        {
            "$addFields": {
                "working_days": {"$size": "$working_dates"},
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date",
                                                            }
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "leave_days": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.late_minutes", 0]},
                        }
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$logs",
                            "as": "log",
                            "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": 1,
                "present_days": 1,
                "leave_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "total_overtime_minutes": 1,
                "attendance_pct": {
                    "$cond": [
                        {"$gt": ["$working_days", 0]},
                        {
                            "$multiply": [
                                {
                                    "$divide": [
                                        "$present_days",
                                        "$working_days",
                                    ]
                                },
                                100,
                            ]
                        },
                        None,
                    ]
                },
            }
        },
    ]

    results = list(db.employees.aggregate(pipeline))

    if not results:
        raise HTTPException(
            status_code=404,
            detail="Employee not found",
        )

    result = results[0]

    # Apply explicit decimal half-up rounding to the reported rate.
    if result["attendance_pct"] is not None:
        result["attendance_pct"] = float(
            Decimal(str(result["attendance_pct"])).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
        )

    return result

# =========================================================
# Late arrival leaderboard
# =========================================================

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(default=10, ge=1, le=50),
    department: str | None = None,
):
    try:
        month_start = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    last_day = calendar.monthrange(
        month_start.year,
        month_start.month,
    )[1]
    month_end = month_start.replace(day=last_day)

    # Match records from the requested month.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                }
            }
        },
        # Missing late_minutes on legacy records counts as zero.
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
            }
        },
        # Employees with no late minutes must not appear.
        {
            "$match": {
                "total_late_minutes": {"$gt": 0}
            }
        },
        # Join employee details. Unknown employee codes are ignored.
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append(
            {
                "$match": {
                    "employee.department": department
                }
            }
        )

    # Competition ranking: employees with equal totals share a rank.
    # Rank is computed before applying the requested limit.
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            # Deterministic order among tied employees.
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "_id": 1,
                }
            },
            # Limit is applied AFTER ranking. Therefore, if the
            # cutoff falls inside a tie, tied employees can exceed it.
            {"$limit": limit},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
        ]
    )

    return list(db.attendance_logs.aggregate(pipeline))

# =========================================================
# Department daily attendance trend
# =========================================================

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    date_from: date = Query(...),
    date_to: date = Query(...),
):
    if date_to < date_from:
        raise HTTPException(
            status_code=422,
            detail="date_to must be on or after date_from",
        )

    day_count = (date_to - date_from).days + 1

    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Return 404 if this department does not exist at all.
    if not db.employees.find_one(
        {"department": department},
        {"_id": 1},
    ):
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    start_string = date_from.isoformat()
    end_string = date_to.isoformat()

    # Start with one seed document, generate the requested calendar
    # dates in MongoDB, then calculate metrics independently per day.
    pipeline = [
        {"$documents": [{}]},
        {
            "$project": {
                "_id": 0,
                "days": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {
                                        "dateString": start_string,
                                    }
                                },
                                "unit": "day",
                                "amount": "$$offset",
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$days"},
        {
            "$project": {
                "_id": 0,
                "date": {
                    "$dateToString": {
                        "date": "$days",
                        "format": "%Y-%m-%d",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$lte": [{"$isoDayOfWeek": "$days"}, 5]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$day"]
                            },
                        }
                    },
                    {"$count": "count"},
                ],
                "as": "headcount_result",
            }
        },
        {
            "$addFields": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$headcount_result.count", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {"$eq": ["$date", "$$day"]}
                        }
                    },
                    {
                        "$lookup": {
                            "from": "employees",
                            "localField": "emp_code",
                            "foreignField": "emp_code",
                            "as": "employee",
                        }
                    },
                    {"$unwind": "$employee"},
                    {
                        "$match": {
                            "employee.department": department
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "status": 1,
                            "half_day": 1,
                            "late_minutes": {
                                "$ifNull": ["$late_minutes", 0]
                            },
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {"$eq": ["$$log.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$gt": ["$$log.late_minutes", 0]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            "$present_count",
                                                            "$headcount",
                                                        ]
                                                    },
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$addFields": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$moving_avg_raw",
                                                    10000,
                                                ]
                                            },
                                            0.5,
                                        ]
                                    }
                                },
                                10000,
                            ]
                        },
                        None,
                    ]
                }
            }
        },
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
        {"$sort": {"date": 1}},
    ]

    return list(db.aggregate(pipeline))

# =========================================================
# analytics/departments/summary
# =========================================================

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    # Convert YYYY-MM into an inclusive month start and exclusive next-month start.
    month_start = f"{month}-01"
    year, month_number = map(int, month.split("-"))

    if month_number == 12:
        next_month_start = f"{year + 1}-01-01"
    else:
        next_month_start = f"{year}-{month_number + 1:02d}-01"

    # Last calendar day of the requested month.
    from datetime import date, timedelta

    month_end = (
        date.fromisoformat(next_month_start) - timedelta(days=1)
    ).isoformat()

    pipeline = [
        # Start with employees so departments with no attendance logs remain visible.
        {
            "$group": {
                "_id": "$department",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$lte": ["$joined_on", month_end]},
                            1,
                            0,
                        ]
                    }
                },
                "employee_codes": {"$push": "$emp_code"},
            }
        },
        # Keep only departments with at least one eligible employee.
        {"$match": {"headcount": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$employee_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "date": {
                                "$gte": month_start,
                                "$lt": next_month_start,
                            },
                            "$expr": {"$in": ["$emp_code", "$$codes"]},
                        }
                    },
                    {
                        "$project": {
                            "_id": 0,
                            "date": 1,
                            "status": 1,
                            "half_day": 1,
                            "work_hours": 1,
                            "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                        }
                    },
                ],
                "as": "logs",
            }
        },
        {
            "$addFields": {
                # Presence records on weekdays; half-days contribute 0.5.
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$lte": [
                                            {
                                                "$isoDayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$$log.date"
                                                    }
                                                }
                                            },
                                            5,
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            },
                        }
                    }
                },
                # Average only presence records with a non-null work_hours value.
                "avg_work_hours": {
                    "$avg": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {"$ne": ["$$log.work_hours", None]},
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": "$$log.work_hours",
                        }
                    }
                },
                "late_records": {
                    "$filter": {
                        "input": "$logs",
                        "as": "log",
                        "cond": {"$gt": ["$$log.late_minutes", 0]},
                    }
                },
                "leave_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "LEAVE"]},
                        }
                    }
                },
                "on_duty_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "late_count": {"$size": "$late_records"},
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$late_records",
                            "as": "log",
                            "in": "$$log.late_minutes",
                        }
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {"$sort": {"department": 1}},
    ]

    return list(db.employees.aggregate(pipeline))


# =========================================================
# /admin/explain/{endpoint}
# =========================================================

from datetime import date, timedelta
from fastapi import HTTPException, Query


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: str | None = None,
    month: str | None = Query(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: str | None = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: date | None = None,
    date_to: date | None = None,
    status: str | None = None,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    allowed = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"endpoint must be one of: {', '.join(sorted(allowed))}",
        )

    def require(value, name):
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"Query parameter '{name}' is required for {endpoint}",
            )
        return value

    def month_bounds(value):
        year, month_number = map(int, value.split("-"))
        start = f"{value}-01"
        if month_number == 12:
            end = f"{year + 1}-01-01"
        else:
            end = f"{year}-{month_number + 1:02d}-01"
        return start, end

    if endpoint == "attendance_list":
        query = {}

        if emp_code is not None:
            query["emp_code"] = emp_code
        if status is not None:
            if status not in {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}:
                raise HTTPException(status_code=422, detail="Invalid status")
            query["status"] = status

        if date_from is not None or date_to is not None:
            date_filter = {}
            if date_from is not None:
                date_filter["$gte"] = date_from.isoformat()
            if date_to is not None:
                date_filter["$lte"] = date_to.isoformat()

            if date_from and date_to and date_from > date_to:
                raise HTTPException(
                    status_code=422,
                    detail="date_from must not be after date_to",
                )

            query["date"] = date_filter

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        emp_code = require(emp_code, "emp_code")
        month = require(month, "month")
        try:
            month_start = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="month must use YYYY-MM format")

        last_day = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = month_start.replace(day=last_day)
        employee = db.employees.find_one(
            {"emp_code": emp_code},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
        if employee is None:
            raise HTTPException(status_code=404, detail="Employee not found")
        try:
            joined_on = date.fromisoformat(employee["joined_on"])
        except (ValueError, TypeError, KeyError):
            raise HTTPException(status_code=500, detail="Employee has an invalid joined_on value")
        effective_start = max(month_start, joined_on)

        # Exact production pipeline: monthly working days, presence, leave,
        # lateness, overtime, and attendance percentage calculations.
        pipeline = [
                {
                    "$match": {
                        "emp_code": emp_code,
                    }
                },
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"employee_code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$emp_code",
                                                    "$$employee_code",
                                                ]
                                            },
                                            {"$gte": ["$date", month_start.isoformat()]},
                                            {"$lte": ["$date", month_end.isoformat()]},
                                        ]
                                    }
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "date": 1,
                                    "status": 1,
                                    "half_day": 1,
                                    "late_minutes": 1,
                                    "overtime_minutes": 1,
                                }
                            },
                        ],
                        "as": "logs",
                    }
                },
                {
                    "$addFields": {
                        "working_dates": {
                            "$cond": [
                                {"$gt": [effective_start.isoformat(), month_end.isoformat()]},
                                [],
                                {
                                    "$filter": {
                                        "input": {
                                            "$map": {
                                                "input": {
                                                    "$range": [
                                                        0,
                                                        {
                                                            "$add": [
                                                                {
                                                                    "$dateDiff": {
                                                                        "startDate": {
                                                                            "$dateFromString": {
                                                                                "dateString": effective_start.isoformat(),
                                                                            }
                                                                        },
                                                                        "endDate": {
                                                                            "$dateFromString": {
                                                                                "dateString": month_end.isoformat(),
                                                                            }
                                                                        },
                                                                        "unit": "day",
                                                                    }
                                                                },
                                                                1,
                                                            ]
                                                        },
                                                    ]
                                                },
                                                "as": "offset",
                                                "in": {
                                                    "$dateAdd": {
                                                        "startDate": {
                                                            "$dateFromString": {
                                                                "dateString": effective_start.isoformat(),
                                                            }
                                                        },
                                                        "unit": "day",
                                                        "amount": "$$offset",
                                                    }
                                                },
                                            }
                                        },
                                        "as": "working_date",
                                        "cond": {
                                            "$lte": [
                                                {"$isoDayOfWeek": "$$working_date"},
                                                5,
                                            ]
                                        },
                                    }
                                },
                            ]
                        }
                    }
                },
                {
                    "$addFields": {
                        "working_days": {"$size": "$working_dates"},
                        "present_days": {
                            "$sum": {
                                "$map": {
                                    "input": {
                                        "$filter": {
                                            "input": "$logs",
                                            "as": "log",
                                            "cond": {
                                                "$and": [
                                                    {
                                                        "$in": [
                                                            "$$log.status",
                                                            ["PRESENT", "WFH", "ON_DUTY"],
                                                        ]
                                                    },
                                                    {
                                                        "$lte": [
                                                            {
                                                                "$isoDayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$log.date",
                                                                    }
                                                                }
                                                            },
                                                            5,
                                                        ]
                                                    },
                                                ]
                                            },
                                        }
                                    },
                                    "as": "log",
                                    "in": {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                }
                            }
                        },
                        "leave_days": {
                            "$size": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {"$eq": ["$$log.status", "LEAVE"]},
                                }
                            }
                        },
                        "late_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$gt": [
                                            {"$ifNull": ["$$log.late_minutes", 0]},
                                            0,
                                        ]
                                    },
                                }
                            }
                        },
                        "total_late_minutes": {
                            "$sum": {
                                "$map": {
                                    "input": "$logs",
                                    "as": "log",
                                    "in": {"$ifNull": ["$$log.late_minutes", 0]},
                                }
                            }
                        },
                        "total_overtime_minutes": {
                            "$sum": {
                                "$map": {
                                    "input": "$logs",
                                    "as": "log",
                                    "in": {"$ifNull": ["$$log.overtime_minutes", 0]},
                                }
                            }
                        },
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "emp_code": 1,
                        "month": {"$literal": month},
                        "working_days": 1,
                        "present_days": 1,
                        "leave_days": 1,
                        "late_count": 1,
                        "total_late_minutes": 1,
                        "total_overtime_minutes": 1,
                        "attendance_pct": {
                            "$cond": [
                                {"$gt": ["$working_days", 0]},
                                {
                                    "$multiply": [
                                        {
                                            "$divide": [
                                                "$present_days",
                                                "$working_days",
                                            ]
                                        },
                                        100,
                                    ]
                                },
                                None,
                            ]
                        },
                    }
                },
            ]

        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    elif endpoint == "department_summary":
        month = require(month, "month")
        try:
            month_start_date = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="month must use YYYY-MM format")
        month_start = month_start_date.isoformat()
        year, month_number = month_start_date.year, month_start_date.month
        if month_number == 12:
            next_month_start = f"{year + 1}-01-01"
        else:
            next_month_start = f"{year}-{month_number + 1:02d}-01"
        month_end = (date.fromisoformat(next_month_start) - timedelta(days=1)).isoformat()

        # Exact production pipeline: eligible headcount includes employees
        # with no attendance logs; metrics are aggregated per department.
        pipeline = [
                # Start with employees so departments with no attendance logs remain visible.
                {
                    "$group": {
                        "_id": "$department",
                        "headcount": {
                            "$sum": {
                                "$cond": [
                                    {"$lte": ["$joined_on", month_end]},
                                    1,
                                    0,
                                ]
                            }
                        },
                        "employee_codes": {"$push": "$emp_code"},
                    }
                },
                # Keep only departments with at least one eligible employee.
                {"$match": {"headcount": {"$gt": 0}}},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"codes": "$employee_codes"},
                        "pipeline": [
                            {
                                "$match": {
                                    "date": {
                                        "$gte": month_start,
                                        "$lt": next_month_start,
                                    },
                                    "$expr": {"$in": ["$emp_code", "$$codes"]},
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "date": 1,
                                    "status": 1,
                                    "half_day": 1,
                                    "work_hours": 1,
                                    "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                                }
                            },
                        ],
                        "as": "logs",
                    }
                },
                {
                    "$addFields": {
                        # Presence records on weekdays; half-days contribute 0.5.
                        "present_days": {
                            "$sum": {
                                "$map": {
                                    "input": {
                                        "$filter": {
                                            "input": "$logs",
                                            "as": "log",
                                            "cond": {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                        }
                                    },
                                    "as": "log",
                                    "in": {
                                        "$cond": [
                                            {
                                                "$lte": [
                                                    {
                                                        "$isoDayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$log.date"
                                                            }
                                                        }
                                                    },
                                                    5,
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {"$eq": ["$$log.half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    },
                                }
                            }
                        },
                        # Average only presence records with a non-null work_hours value.
                        "avg_work_hours": {
                            "$avg": {
                                "$map": {
                                    "input": {
                                        "$filter": {
                                            "input": "$logs",
                                            "as": "log",
                                            "cond": {
                                                "$and": [
                                                    {
                                                        "$in": [
                                                            "$$log.status",
                                                            ["PRESENT", "WFH", "ON_DUTY"],
                                                        ]
                                                    },
                                                    {"$ne": ["$$log.work_hours", None]},
                                                ]
                                            },
                                        }
                                    },
                                    "as": "log",
                                    "in": "$$log.work_hours",
                                }
                            }
                        },
                        "late_records": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {"$gt": ["$$log.late_minutes", 0]},
                            }
                        },
                        "leave_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {"$eq": ["$$log.status", "LEAVE"]},
                                }
                            }
                        },
                        "on_duty_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {"$eq": ["$$log.status", "ON_DUTY"]},
                                }
                            }
                        },
                    }
                },
                {
                    "$addFields": {
                        "late_count": {"$size": "$late_records"},
                        "total_late_minutes": {
                            "$sum": {
                                "$map": {
                                    "input": "$late_records",
                                    "as": "log",
                                    "in": "$$log.late_minutes",
                                }
                            }
                        },
                    }
                },
                {
                    "$project": {
                        "_id": 0,
                        "department": "$_id",
                        "headcount": 1,
                        "present_days": 1,
                        "avg_work_hours": 1,
                        "late_count": 1,
                        "total_late_minutes": 1,
                        "leave_count": 1,
                        "on_duty_count": 1,
                    }
                },
                {"$sort": {"department": 1}},
            ]

        # The production endpoint returns every department. This optional
        # explain-only filter narrows the grouped output without changing its metrics.
        if department is not None:
            pipeline.insert(1, {"$match": {"_id": department}})

        explain = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "employees"

    elif endpoint == "late_leaderboard":
        month = require(month, "month")
        start, end = month_bounds(month)

        # Keep this pipeline in sync with late_leaderboard().
        pipeline = [
            {"$match": {"date": {"$gte": start, "$lt": end}}},
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                }
            },
            {"$match": {"total_late_minutes": {"$gt": 0}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        # Apply the department filter before ranking so ranks are
        # calculated only among employees in the requested department.
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            # Match competition rank, not row count: ties may exceed limit.
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "rank": 1,
                }
            },
        ])

        explain = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )
        collection = "attendance_logs"

    else:  # department_trend
        department = require(department, "department")
        from_date = require(from_date, "from")
        to_date = require(to_date, "to")

        if to_date < from_date:
            raise HTTPException(status_code=422, detail="'to' must not be before 'from'")
        day_count = (to_date - from_date).days + 1
        if day_count > 92:
            raise HTTPException(status_code=422, detail="Date range must not exceed 92 days")
        if not db.employees.find_one({"department": department}, {"_id": 1}):
            raise HTTPException(status_code=404, detail="Unknown department")

        start_string = from_date.isoformat()
        end_string = to_date.isoformat()

        # Exact production pipeline: generate every requested date, calculate
        # eligible headcount and daily attendance, then compute the 7-row average.
        pipeline = [
                {"$documents": [{}]},
                {
                    "$project": {
                        "_id": 0,
                        "days": {
                            "$map": {
                                "input": {"$range": [0, day_count]},
                                "as": "offset",
                                "in": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": start_string,
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                },
                {"$unwind": "$days"},
                {
                    "$project": {
                        "_id": 0,
                        "date": {
                            "$dateToString": {
                                "date": "$days",
                                "format": "%Y-%m-%d",
                                "timezone": "UTC",
                            }
                        },
                        "is_working_day": {
                            "$lte": [{"$isoDayOfWeek": "$days"}, 5]
                        },
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "let": {"day": "$date"},
                        "pipeline": [
                            {
                                "$match": {
                                    "department": department,
                                    "$expr": {
                                        "$lte": ["$joined_on", "$$day"]
                                    },
                                }
                            },
                            {"$count": "count"},
                        ],
                        "as": "headcount_result",
                    }
                },
                {
                    "$addFields": {
                        "headcount": {
                            "$ifNull": [
                                {"$arrayElemAt": ["$headcount_result.count", 0]},
                                0,
                            ]
                        }
                    }
                },
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"day": "$date"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {"$eq": ["$date", "$$day"]}
                                }
                            },
                            {
                                "$lookup": {
                                    "from": "employees",
                                    "localField": "emp_code",
                                    "foreignField": "emp_code",
                                    "as": "employee",
                                }
                            },
                            {"$unwind": "$employee"},
                            {
                                "$match": {
                                    "employee.department": department
                                }
                            },
                            {
                                "$project": {
                                    "_id": 0,
                                    "status": 1,
                                    "half_day": 1,
                                    "late_minutes": {
                                        "$ifNull": ["$late_minutes", 0]
                                    },
                                }
                            },
                        ],
                        "as": "logs",
                    }
                },
                {
                    "$addFields": {
                        "present_count": {
                            "$sum": {
                                "$map": {
                                    "input": {
                                        "$filter": {
                                            "input": "$logs",
                                            "as": "log",
                                            "cond": {
                                                "$in": [
                                                    "$$log.status",
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                        }
                                    },
                                    "as": "log",
                                    "in": {
                                        "$cond": [
                                            {"$eq": ["$$log.half_day", True]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                }
                            }
                        },
                        "late_count": {
                            "$size": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {"$gt": ["$$log.late_minutes", 0]},
                                }
                            }
                        },
                    }
                },
                {
                    "$addFields": {
                        "attendance_rate": {
                            "$cond": [
                                {
                                    "$and": [
                                        "$is_working_day",
                                        {"$gt": ["$headcount", 0]},
                                    ]
                                },
                                {
                                    "$divide": [
                                        {
                                            "$floor": {
                                                "$add": [
                                                    {
                                                        "$multiply": [
                                                            {
                                                                "$divide": [
                                                                    "$present_count",
                                                                    "$headcount",
                                                                ]
                                                            },
                                                            10000,
                                                        ]
                                                    },
                                                    0.5,
                                                ]
                                            }
                                        },
                                        10000,
                                    ]
                                },
                                None,
                            ]
                        }
                    }
                },
                {
                    "$setWindowFields": {
                        "sortBy": {"date": 1},
                        "output": {
                            "moving_avg_raw": {
                                "$avg": "$attendance_rate",
                                "window": {"documents": [-6, 0]},
                            }
                        },
                    }
                },
                {
                    "$addFields": {
                        "moving_avg_7d": {
                            "$cond": [
                                {"$ne": ["$moving_avg_raw", None]},
                                {
                                    "$divide": [
                                        {
                                            "$floor": {
                                                "$add": [
                                                    {
                                                        "$multiply": [
                                                            "$moving_avg_raw",
                                                            10000,
                                                        ]
                                                    },
                                                    0.5,
                                                ]
                                            }
                                        },
                                        10000,
                                    ]
                                },
                                None,
                            ]
                        }
                    }
                },
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
                {"$sort": {"date": 1}},
            ]

        # $documents is a collectionless aggregation stage, so explain the same
        # pipeline with aggregate: 1 rather than attaching it to a collection.
        explain = db.command(
            "explain",
            {"aggregate": 1, "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        collection = "database"

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain,
    }



# TODO:
# - GET /analytics/employees/{emp_code}/monthly
# - GET /analytics/departments/summary
# - GET /analytics/leaderboard/late
# - GET /analytics/departments/{department}/trend
# - GET /admin/explain/{endpoint}