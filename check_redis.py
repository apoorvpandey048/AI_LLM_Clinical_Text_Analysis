import redis, json, sys

r = redis.from_url('redis://localhost:6379/0', decode_responses=True)

q_len = r.llen('celery')
tasks_raw = r.lrange('celery', 0, 4)
active_keys = r.keys('celery-task-meta-*')
unacked = r.keys('unacked*')

print(f'Queue length (celery): {q_len}')
print(f'Task meta count: {len(active_keys)}')
print(f'Unacked keys: {len(unacked)} -> {unacked[:5]}')

for i, t in enumerate(tasks_raw):
    try:
        parsed = json.loads(t)
        task_name = parsed.get('headers', {}).get('task', '?')
        body_raw = parsed.get('body', '{}')
        if isinstance(body_raw, str):
            body_decoded = json.loads(body_raw)
        else:
            body_decoded = body_raw
        # Celery body is [args, kwargs, options]
        if isinstance(body_decoded, list) and len(body_decoded) >= 1:
            args = body_decoded[0]
            job_id = args[0] if args else '?'
        else:
            job_id = '?'
        print(f'  Task[{i}]: {task_name} job_id={job_id}')
    except Exception as e:
        print(f'  Task[{i}] parse error: {e} raw={str(t)[:100]}')

r.close()
