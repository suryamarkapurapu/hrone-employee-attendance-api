"""
Employee Attendance & Analytics API - STARTER

Run: uvicorn app.main:app --port 8000
Env: MONGO_URI, MONGO_DB
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Optional
import re
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
    # Employees: emp_code must be unique.
    db.employees.create_index(
        [("emp_code", 1)],
        unique=True,
        name="uq_employees_emp_code",
    )

    db.employees.create_index(
        [("joined_on", 1)],
        name="ix_employees_joined_on",
    )

    # Attendance: one record per employee per attendance date.
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )

    # Attendance listing: supports the required date-descending,
    # employee-code-ascending sort without an in-memory sort.
    db.attendance_logs.create_index(
        [("date", -1), ("emp_code", 1)],
        name="ix_attendance_date_desc_emp_code_asc",
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


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate the required month format: YYYY-MM.
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    # Check that the employee exists.
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0, "emp_code": 1, "joined_on": 1}
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="Employee not found"
        )

    # Calculate the first and last calendar dates of the month.
    month_start_dt = datetime.strptime(
        f"{month}-01", "%Y-%m-%d"
    )

    if month_start_dt.month == 12:
        next_month_dt = datetime(
            month_start_dt.year + 1, 1, 1
        )
    else:
        next_month_dt = datetime(
            month_start_dt.year,
            month_start_dt.month + 1,
            1
        )

    month_start = month_start_dt.date()
    month_end = (next_month_dt - timedelta(days=1)).date()

    # Count weekdays from the later of the joining date and month start.
    joined_on = datetime.strptime(
        employee["joined_on"], "%Y-%m-%d"
    ).date()

    effective_start = max(month_start, joined_on)
    working_days = 0
    current_day = effective_start

    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # Aggregate the employee's attendance records for this month.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat()
                }
            }
        },
        {
            "$addFields": {
                "_day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {
                            "dateString": "$date"
                        }
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"]
                                        ]
                                    },
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    "$_day_of_week",
                                                    [1, 7]
                                                ]
                                            }
                                        ]
                                    }
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                }
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # A month with no attendance records still returns a complete summary.
    if results:
        summary = results[0]
    else:
        summary = {
            "present_days": 0,
            "leave_days": 0,
            "late_count": 0,
            "total_late_minutes": 0,
            "total_overtime_minutes": 0
        }

    present_days = summary["present_days"]

    # Apply ROUND_HALF_UP to the attendance percentage.
    if working_days == 0:
        attendance_pct = None
    else:
        percentage = (
            Decimal(str(present_days))
            / Decimal(working_days)
            * Decimal("100")
        )
        attendance_pct = float(
            percentage.quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": summary["leave_days"],
        "late_count": summary["late_count"],
        "total_late_minutes": summary["total_late_minutes"],
        "total_overtime_minutes": summary["total_overtime_minutes"],
        "attendance_pct": attendance_pct
    }

# GET   /analytics/departments/summary


# GET /analytics/departments/summary
@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    # Validate the reporting month.
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must be in YYYY-MM format"
        )

    month_start_dt = datetime.strptime(month + "-01", "%Y-%m-%d")

    if month_start_dt.month == 12:
        next_month_dt = datetime(month_start_dt.year + 1, 1, 1)
    else:
        next_month_dt = datetime(
            month_start_dt.year,
            month_start_dt.month + 1,
            1
        )

    month_start = month_start_dt.date().isoformat()
    month_end = (next_month_dt - timedelta(days=1)).date().isoformat()

    # Start from employees so employees with no attendance logs
    # are still included in the department headcount.
    employee_match = {
        "joined_on": {"$lte": month_end}
    }
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    },
                    {
                        "$addFields": {
                            "_day_of_week": {
                                "$dayOfWeek": {
                                    "$dateFromString": {"dateString": "$date"}
                                }
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$not": [{"$in": ["$logs._day_of_week", [1, 7]]}]}
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1
                                ]
                            },
                            0
                        ]
                    }
                },
                "work_hours_values": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$logs.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$logs.work_hours", None]}
                                ]
                            },
                            "$logs.work_hours",
                            None
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$logs.status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$headcount"},
                "present_days": 1,
                "avg_work_hours": {
                    "$let": {
                        "vars": {
                            "valid_hours": {
                                "$filter": {
                                    "input": "$work_hours_values",
                                    "as": "hours",
                                    "cond": {"$ne": ["$$hours", None]}
                                }
                            }
                        },
                    "in": {
    "$cond": [
        {"$gt": [{"$size": "$$valid_hours"}, 0]},
        {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": [{"$avg": "$$valid_hours"}, 100]},
                            0.5
                        ]
                    }
                },
                100
            ]
        },
        None
    ]
}
                    }
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = list(db.employees.aggregate(pipeline))

    return {"month": month, "items": items}

# GET   /analytics/leaderboard/late


# GET /analytics/leaderboard/late
@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = 10,
    department: Optional[str] = None,
):
    # Validate the month format.
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    # The contract permits limits from 1 through 50.
    if limit < 1 or limit > 50:
        raise HTTPException(
            status_code=422,
            detail="limit must be between 1 and 50",
        )

    # Build the inclusive calendar-month boundaries.
    month_start = f"{month}-01"

    year, month_number = map(int, month.split("-"))
    if month_number == 12:
        next_month = f"{year + 1:04d}-01-01"
    else:
        next_month = f"{year:04d}-{month_number + 1:02d}-01"

    month_end = (
        datetime.strptime(next_month, "%Y-%m-%d")
        - timedelta(days=1)
    ).strftime("%Y-%m-%d")

    # Aggregate late minutes and late occurrences per employee.
    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start,
                    "$lte": month_end,
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$gt": [
                                    {"$ifNull": ["$late_minutes", 0]},
                                    0,
                                ]
                            },
                            1,
                            0,
                        ]
                    }
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

    # Filter before ranking so ranks are department-specific.
    if department is not None:
        pipeline.append(
            {"$match": {"employee.department": department}}
        )

    # Rank only by total late minutes.
    # Equal totals share a rank; the next rank is skipped.
    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            # Apply the limit after ranking to preserve cutoff ties.
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "employee.emp_code": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$employee.emp_code",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


# GET /analytics/departments/{department}/trend
# GET /admin/explain/{endpoint}

# GET   /analytics/departments/{department}/trend


# GET /analytics/departments/{department}/trend
from fastapi import Query

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to: str = Query(...),
):
    from datetime import date

    # Validate strict YYYY-MM-DD dates.
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to)
        if start_date.isoformat() != from_date or end_date.isoformat() != to:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="'from' and 'to' must use YYYY-MM-DD format",
        )

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    day_count = (end_date - start_date).days + 1
    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail="date range cannot exceed 92 days",
        )

    # A department is known if at least one employee belongs to it.
    if db.employees.count_documents({"department": department}) == 0:
        raise HTTPException(status_code=404, detail="Department not found")

    # Dates are generated inside MongoDB; there is no Python date loop.
    pipeline = [
        {"$match": {"department": department}},
        {
            "$set": {
                "_trend_dates": {
                    "$map": {
                        "input": {"$range": [0, day_count]},
                        "as": "offset",
                        "in": {
                            "$dateToString": {
                                "format": "%Y-%m-%d",
                                "date": {
                                    "$dateAdd": {
                                        "startDate": {
                                            "$dateFromString": {
                                                "dateString": from_date
                                            }
                                        },
                                        "unit": "day",
                                        "amount": "$$offset",
                                    }
                                },
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_trend_dates"},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "trend_date": "$_trend_dates",
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$eq": ["$date", "$$trend_date"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "_daily_logs",
            }
        },
        {
            "$unwind": {
                "path": "$_daily_logs",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$_trend_dates",
                "headcount": {
                    "$sum": {
                        "$cond": [
                            {"$lte": ["$joined_on", "$_trend_dates"]},
                            1,
                            0,
                        ]
                    }
                },
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$in": [
                                    "$_daily_logs.status",
                                    ["PRESENT", "WFH", "ON_DUTY"],
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$_daily_logs.half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$_daily_logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$_id"
                                        }
                                    }
                                },
                                [1, 7],
                            ]
                        }
                    ]
                }
            }
        },
        {
            "$set": {
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
        {"$sort": {"_id": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"_id": 1},
                "output": {
                    "_moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$_moving_avg_raw", None]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    "$_moving_avg_raw",
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
                },
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))
    return {"department": department, "items": items}

# GET   /admin/explain/{endpoint}


# GET /admin/explain/{endpoint}
from fastapi import Query
from bson import json_util


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    allowed_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }

    if endpoint not in allowed_endpoints:
        raise HTTPException(
            status_code=422,
            detail="Unsupported explain endpoint",
        )

    if endpoint == "attendance_list":
        if page < 1 or not 1 <= page_size <= 100:
            raise HTTPException(
                status_code=422,
                detail="Invalid pagination parameters",
            )

        query = {}

        if emp_code:
            query["emp_code"] = emp_code

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        if status:
            query["status"] = status

        explained = db.command({
            "explain": {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size,
                "limit": page_size,
                "projection": {"_id": 0},
            },
            "verbosity": "executionStats",
        })

        collection = "attendance_logs"

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        employee = db.employees.find_one({"emp_code": emp_code})
        if employee is None:
            raise HTTPException(status_code=404, detail="Employee not found")

        start = datetime.strptime(month + "-01", "%Y-%m-%d")
        if start.month == 12:
            next_month = datetime(start.year + 1, 1, 1)
        else:
            next_month = datetime(start.year, start.month + 1, 1)

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": start.date().isoformat(),
                        "$lte": (next_month - timedelta(days=1)).date().isoformat(),
                    },
                }
            },
            {
                "$addFields": {
                    "_day_of_week": {
                        "$dayOfWeek": {
                            "$dateFromString": {"dateString": "$date"}
                        }
                    }
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$not": [
                                                {"$in": ["$_day_of_week", [1, 7]]}
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {
                            "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "total_overtime_minutes": {
                        "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                    },
                }
            },
        ]

        explained = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })
        collection = "attendance_logs"


    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        month_start_dt = datetime.strptime(
            month + "-01", "%Y-%m-%d"
        )

        if month_start_dt.month == 12:
            next_month_dt = datetime(
                month_start_dt.year + 1, 1, 1
            )
        else:
            next_month_dt = datetime(
                month_start_dt.year,
                month_start_dt.month + 1,
                1,
            )

        month_start = month_start_dt.date().isoformat()
        month_end = (
            next_month_dt - timedelta(days=1)
        ).date().isoformat()

        employee_match = {
            "joined_on": {"$lte": month_end}
        }

        if department is not None:
            employee_match["department"] = department

        pipeline = [
            {"$match": employee_match},
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
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        },
                        {
                            "$addFields": {
                                "_day_of_week": {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                }
                            }
                        },
                    ],
                    "as": "logs",
                }
            },
            {
                "$unwind": {
                    "path": "$logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$addToSet": "$emp_code"},
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$not": [
                                                {
                                                    "$in": [
                                                        "$logs._day_of_week",
                                                        [1, 7],
                                                    ]
                                                }
                                            ]
                                        },
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "work_hours_values": {
                        "$push": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {"$ne": ["$logs.work_hours", None]},
                                    ]
                                },
                                "$logs.work_hours",
                                None,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$logs.late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                    },
                    "leave_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "LEAVE"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "on_duty_count": {
                        "$sum": {
                            "$cond": [
                                {"$eq": ["$logs.status", "ON_DUTY"]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": {"$size": "$headcount"},
                    "present_days": 1,
                    "avg_work_hours": {
                        "$let": {
                            "vars": {
                                "valid_hours": {
                                    "$filter": {
                                        "input": "$work_hours_values",
                                        "as": "hours",
                                        "cond": {"$ne": ["$$hours", None]},
                                    }
                                }
                            },
                            "in": {
                                "$cond": [
                                    {
                                        "$gt": [
                                            {"$size": "$$valid_hours"},
                                            0,
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
                                                                    "$avg": "$$valid_hours"
                                                                },
                                                                100,
                                                            ]
                                                        },
                                                        0.5,
                                                    ]
                                                }
                                            },
                                            100,
                                        ]
                                    },
                                    None,
                                ]
                            },
                        }
                    },
                    "late_count": 1,
                    "total_late_minutes": 1,
                    "leave_count": 1,
                    "on_duty_count": 1,
                }
            },
            {"$sort": {"department": 1}},
        ]

        explained = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        collection = "employees"


    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        if limit < 1 or limit > 50:
            raise HTTPException(
                status_code=422,
                detail="limit must be between 1 and 50",
            )

        month_start = f"{month}-01"
        year, month_number = map(int, month.split("-"))

        if month_number == 12:
            next_month = f"{year + 1:04d}-01-01"
        else:
            next_month = f"{year:04d}-{month_number + 1:02d}-01"

        month_end = (
            datetime.strptime(next_month, "%Y-%m-%d")
            - timedelta(days=1)
        ).strftime("%Y-%m-%d")

        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start,
                        "$lte": month_end,
                    }
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {
                        "$sum": {"$ifNull": ["$late_minutes", 0]}
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                                1,
                                0,
                            ]
                        }
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

        if department is not None:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "employee.emp_code": 1,
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "rank": 1,
                    "emp_code": "$employee.emp_code",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ])

        explained = db.command({
            "explain": {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        collection = "attendance_logs"


    elif endpoint == "department_trend":
        if not department or not from_date or not to:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )

        from datetime import date

        try:
            start_date = date.fromisoformat(from_date)
            end_date = date.fromisoformat(to)

            if (
                start_date.isoformat() != from_date
                or end_date.isoformat() != to
            ):
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="'from' and 'to' must use YYYY-MM-DD format",
            )

        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="'to' must be on or after 'from'",
            )

        day_count = (end_date - start_date).days + 1

        if day_count > 92:
            raise HTTPException(
                status_code=422,
                detail="date range cannot exceed 92 days",
            )

        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        # Match the department trend endpoint's aggregation pipeline.
        pipeline = [
            {"$match": {"department": department}},
            {
                "$set": {
                    "_trend_dates": {
                        "$map": {
                            "input": {"$range": [0, day_count]},
                            "as": "offset",
                            "in": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": {
                                                "$dateFromString": {
                                                    "dateString": from_date,
                                                }
                                            },
                                            "unit": "day",
                                            "amount": "$$offset",
                                        }
                                    },
                                }
                            },
                        }
                    }
                }
            },
            {"$unwind": "$_trend_dates"},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "employee_code": "$emp_code",
                        "trend_date": "$_trend_dates",
                    },
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
                                        {
                                            "$eq": [
                                                "$date",
                                                "$$trend_date",
                                            ]
                                        },
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "_daily_logs",
                }
            },
            {
                "$unwind": {
                    "path": "$_daily_logs",
                    "preserveNullAndEmptyArrays": True,
                }
            },
            {
                "$group": {
                    "_id": "$_trend_dates",
                    "headcount": {
                        "$sum": {
                            "$cond": [
                                {"$lte": ["$joined_on", "$_trend_dates"]},
                                1,
                                0,
                            ]
                        }
                    },
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$in": [
                                        "$_daily_logs.status",
                                        ["PRESENT", "WFH", "ON_DUTY"],
                                    ]
                                },
                                {
                                    "$cond": [
                                        {"$eq": ["$_daily_logs.half_day", True]},
                                        0.5,
                                        1,
                                    ]
                                },
                                0,
                            ]
                        }
                    },
                    "late_count": {
                        "$sum": {
                            "$cond": [
                                {"$gt": ["$_daily_logs.late_minutes", 0]},
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
            {
                "$set": {
                    "is_working_day": {
                        "$not": [
                            {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$_id",
                                            }
                                        }
                                    },
                                    [1, 7],
                                ]
                            }
                        ]
                    }
                }
            },
            {
                "$set": {
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
            {"$sort": {"_id": 1}},
            {
                "$setWindowFields": {
                    "sortBy": {"_id": 1},
                    "output": {
                        "_moving_avg_raw": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": "$_id",
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    "moving_avg_7d": {
                        "$cond": [
                            {"$ne": ["$_moving_avg_raw", None]},
                            {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$_moving_avg_raw",
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
                    },
                }
            },
        ]

        explained = db.command({
            "explain": {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        })

        collection = "employees"


    else:
        raise HTTPException(
            status_code=422,
            detail=f"Explain support for {endpoint} is not implemented yet",
        )


    # Convert MongoDB's BSON explain document to ordinary JSON-compatible data.
    explain_json = json_util.loads(json_util.dumps(explained))

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explain_json,
    }
