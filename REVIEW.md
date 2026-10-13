# Code Review — Starter Implementation

I reviewed the original `app/main.py` against the assignment requirements and API contract. The following issues were identified in the starter implementation, along with their symptoms and corrective actions.

## 1. Health endpoint does not check MongoDB connectivity

- **Where:** `health()`
- **Problem:** The endpoint always returns `{"status": "ok"}`, even when MongoDB is unavailable.
- **Symptom:** Stop MongoDB and call `GET /health`. The starter still reports success instead of returning HTTP 503.
- **Fix:** Ping MongoDB and return HTTP 200 only when the ping succeeds; otherwise return HTTP 503.

## 2. Employee creation has a race condition

- **Where:** `create_employee()`
- **Problem:** Checking for an existing employee before inserting is not atomic. Two simultaneous requests can both pass the check.
- **Symptom:** Submit two concurrent requests with the same `emp_code`. Without database-enforced uniqueness, duplicate employees may be created.
- **Fix:** Create a unique index on `employees.emp_code` and handle `DuplicateKeyError` as HTTP 409.

## 3. Employee creation uses a naive timestamp

- **Where:** `create_employee()`, `doc["created_at"]`
- **Problem:** `datetime.now()` produces a naive datetime without explicit UTC information.
- **Symptom:** The stored timestamp can be interpreted inconsistently across environments.
- **Fix:** Store a timezone-aware UTC datetime and return its epoch-millisecond representation where required by the API contract.

## 4. Employee listing calculates the wrong total

- **Where:** `list_employees()`, `total = db.employees.count_documents({})`
- **Problem:** The total ignores the department filter.
- **Symptom:** Request `GET /employees?department=Engineering`. The returned items may contain only Engineering employees, while `total` counts every employee.
- **Fix:** Count documents using the same filter `q` used to retrieve the results.

## 5. Employee pagination skips too many records

- **Where:** `list_employees()`, `skip = page * page_size`
- **Problem:** Page numbering starts at 1, but the offset should be `(page - 1) * page_size`.
- **Symptom:** With `page=1&page_size=20`, the first 20 employees are skipped.
- **Fix:** Calculate the offset using `(page - 1) * page_size`.

## 6. Employee listing has no explicit ordering

- **Where:** `list_employees()`
- **Problem:** The query does not sort employees by `emp_code`.
- **Symptom:** Results may not follow the contract's required ascending employee-code order.
- **Fix:** Sort by `emp_code` ascending before applying pagination.

## 7. Employee listing does not validate pagination parameters

- **Where:** `list_employees()`
- **Problem:** `page` and `page_size` are plain integer parameters without constraints.
- **Symptom:** Values such as `page=0`, negative page sizes, or `page_size=101` are not rejected according to the contract.
- **Fix:** Validate that `page` is at least 1 and `page_size` is between 1 and 100, with a default of 20.

## 8. Punch-in does not validate that the employee exists

- **Where:** `punch_in()`
- **Problem:** The employee lookup result is not checked before accessing `emp["shift_start"]`.
- **Symptom:** Punching in with an unknown employee code can raise an unhandled exception instead of returning HTTP 404.
- **Fix:** Check the employee lookup result and return HTTP 404 when no employee exists.

## 9. Punch-in interprets timestamps using the machine's local time zone

- **Where:** `punch_in()`, `datetime.fromtimestamp(body.punched_at / 1000)`
- **Problem:** `datetime.fromtimestamp()` without a time zone uses the host's local time zone. The API requires UTC instants and IST calendar dates.
- **Symptom:** The same epoch-millisecond timestamp can produce different dates on machines configured with different time zones.
- **Fix:** Convert timestamps explicitly to UTC, truncate them to whole seconds, and derive attendance dates using IST rules.

## 10. Punch-in incorrectly treats zero as a missing timestamp

- **Where:** `punch_in()`, `if body.punched_at`
- **Problem:** The condition checks truthiness instead of whether the value is `None`.
- **Symptom:** A supplied timestamp of `0` is treated as if no timestamp were supplied, rather than being validated and rejected according to the timestamp contract.
- **Fix:** Distinguish omitted timestamps using `body.punched_at is None` and validate supplied timestamps strictly as integer epoch milliseconds within the permitted range.

## 11. Punch-in is vulnerable to concurrent duplicate records

- **Where:** `punch_in()`, duplicate lookup followed by `insert_one()`
- **Problem:** The duplicate check and insertion are separate operations. Two concurrent requests can both pass the check.
- **Symptom:** Simultaneous punch-ins for the same employee and attendance date may both attempt to create records.
- **Fix:** Enforce a unique compound index on `(emp_code, date)` and convert duplicate-key failures to HTTP 409.

