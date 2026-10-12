"""
Employee Attendance & Analytics API - STARTER

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, StrictInt
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from decimal import Decimal, ROUND_HALF_UP

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017")
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0"
)

@app.on_event("startup")
def create_indexes():
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code"
    )

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def get_attendance_date(
    punch_in: datetime,
    shift_start: str,
    shift_end: str
) -> str:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    # A shift ending at or before its start crosses midnight.
    overnight = (end_h, end_m) <= (start_h, start_m)

    # Early-morning punches belong to the previous shift day.
    if overnight and (punch_in.hour, punch_in.minute) < (end_h, end_m):
        return (punch_in.date() - timedelta(days=1)).isoformat()

    return punch_in.date().isoformat()



def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    attendance_date: str
) -> int:
    h, m = map(int, shift_start.split(":"))

    shift_start_dt = datetime.fromisoformat(
        attendance_date
    ).replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    elapsed_seconds = (
        punch_in - shift_start_dt
    ).total_seconds()

    # No lateness within the 10-minute grace period.
    if elapsed_seconds <= 600:
        return 0

    # Count whole minutes from the actual shift start.
    return int(elapsed_seconds // 60)



def compute_work_hours(
    punch_in: datetime,
    punch_out: datetime
) -> float:
    elapsed_seconds = Decimal(
        str((punch_out - punch_in).total_seconds())
    )

    hours = elapsed_seconds / Decimal(3600)

    # Assignment requires ROUND_HALF_UP, not Python's round().
    rounded_hours = hours.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )

    return float(rounded_hours)


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str
) -> int:
    start_h, start_m = map(int, shift_start.split(":"))
    end_h, end_m = map(int, shift_end.split(":"))

    shift_start_dt = datetime.fromisoformat(date_str).replace(
        hour=start_h,
        minute=start_m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    shift_end_dt = datetime.fromisoformat(date_str).replace(
        hour=end_h,
        minute=end_m,
        second=0,
        microsecond=0,
        tzinfo=IST
    )

    # If the shift ends at or before it starts, it crosses midnight.
    if shift_end_dt <= shift_start_dt:
        shift_end_dt += timedelta(days=1)

    overtime_seconds = (punch_out - shift_end_dt).total_seconds()
    overtime_minutes = max(0, int(overtime_seconds // 60))

    # Overtime is counted only when it reaches 30 whole minutes.
    return overtime_minutes if overtime_minutes >= 30 else 0

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = None


class RegularizeRequest(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[StrictInt] = None
    punch_out: Optional[StrictInt] = None
    reason: str
    regularized_by: str


# --------------------------------------------------------------------------- #
# Health endpoint
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable"
        )


# --------------------------------------------------------------------------- #
# Employee endpoints
# --------------------------------------------------------------------------- #

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(
            status_code=409,
            detail="emp_code already exists"
        )

    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)

    try:
       db.employees.insert_one(doc)
    except DuplicateKeyError:
       raise HTTPException(
        status_code=409,
        detail="emp_code already exists"
    )
    doc.pop("_id", None)

    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    q = {}

    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)

    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


# --------------------------------------------------------------------------- #
# Punch-in endpoint
# --------------------------------------------------------------------------- #

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # 1. Validate that the employee exists.
    emp = db.employees.find_one({"emp_code": body.emp_code})

    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    # 2. Convert the timestamp to IST and truncate to whole seconds.
    if body.punched_at is None:
        ts = datetime.now(timezone.utc).astimezone(IST)
        ts = ts.replace(microsecond=0)
    else:
        # Reject values that look like epoch seconds.
        if body.punched_at < 100_000_000_000:
            raise HTTPException(
                status_code=422,
                detail="punched_at must be epoch milliseconds"
            )

        try:
            ts = datetime.fromtimestamp(
                body.punched_at // 1000,
                tz=timezone.utc
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="punched_at is out of range"
            )

    # 3. Derive the attendance date from the IST timestamp.
    # 3. Derive the attendance date using the employee's shift.
        d = get_attendance_date(
           ts,
           emp["shift_start"],
           emp["shift_end"]
        )

    # 4. Reject an existing attendance record for this employee and date.
    existing = db.attendance_logs.find_one({
        "emp_code": body.emp_code,
        "date": d
    })

    if existing:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date"
        )

    # 5. Create the attendance record.
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
                  ts,
               emp["shift_start"],
                 d
              ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
      res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
      raise HTTPException(
        status_code=409,
        detail="already punched in for this date"
    )


    # Return timestamps as epoch milliseconds.
    doc["punch_in"] = int(ts.timestamp() * 1000)
    doc["punch_out"] = None

    doc.pop("_id", None)

    return doc



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    # Validate and convert the timestamp.
    if body.punched_at is None:
        ts = datetime.now(timezone.utc).astimezone(IST)
        ts = ts.replace(microsecond=0)
    else:
        if body.punched_at < 100_000_000_000:
            raise HTTPException(
                status_code=422,
                detail="punched_at must be epoch milliseconds"
            )
        try:
            ts = datetime.fromtimestamp(
                body.punched_at // 1000,
                tz=timezone.utc
            ).astimezone(IST)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail="punched_at is out of range"
            )


    # Find the most recent attendance record for this employee
    # without filtering by the submitted punch-out timestamp.
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code},
        sort=[("punch_in", -1)]
    )

    if record is None:
        raise HTTPException(
            status_code=404,
            detail="No punch-in record found"
        )



    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Already punched out"
        )

    # A punch-out must be after punch-in and within 24 hours.
    punch_in_ts = record["punch_in"]

# MongoDB may return a naive datetime representing UTC.
    if punch_in_ts.tzinfo is None:
       punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)

# Convert the stored timestamp to IST before calculations.
    punch_in_ts = punch_in_ts.astimezone(IST)

    elapsed_seconds = (ts - punch_in_ts).total_seconds()

    if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punch-out must be after punch-in and within 24 hours"
        )

    # Calculate derived fields.
    work_hours = compute_work_hours(punch_in_ts, ts)
    overtime_minutes = compute_overtime(
        ts,
        emp["shift_start"],
        emp["shift_end"],
        record["date"]
    )
    half_day = work_hours < 4.50

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day
            }
        }
    )

    # If another request closed the record first, report a conflict.
    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Already punched out"
        )

    updated_record = db.attendance_logs.find_one(
        {"_id": record["_id"]}
    )

    if updated_record is None:
        raise HTTPException(
            status_code=500,
            detail="Attendance record could not be retrieved"
        )


    # Convert punch timestamps to epoch milliseconds.
    for field in ("punch_in", "punch_out"):
        value = updated_record.get(field)

        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            updated_record[field] = int(value.timestamp() * 1000)

    # History timestamps must also use epoch milliseconds.
    for entry in updated_record.get("history", []):
        value = entry.get("at")

        if value is not None:
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)

            entry["at"] = int(value.timestamp() * 1000)

    updated_record.pop("_id", None)
    return updated_record



# --------------------------------------------------------------------------- #
# Attendance listing endpoint
# --------------------------------------------------------------------------- #


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20
):
    # Validate pagination.
    if page < 1:
        raise HTTPException(
            status_code=422,
            detail="page must be at least 1"
        )

    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="page_size must be between 1 and 100"
        )

    # Validate date range.
    if date_from:
        try:
            parsed_from = datetime.strptime(date_from, "%Y-%m-%d")
            if parsed_from.strftime("%Y-%m-%d") != date_from:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="date_from must use YYYY-MM-DD"
            )

    if date_to:
        try:
            parsed_to = datetime.strptime(date_to, "%Y-%m-%d")
            if parsed_to.strftime("%Y-%m-%d") != date_to:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="date_to must use YYYY-MM-DD"
            )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to"
        )

    # Build the same filter for counting and fetching.
    q = {}

    if emp_code:
        q["emp_code"] = emp_code

    if date_from or date_to:
        q["date"] = {}

        if date_from:
            q["date"]["$gte"] = date_from

        if date_to:
            q["date"]["$lte"] = date_to

    if status:
        q["status"] = status

    total = db.attendance_logs.count_documents(q)

    # Let MongoDB sort and paginate instead of loading every record.
    page_docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )


    # Convert punch timestamps from MongoDB datetimes
    # to epoch milliseconds for the API response.
    for doc in page_docs:
        for field in ("punch_in", "punch_out"):
            value = doc.get(field)

            if value is not None:
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)

                doc[field] = int(value.timestamp() * 1000)

        # Convert audit-history timestamps too.
        for entry in doc.get("history", []):
            if entry.get("at") is not None:
                value = entry["at"]

                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)

                entry["at"] = int(value.timestamp() * 1000)

    return {
        "items": page_docs,
        "total": total,
        "page": page,
        "page_size": page_size
    }




# --------------------------------------------------------------------------- #
# TODO: Remaining assignment endpoints
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# PATCH /attendance/{emp_code}/{date}
# --------------------------------------------------------------------------- #

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeRequest
):
    # 1. Validate the request's text fields.
    if not 5 <= len(body.reason) <= 200:
        raise HTTPException(
            status_code=422,
            detail="reason must contain 5 to 200 characters"
        )

    if not 1 <= len(body.regularized_by) <= 50:
        raise HTTPException(
            status_code=422,
            detail="regularized_by must contain 1 to 50 characters"
        )

    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }

    if body.status is not None and body.status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="Invalid attendance status"
        )

    # Validate the path date.
    try:
        requested_date = datetime.strptime(date, "%Y-%m-%d").date()
        if requested_date.isoformat() != date:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="date must use YYYY-MM-DD format"
        )

    # 2. Confirm the employee and attendance record exist.
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": date
    })
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found"
        )

    # 3. Convert optional epoch-millisecond values into IST datetimes.
    def parse_timestamp(value, field_name):
        if value is None:
            return None

        if value < 100_000_000_000:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must be epoch milliseconds"
            )

        try:
            parsed = datetime.fromtimestamp(
                value // 1000,
                tz=timezone.utc
            ).astimezone(IST)
            return parsed.replace(microsecond=0)
        except (ValueError, OverflowError, OSError):
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} is out of range"
            )

    supplied_fields = body.model_fields_set

    # Begin with the existing values. Omitted fields remain unchanged.
    old_status = record["status"]
    old_in = record.get("punch_in")
    old_out = record.get("punch_out")

    if old_in is not None and old_in.tzinfo is None:
        old_in = old_in.replace(tzinfo=timezone.utc).astimezone(IST)
    elif old_in is not None:
        old_in = old_in.astimezone(IST)

    if old_out is not None and old_out.tzinfo is None:
        old_out = old_out.replace(tzinfo=timezone.utc).astimezone(IST)
    elif old_out is not None:
        old_out = old_out.astimezone(IST)

    new_status = body.status if body.status is not None else old_status
    new_in = (
        parse_timestamp(body.punch_in, "punch_in")
        if "punch_in" in supplied_fields else old_in
    )
    new_out = (
        parse_timestamp(body.punch_out, "punch_out")
        if "punch_out" in supplied_fields else old_out
    )

    # 4. ABSENT and LEAVE must not have punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied_fields or "punch_out" in supplied_fields:
            raise HTTPException(
                status_code=422,
                detail="Do not supply punch times for ABSENT or LEAVE"
            )
        new_in = None
        new_out = None

    # Presence statuses require a punch-in.
    elif new_in is None:
        raise HTTPException(
            status_code=422,
            detail="Presence statuses require punch_in"
        )

    # 5. Validate punch-in against the attendance date (R1).
    if new_in is not None:
        shift_start = emp["shift_start"]
        shift_end = emp["shift_end"]

        start_h, start_m = map(int, shift_start.split(":"))
        end_h, end_m = map(int, shift_end.split(":"))

        overnight = (end_h, end_m) <= (start_h, start_m)
        attendance_day = new_in.date()

        if overnight and (new_in.hour, new_in.minute) < (end_h, end_m):
            attendance_day -= timedelta(days=1)

        if attendance_day.isoformat() != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in must belong to the attendance date"
            )

    # 6. Validate punch-out and calculate derived values.
    if new_out is not None:
        if new_in is None:
            raise HTTPException(
                status_code=422,
                detail="punch_out requires punch_in"
            )

        elapsed_seconds = (new_out - new_in).total_seconds()
        if elapsed_seconds <= 0 or elapsed_seconds > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours"
            )

    new_late = (
    compute_late_minutes(
        new_in,
        emp["shift_start"],
        date
    )
    if new_in is not None else 0
)

    if new_in is not None and new_out is not None:
        new_work = compute_work_hours(new_in, new_out)
        new_overtime = compute_overtime(
            new_out,
            emp["shift_start"],
            emp["shift_end"],
            date
        )
        new_half_day = new_work < 4.50
    else:
        new_work = None
        new_overtime = 0
        new_half_day = False

    # 7. Build changes using only values that actually changed.
    def epoch_ms(value):
        if value is None:
            return None
        return int(value.timestamp() * 1000)

    old_values = {
        "status": old_status,
        "punch_in": epoch_ms(old_in),
        "punch_out": epoch_ms(old_out),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False)
    }

    new_values = {
        "status": new_status,
        "punch_in": epoch_ms(new_in),
        "punch_out": epoch_ms(new_out),
        "work_hours": new_work,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day
    }

    changes = {
        key: {"from": old_values[key], "to": new_values[key]}
        for key in old_values
        if old_values[key] != new_values[key]
    }

    if not changes:
        raise HTTPException(
            status_code=422,
            detail="The correction does not change the record"
        )

    # 8. Append one audit entry and update the record atomically.
    now = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": old_status,
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
            "history": record.get("history", [])
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_in,
                "punch_out": new_out,
                "work_hours": new_work,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day
            },
            "$push": {"history": history_entry}
        }
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="Attendance record changed concurrently; retry the correction"
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated is None:
        raise HTTPException(
            status_code=500,
            detail="Updated attendance record could not be retrieved"
        )

    updated.pop("_id", None)

    # Convert timestamps in the response to epoch milliseconds.
    for field in ("punch_in", "punch_out"):
        if updated.get(field) is not None:
            value = updated[field]
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    for entry in updated.get("history", []):
        at = entry["at"]
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        entry["at"] = int(at.timestamp() * 1000)

    return updated

# POST  /attendance/punch-out
# PATCH /attendance/{emp_code}/{date}
# GET   /analytics/employees/{emp_code}/monthly
# GET   /analytics/departments/summary
# GET   /analytics/leaderboard/late
# GET   /analytics/departments/{department}/trend
# GET   /admin/explain/{endpoint}
