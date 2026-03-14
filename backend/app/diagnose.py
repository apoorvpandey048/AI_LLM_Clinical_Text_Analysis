"""Quick diagnosis script - run with: python -c "exec(open('app/diagnose.py').read())" """
import sys, json

# 1. Check DB schema
from app.db.session import engine
from sqlalchemy import text, inspect as sqlinspect

insp = sqlinspect(engine)
cols = [c['name'] for c in insp.get_columns('prompt_templates')]
print("SCHEMA prompt_templates columns:", cols)
print("has is_builtin:", "is_builtin" in cols)

# 2. Try building pipeline snapshot
try:
    from app.workers.tasks import _build_pipeline_snapshot
    snap = _build_pipeline_snapshot()
    print("SNAPSHOT OK - layers:", [l['layer_name'] for l in snap])
except Exception as e:
    print("SNAPSHOT FAILED:", type(e).__name__, str(e))

# 3. Check Redis queue depth
import redis, os
r = redis.from_url(os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0"))
qlen = r.llen("celery")
print("REDIS celery queue depth:", qlen)

# 4. Check Celery registered tasks
from app.workers.celery_app import celery_app
i = celery_app.control.inspect(timeout=3)
reg = i.registered()
print("REGISTERED tasks:", json.dumps(reg, indent=2))
