# DECISIONS.md

### 1. Indexes

I created a unique index on `employees.emp_code` to prevent duplicate employee codes. I also created a unique compound index on `attendance_logs.emp_code` and `date` to allow only one attendance record per employee per day. I would avoid a standalone index on a low-selectivity field such as status unless query performance measurements justified it.

### 2. Punch-in race

Two identical punch-in requests may both pass the initial existence check. The unique compound index allows only one insert. The other insert raises `DuplicateKeyError`, which the API catches and converts to HTTP 409 Conflict. The unique indexes and sequential duplicate rejection have been verified. Simultaneous requests still need dedicated concurrency testing.

### 3. Ties

The late-arrival leaderboard uses standard competition ranking. If employees tie at the limit, all employees whose rank is less than or equal to the limit should be returned, rather than splitting a tie. The tested leaderboard returned tied employees with the expected shared ranks.

### 4. Headcount

The department summary starts from employees and uses an aggregation with attendance records. This keeps employees with no attendance logs in the headcount instead of excluding them. Tests verified inclusion of employees without logs and exclusion of employees joining after the reporting month.

### 5. Scaling

For 100 times more data, I would measure query plans and execution times, add indexes for demonstrated query patterns, and consider caching expensive analytics if necessary. The explain endpoint has been exercised for the five supported query patterns, but larger-scale performance testing remains necessary.

### 6. Punch-out calculations

Punch-out stores the closing timestamp and derives work hours, overtime, and half-day status. MongoDB datetimes are normalized before comparison with timezone-aware timestamps. Work hours use half-up rounding, overtime requires at least 30 whole minutes beyond shift end, and half-day status depends on rounded work hours being below 4.50.

Basic punch-out behavior, the exact 24-hour boundary, rejection beyond 24 hours, and selected half-day calculations have been tested. Additional overnight and overtime threshold tests remain to be verified through the endpoint.

### 7. Attendance correction and audit history

Manual attendance corrections use PATCH `/attendance/{emp_code}/{date}`. The API preserves omitted fields, recalculates derived values, and appends a history entry for each successful correction. No-op requests are rejected with HTTP 422. Conditional updates are intended to detect conflicting concurrent corrections.

Basic status corrections, no-op rejection, timestamp validation, invalid status handling, absent/leave punch-time validation, and history preservation have been tested. Concurrent correction behavior and additional timestamp edge cases remain to be verified.