## 12. Punch-in does not validate attendance status

- **Where:** `PunchInIn.status`
- **Problem:** The starter accepts an unrestricted string, although punch-in supports only `PRESENT`, `WFH`, and `ON_DUTY`.
- **Symptom:** A request containing an invalid status such as `"HOLIDAY"` can pass request-model validation.
- **Fix:** Restrict the field to the supported status values using a validated enum or literal type.

## 13. Late calculation does not correctly handle overnight shifts

- **Where:** `compute_late_minutes()`
- **Problem:** The helper reconstructs the shift start using the punch-in's calendar date. For an overnight shift, the relevant shift start may fall on the previous date.
- **Symptom:** A punch-in after midnight for a `22:00–06:00` shift is compared against 22:00 on the wrong date.
- **Fix:** Determine the IST attendance date first, construct the correct shift-start instant, and apply the ten-minute grace rule using elapsed time.

## 14. Work-hour rounding does not guarantee half-up rounding

- **Where:** `compute_work_hours()`
- **Problem:** Python's built-in `round()` uses ties-to-even rounding in applicable cases, whereas the contract requires decimal half-up rounding.
- **Symptom:** Values exactly at a rounding midpoint can produce a different result from the required business rule.
- **Fix:** Calculate elapsed seconds and round using `Decimal` with `ROUND_HALF_UP`.

## 15. Overtime calculation mishandles overnight shifts and the minimum threshold

- **Where:** `compute_overtime()`
- **Problem:** The helper always constructs shift end on the attendance date and returns all positive overtime minutes. It does not handle next-day shift endings correctly or enforce the 30-minute minimum.
- **Symptom:** For a `22:00–06:00` shift, the shift-end instant is constructed on the wrong date. Overtime of 29 minutes is also incorrectly counted.
- **Fix:** Construct the shift-end instant on the correct date, calculate whole elapsed minutes, and count overtime only when it is at least 30 minutes.

## 16. Attendance listing loads the entire matching dataset into Python

- **Where:** `list_attendance()`, `docs = list(db.attendance_logs.find(q))`
- **Problem:** Every matching record is loaded into application memory before sorting and pagination.
- **Symptom:** Query latency and memory use grow substantially as the attendance collection grows.
- **Fix:** Create appropriate indexes and perform sorting, counting, skipping, and limiting in MongoDB instead of fetching all records.

## 17. Attendance listing does not use a deterministic sort

- **Where:** `list_attendance()`, `docs.sort(...)`
- **Problem:** The starter sorts only by date descending. Multiple records with the same date have no explicit tie-breaker.
- **Symptom:** Records sharing a date may appear in inconsistent relative order across paginated requests.
- **Fix:** Sort by date descending and `emp_code` ascending in MongoDB.

## 18. Attendance listing exposes MongoDB internal identifiers

- **Where:** `list_attendance()`
- **Problem:** The query does not exclude `_id`, and the response loop converts `_id` into an `id` field that is not part of the API contract.
- **Symptom:** The response contains database-specific identifiers instead of only the contract's attendance fields.
- **Fix:** Exclude `_id` from the response projection and return the documented response fields.

## 19. Required database indexes are not created at startup

- **Where:** Module initialization and database setup
- **Problem:** The starter defines no index-creation routine.
- **Symptom:** Unique constraints are not guaranteed by MongoDB, and filtered queries may require expensive collection scans.
- **Fix:** Create the required unique and query-supporting indexes during application startup, using idempotent index creation.

## 20. Additional endpoints required by the contract are missing

- **Where:** The `TODO` section at the end of the starter file
- **Problem:** Punch-out, regularization, all four analytics operations, and the explain endpoint are not implemented.
- **Symptom:** Requests to the required routes return HTTP 404 because the routes do not exist.
- **Fix:** Implement the missing endpoints according to `openapi.yaml`, using MongoDB aggregation pipelines for analytics and appropriate validation and concurrency controls for write operations.

## Items examined and found acceptable

- **Employee filter construction:** The starter uses an exact department match when a department is supplied. The filtering expression itself is reasonable; the total-count calculation is the defect.
- **Attendance date-range filter:** The `$gte` and `$lte` conditions correctly express an inclusive string-date range, assuming valid normalized `YYYY-MM-DD` values.
- **Punch-in initial derived fields:** Initializing `punch_out` and `work_hours` to `None`, `overtime_minutes` to `0`, `half_day` to `False`, and `history` to an empty list is consistent with the required initial state.
- **Default pagination values:** Defaults of page 1 and page size 20 are correct. The defects are the incorrect offset calculation and missing validation.
