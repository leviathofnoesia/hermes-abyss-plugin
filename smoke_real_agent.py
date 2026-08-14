"""Real-agent smoke test for the Abyss resolver.

Dispatches a REAL `hermes chat -q -s abyss-doctor` process through the
production code path (_dispatch_resolution) against a TEMP COPY of the live
profile's abyss-data, so the real DB is never modified. The agent itself has
normal tool access (it is the user's own free-Nous agent) and is instructed
to investigate + fix; the point is to verify the whole spawn -> report ->
finalize machinery works with the real CLI.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REAL_HOME = Path(r"C:\Users\billy\AppData\Local\hermes")
REAL_PROFILE = REAL_HOME / "profiles" / "kraken"

tmp = Path(tempfile.mkdtemp(prefix="abyss-smoke-"))
print("SMOKE_TMP:", tmp)
(tmp / "abyss-data").mkdir(parents=True, exist_ok=True)
shutil.copy(REAL_PROFILE / "abyss-data" / "activity.db", tmp / "abyss-data" / "activity.db")
shutil.copy(REAL_PROFILE / "abyss-data" / "traces.db", tmp / "abyss-data" / "traces.db")

os.environ["HERMES_HOME"] = str(REAL_HOME)
os.environ["HERMES_PROFILE_HOME"] = str(tmp)
os.environ.pop("ABYSS_AGENT_CMD", None)  # ensure no stub override

sys.path.insert(0, str(REAL_HOME / "plugins" / "abyss"))
import __init__ as abyss  # noqa: E402

conn = abyss._get_activity_conn()
row = conn.execute(
    "SELECT id, signal_type, severity, label, description, session_id "
    "FROM signals WHERE resolved = 0 ORDER BY timestamp DESC LIMIT 1"
).fetchone()
conn.close()
if row is None:
    print("NO_OPEN_SIGNAL")
    sys.exit(2)
row = dict(row)
sig_id = row["id"]
print("DISPATCHING on signal", sig_id, row)

res = abyss._dispatch_resolution("signals", sig_id)
print("DISPATCH_RESULT:", res)
rid = res.get("report_id") or "unknown"

deadline = time.time() + 300
final = None
while time.time() < deadline:
    conn = abyss._get_activity_conn()
    r = conn.execute(
        "SELECT resolution_status, resolution_note, resolved, resolution_finished_at "
        "FROM signals WHERE id = ?",
        (sig_id,),
    ).fetchone()
    conn.close()
    if r and r[0] not in (None, "running"):
        final = tuple(r)
        break
    time.sleep(5)

print("FINAL_ROW:", final)
rep_path = tmp / "abyss-data" / "resolutions" / f"{rid}.json"
print("REPORT_PATH:", rep_path, "EXISTS:", rep_path.exists())
if rep_path.exists():
    print("REPORT:", rep_path.read_text(encoding="utf-8")[:2000])
log_path = rep_path.with_suffix(".log")
if log_path.exists():
    print("AGENT_LOG_TAIL:", log_path.read_text(encoding="utf-8", errors="replace")[-1500:])
print("SMOKE_DONE")
