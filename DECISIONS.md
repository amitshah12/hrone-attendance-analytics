# Design Decisions

## 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes, plus indexes on department and join date for employee filtering and headcount calculations. For attendance, a unique compound index on `(emp_code, date)` ensures one record per employee per attendance date and supports atomic conflict handling. Additional indexes on `(date, emp_code)` and `(status, date, emp_code)` support date-range queries and filtered attendance listings.

## 2. Concurrent Punch-ins

Two simultaneous punch-ins cannot both create the same attendance record because MongoDB enforces the unique `(emp_code, date)` index. One insert succeeds; the other encounters a duplicate-key error, which the API converts to HTTP 409. This avoids relying on an unsafe check-then-insert sequence.

## 3. Leaderboard Ties

The leaderboard uses MongoDB ranking so employees with equal late minutes share a rank, and subsequent ranks are skipped. The limit is applied after ranking, allowing tied employees at the cutoff to remain in the response. Ties are ordered by employee code for deterministic output.

## 4. Employees Without Logs

The department summary starts from employees grouped by department, rather than attendance records. It counts employees who joined by the end of the requested month, including employees with no attendance logs, and excludes employees who have not yet joined.

## 5. Scaling Further

For 100 times more data, I would benchmark with representative workloads, inspect execution statistics, and tune indexes against actual query patterns. I would also evaluate aggregation costs, connection-pool settings, and whether frequently requested summaries need pre-aggregation.