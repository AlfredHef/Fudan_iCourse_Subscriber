#!/usr/bin/env python3
"""Export every saved lecture summary as a stable Markdown record.

The export is intentionally independent of ``emailed_at``: email delivery and
server backup can fail and retry separately. Re-exporting the whole database
with rsync is idempotent because each lecture keeps the same filename.
"""

import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path


def export_records(db_path: str, output_dir: str) -> int:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute("""
            SELECT l.sub_id, l.course_id, c.title AS course_title,
                   l.sub_title, l.date, l.summary
            FROM lectures AS l
            JOIN courses AS c ON c.course_id = l.course_id
            WHERE l.processed_at IS NOT NULL AND l.summary IS NOT NULL
            ORDER BY l.date, l.sub_id
        """)
        count = 0
        for row in rows:
            sub_id = str(row["sub_id"])
            safe_id = sub_id if re.fullmatch(r"[A-Za-z0-9_-]{1,100}", sub_id) else hashlib.sha256(sub_id.encode()).hexdigest()
            path = destination / f"{safe_id}.md"
            record = (
                f"# {row['course_title'] or row['course_id']}\n\n"
                f"课次：{row['sub_title'] or sub_id}\n\n"
                f"日期：{row['date'] or '未知'}\n\n"
                f"课程 ID：{row['course_id']}\n\n"
                f"课次 ID：{sub_id}\n\n"
                "---\n\n"
                f"{row['summary'].rstrip()}\n"
            )
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(record)
            count += 1
        return count
    finally:
        connection.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: export_email_records.py DATABASE OUTPUT_DIRECTORY")
    print(f"Exported {export_records(sys.argv[1], sys.argv[2])} lecture summaries")
