# Employee Attendance & Analytics API

A REST API built with Python, FastAPI, and MongoDB for employee management, attendance tracking, and analytics.

## Assignment References

Read these files to understand the requirements and implementation:

1. `PROBLEM_STATEMENT.docx` — assignment requirements and evaluation.
2. `openapi.yaml` — API contract and business rules.
3. `DATA_MODEL.md` — MongoDB data model.
4. `app/main.py` — API implementation.
5. `REVIEW.md` and `DECISIONS.md` — code review findings and design decisions.

## Requirements

- Python 3.11 or newer
- MongoDB 6.0 or newer

## Local Setup (Windows PowerShell)

Run these commands from the project directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Ensure MongoDB is running. By default, the application uses:

- MongoDB URI: `mongodb://localhost:27017`
- Database: `attendance_db`

You can configure `MONGO_URI` and `MONGO_DB` through environment variables.

The project includes `sample_data/` and `sample_seed.py` for the supplied sample dataset. Follow the assignment's data model and seeding instructions when preparing test data.

## Run the API

```powershell
uvicorn app.main:app --port 8000
```

Open Swagger UI at http://127.0.0.1:8000/docs.

## Implementation Status

- Employee creation and listing are implemented.
- Attendance punch-in, punch-out, listing, and correction through PATCH are implemented.
- Attendance corrections include validation and append-only audit history.
- Employee monthly analytics, department summary, late leaderboard, and department trend endpoints are implemented.
- The five supported explain cases have been exercised using MongoDB execution statistics.
- Health endpoint, Python syntax, API route registration, and Git whitespace checks have been verified.
- Selected business rules and analytics cases have been tested through Swagger UI and direct helper checks.
- Additional contract and edge-case verification remains necessary, particularly for concurrency, validation boundaries, and untested requirements.

See `REVIEW.md` for recorded findings, test evidence, and outstanding verification items.

## Submission

Submit a public Git repository containing the required source code, `requirements.txt`, `REVIEW.md`, `DECISIONS.md`, and this README. Keep all implementation code in `app/main.py`, as required by the assignment.

Do not commit `.env` files, credentials, or other secrets. Do not include a Dockerfile.
