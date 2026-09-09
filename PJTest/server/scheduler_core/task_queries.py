"""Bound task summaries before counting examples (SQLite 3.7 compatible)."""

TASK_COLUMNS = (
    "task_id", "task_name", "template_name", "revision", "revision_policy",
    "resolved_zip_path", "split_mode", "result_json", "suite", "target_worker",
    "status", "priority", "work_root", "target_dir", "total_examples",
    "created_at", "started_at", "finished_at", "message",
)
COUNT_COLUMNS = ("real_total", "pending_count", "running_count", "success_count",
                 "failed_count", "done_count")


def list_tasks(conn, limit, status=None, suite=None, before_id=None):
    """Read one page and its counts from the same database snapshot."""
    conditions, params = [], []
    for name, value in (("status", status), ("suite", suite)):
        if value:
            conditions.append(name + " = ?")
            params.append(value)
    if before_id is not None:
        conditions.append("id < ?")
        params.append(int(before_id))
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    sql = ("SELECT id, " + ", ".join(TASK_COLUMNS) + " FROM tasks" + where
           + " ORDER BY id DESC LIMIT ?")
    conn.execute("BEGIN")
    try:
        rows = [dict(row) for row in conn.execute(sql, params + [int(limit)])]
        by_id = {}
        for row in rows:
            row.update(dict.fromkeys(COUNT_COLUMNS, 0))
            by_id[row["task_id"]] = row
        if rows:
            placeholders = ",".join("?" for _ in rows)
            counts = conn.execute(
                "SELECT task_id, status, COUNT(*) AS n FROM task_examples "
                "WHERE task_id IN (%s) GROUP BY task_id, status" % placeholders,
                [row["task_id"] for row in rows],
            )
            for item in counts:
                row, state, count = by_id[item["task_id"]], item["status"], item["n"]
                row["real_total"] += count
                direct = {"pending": "pending_count", "running": "running_count",
                          "success": "success_count"}.get(state)
                if direct:
                    row[direct] += count
                if state in ("failed", "timeout"):
                    row["failed_count"] += count
                if state in ("success", "failed", "timeout", "canceled"):
                    row["done_count"] += count
        return rows
    finally:
        conn.rollback()
