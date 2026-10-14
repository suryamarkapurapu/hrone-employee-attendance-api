# DECISIONS.md

1. Indexes

I created a unique index on employees.emp_code to prevent duplicate employee codes and a unique compound index on attendance_logs.emp_code and date to enforce one attendance record per employee per day. The joined_on index supports employee eligibility filtering, the attendance date-and-employee-code index supports listing and monthly filtering, and the department index supports department-trend queries. I would add further indexes only when query plans justify their read and write costs.

2. Punch-in race

Concurrent punch-in requests can both pass an initial existence check. The unique compound index allows only one insert; duplicate-key errors become HTTP 409 Conflict. A test with ten simultaneous requests returned one HTTP 201 and nine HTTP 409 responses.

3. Leaderboard ties

The leaderboard uses standard competition ranking. Employees with equal late-minute totals share a rank. Applying the limit to ranks rather than row count preserves ties at the cutoff.

4. Department headcount

The department summary starts from employees and looks up attendance records, preserving employees with no logs. Tests verified inclusion of employees without attendance and exclusion of employees who joined after the reporting month.

5. Scaling

For 100 times more data, I would measure execution statistics on representative datasets, inspect index usage, and benchmark latency. I would optimize demonstrated bottlenecks and consider caching expensive analytics if appropriate. Local explain tests use indexes for all five supported query patterns, but performance on 100,000 records still requires dedicated testing.

### 6. Punch-out calculations

Punch-out stores the closing timestamp and derives work hours, overtime, and half-day status. MongoDB datetimes are normalized before comparison with timezone-aware timestamps. Work hours use half-up rounding, overtime requires at least 30 whole minutes beyond shift end, and half-day status depends on rounded work hours being below 4.50.

Basic punch-out behavior, the exact 24-hour boundary, rejection beyond 24 hours, and selected half-day calculations have been tested. Additional overnight and overtime threshold tests remain to be verified through the endpoint.

### 7. Attendance correction and audit history

Manual attendance corrections use PATCH `/attendance/{emp_code}/{date}`. The API preserves omitted fields, recalculates derived values, and appends a history entry for each successful correction. No-op requests are rejected with HTTP 422. Conditional updates are intended to detect conflicting concurrent corrections.

Basic status corrections, no-op rejection, timestamp validation, invalid status handling, absent/leave punch-time validation, and history preservation have been tested. Concurrent correction behavior and additional timestamp edge cases remain to be verified.

