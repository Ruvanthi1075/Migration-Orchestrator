import csv
import io

from fastapi import Header, Query
from fastapi.responses import Response

from console.state import EnvStore, env_store


def get_env(env: str = Query("prod", description="Environment: prod | staging | lab")) -> EnvStore:
    return env_store(env)


def get_actor(x_actor: str = Header("system@qsmo.local", description="Who is calling (no auth; used for audit trail)")) -> str:
    return x_actor


def csv_response(rows, columns, filename):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([r.get(c, "") if not isinstance(r.get(c), (list, dict)) else str(r.get(c)) for c in columns])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
