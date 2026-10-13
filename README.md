# Employee Attendance & Analytics API

A REST API built with **FastAPI, Python, and MongoDB** for employee management, attendance tracking, attendance regularization, and workforce analytics.

## Features

- Employee creation and paginated employee listing.
- Attendance punch-in and punch-out.
- Attendance filtering by employee, date range, and status.
- Attendance regularization with an append-only audit history.
- Shift-aware calculations for late arrivals, overtime, work hours, and half-days.
- Monthly employee attendance analytics.
- Department-level attendance summaries.
- Late-arrival leaderboard with competition ranking and tie handling.
- Department attendance trends with seven-day moving averages.
- MongoDB query-plan inspection through an explain endpoint.
- Database indexes created automatically at application startup.

## Technology Stack

- Python 3.11+
- FastAPI
- Pydantic
- PyMongo
- MongoDB 6+
- Uvicorn
- python-dotenv

## Project Structure

```text
project-root/
├── app/
│   └── main.py
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
├── openapi.yaml
├── DATA_MODEL.md
└── sample_data/
    ├── employees.json
    └── attendance_logs.json
```

The exact repository may contain additional files. Keep local virtual environments and secret configuration files out of version control.

## Prerequisites

Install the following before running the API:

- Python 3.11 or newer
- MongoDB 6 or newer, running locally or hosted remotely
- Git, if cloning the repository

## Installation and Setup

### 1. Clone the repository

```bash
git clone <YOUR_PUBLIC_REPOSITORY_URL>
cd <YOUR_REPOSITORY_DIRECTORY>
```

Replace the placeholders with your repository URL and directory name.

### 2. Create a virtual environment

**Windows — Command Prompt**

```bat
python -m venv .venv
.venv\Scripts\activate
```

**macOS/Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install dependencies

```bash
python -m pip install -r requirements.txt
```

### 4. Configure MongoDB

The application reads `MONGO_URI` and `MONGO_DB` from environment variables. A local `.env` file is supported for development, and real environment variables take precedence over values loaded from that file.

Example local configuration:

```dotenv
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Make sure MongoDB is running and reachable before starting the API.

**Security:** Do not commit `.env` files, credentials, or production connection strings. Configure production secrets through the deployment environment.

### 5. Start the application

From the project root:

```bash
uvicorn app.main:app --port 8000
```

The API is available at:

- Base URL: `http://localhost:8000`
- Interactive Swagger documentation: `http://localhost:8000/docs`
- OpenAPI JSON: `http://localhost:8000/openapi.json`

The application creates its configured MongoDB indexes at startup. No manual index creation should be necessary.

## Health Check

Send a request to:

```http
GET /health
```

The endpoint returns HTTP 200 when MongoDB is reachable and HTTP 503 when the database health check fails.

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Check application/database health |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees with optional department filtering and pagination |
| POST | `/attendance/punch-in` | Record attendance punch-in |
| POST | `/attendance/punch-out` | Record attendance punch-out |
| GET | `/attendance` | Filter and paginate attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly employee analytics |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Rank employees by late minutes |
| GET | `/analytics/departments/{department}/trend` | Daily department attendance trend |
| GET | `/admin/explain/{endpoint}` | Inspect MongoDB execution statistics |

For request schemas, required fields, query parameters, and response formats, refer to the interactive API documentation at `/docs` and the supplied `openapi.yaml`.

## Example Requests

### Create an employee

```http
POST /employees
Content-Type: application/json
```

```json
{
  "emp_code": "EMP1001",
  "name": "Example Employee",
  "email": "employee@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05"
}
```

Use a unique employee code and a valid email address. The API sets `created_at` itself.

### Punch in

```http
POST /attendance/punch-in
Content-Type: application/json
```

```json
{
  "emp_code": "EMP1001",
  "status": "PRESENT"
}
```

The `punched_at` field is optional. When supplied, it must be an integer Unix epoch timestamp in milliseconds.

### Monthly employee analytics

```http
GET /analytics/employees/EMP0001/monthly?month=2026-07
```

### Department summary

```http
GET /analytics/departments/summary?month=2026-07
```

