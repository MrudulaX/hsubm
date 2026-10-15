# DECISIONS.md

1. I made emp_code unique for 409 on duplicate staff, and emp_code plus date unique for one record per day. Date with emp_code covers attendance sort, emp_code with punch_in covers punch-out lookup. Department with emp_code covers list by department, department with joined_on covers headcount. I skipped status for leaderboard because date already filters.

2. Both cut millis, get IST date with overnight check, then insert the same emp_code and date. One wins with 201, the other gets duplicate key and I return 409. I do not check first like the starter did.

3. I sum late per person, rank by total, keep rank under limit, and sort by total then emp_code. So 100, 90, 90 with limit 2 gives 1, 2, 2 and returns 3 rows.

4. I take staff joined before month end, join logs by emp_code, and keep no-log staff with preserve. I check in-month inside the group. Headcount counts emp_code, so zero-log still counts but adds zero.

5. I would push the month filter inside the lookup so I only pull that month, and keep a monthly total per person updated on each punch, sharded by emp_code. Then summary reads one row per person, not all logs.
