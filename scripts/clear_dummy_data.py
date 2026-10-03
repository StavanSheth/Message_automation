"""Script to remove all dummy and mock data from app.db."""
import sqlite3
from pathlib import Path

def clear_dummy_data():
    db_path = Path("data/app.db")
    if not db_path.exists():
        print("Database data/app.db does not exist.")
        return

    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    tables_to_clear = [
        "contacts",
        "source_records",
        "tasks",
        "messages",
        "followups",
        "sync_runs",
        "events",
        "errors",
        "workers",
        "browser_sessions",
        "verification_results",
        "automation_runs",
        "reconciliations",
        "manual_reviews",
        "execution_identities",
        "rate_limit_cooldowns",
        "diagnostic_artifacts",
    ]

    for table in tables_to_clear:
        try:
            c.execute(f"DELETE FROM {table}")
        except sqlite3.OperationalError as e:
            print(f"Skipping {table}: {e}")

    try:
        c.execute("UPDATE system_controls SET value='STOPPED', updated_at=datetime('now') WHERE key='system_state'")
    except Exception as e:
        print(f"Error resetting system_controls: {e}")

    conn.commit()
    c.execute("VACUUM")
    conn.close()
    print("All dummy data successfully cleared from database!")

if __name__ == "__main__":
    clear_dummy_data()