### Late-arrival leaderboard

```http
GET /analytics/leaderboard/late?month=2026-07&limit=10
```

### Department trend

```http
GET /analytics/departments/Engineering/trend?date_from=2026-07-06&date_to=2026-07-12
```

### Explain a query

```http
GET /admin/explain/attendance_list?emp_code=EMP0001&page=1&page_size=20
```

Use `/docs` to inspect the exact parameters supported by each endpoint.

## Business Rules

- Instants are accepted and returned as integer Unix epoch milliseconds; MongoDB stores them as BSON datetimes.
- Attendance dates and shift times follow IST (UTC+05:30).
- Overnight shifts are supported.
- Late arrivals use a ten-minute grace period.
- Overtime is counted only when it reaches at least 30 minutes.
- Work hours and reported rates use the specified half-up rounding rules.
- `PRESENT`, `WFH`, and `ON_DUTY` count as presence; `ABSENT` and `LEAVE` do not.
- Half-days contribute 0.5 to present-day totals.
- Working days are Monday through Friday; no holiday calendar is applied.
- Attendance regularizations append audit-history entries. Punch-in and punch-out operations do not create history entries.
- Analytics use stored derived fields rather than recalculating them from punch timestamps.

Refer to `openapi.yaml` for the full API contract and `DATA_MODEL.md` for the database model.

## MongoDB Indexes

The application creates indexes at startup to support uniqueness and query performance. The configured indexes include:

| Collection | Index | Purpose |
|---|---|---|
| `employees` | Unique `emp_code` | Enforce unique employee codes |
| `employees` | `(department, emp_code)` | Department filtering and ordered employee listing |
| `employees` | `(department, joined_on)` | Department headcount and join-date queries |
| `attendance_logs` | Unique `(emp_code, date)` | Enforce one attendance record per employee per date |
| `attendance_logs` | `(date, emp_code)` | Date-range queries and attendance listing |
| `attendance_logs` | `(status, date, emp_code)` | Status-filtered attendance queries |

Use the explain endpoint to inspect query execution statistics and index usage.

## Testing and Validation

The API can be exercised using Swagger UI at `http://localhost:8000/docs`.

Recommended checks include:

- Valid and invalid employee creation.
- Duplicate employee codes and concurrent punch-ins.
- Punch-out conflicts and missing attendance records.
- Timestamp validation and rounding boundaries.
- Overnight shifts and overtime thresholds.
- Regularization history and concurrent updates.
- Pagination, filters, and total counts.
- Employees with no attendance logs and employees who joined mid-month.
- Leaderboard ties and cutoff behavior.
- Weekend rows and moving-average calculations.
- MongoDB execution plans for the analytics and listing endpoints.

Only describe a test as passed after running it and verifying its result.

## Sample Data

The repository may include sample employee and attendance JSON files for local testing. If you use `sample_seed.py`, inspect it first: the supplied seeding script deletes existing documents in its target collections before inserting the sample dataset.

**Run the seeding script only against a disposable development database.** Never run it against production or data you need to preserve.

## Error Handling

The API follows the contract's HTTP status codes:

- `404 Not Found` — required employee or attendance record does not exist.
- `409 Conflict` — duplicate records or conflicting state transitions.
- `422 Unprocessable Entity` — invalid request data or business-rule violations.
- `503 Service Unavailable` — MongoDB health check fails.

## Design and Review Documents

- `REVIEW.md` documents issues found in the original starter implementation and their corrections.
- `DECISIONS.md` explains the main indexing, concurrency, ranking, headcount, and scaling decisions.
- `openapi.yaml` defines the API contract.
- `DATA_MODEL.md` describes the MongoDB data model.

## Limitations

- Working days are Monday through Friday; public holidays are not modeled.
- The implementation is designed for the assignment's attendance-tracking and analytics requirements.
- Performance at larger data volumes should be evaluated with representative datasets and MongoDB execution statistics.

## License and Assignment Use

This repository was prepared as part of a software engineering assignment. Follow the assignment's submission, confidentiality, and publication instructions when sharing the repository.
