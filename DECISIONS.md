# DECISIONS.md

### 1. Indexes
I created a unique index on `employees.emp_code` to prevent duplicate employee codes. I also created a unique compound index on `attendance_logs.emp_code` and `date` to allow only one attendance record per employee per day. I would avoid a standalone index on a low-selectivity field such as status unless query performance measurements justified it.

### 2. Punch-in race
Two identical punch-in requests may both pass the initial existence check. The unique compound index allows only one insert. The other insert raises `DuplicateKeyError`, which the API catches and converts to HTTP 409 Conflict. The indexes and sequential duplicate behavior are verified; simultaneous requests still need testing.

### 3. Ties
The late-arrival leaderboard should use standard competition ranking. If employees tie at the limit, I would return all employees whose rank is less than or equal to the limit, rather than splitting a tie.

### 4. Headcount
The department summary should start from employees and use a left-side aggregation with attendance records. This keeps employees with no attendance logs in the headcount instead of excluding them.

### 5. Scaling
For 100 times more data, I would measure query plans and execution times, add indexes for demonstrated query patterns, and consider caching expensive analytics if necessary.

### 6. Punch-out calculations

Punch-out stores the closing timestamp and derives work hours, overtime, and half-day status. I normalize MongoDB datetimes before comparing them with timezone-aware timestamps. Work hours use half-up rounding, overtime requires at least 30 whole minutes, and half-day status depends on rounded work hours being below 4.50. Dedicated edge-case tests are still pending.


### 7. Attendance correction and audit history

Manual attendance corrections use PATCH `/attendance/{emp_code}/{date}`. The API preserves omitted fields, recalculates derived values, and appends a history entry for each successful correction. No-op requests are rejected with HTTP 422. Conditional updates are used to detect conflicting concurrent corrections. Basic status corrections and validation scenarios have been tested; concurrency and timestamp edge cases remain to be verified.
