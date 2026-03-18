from celery import Celery
import time
app = Celery(broker='redis://localhost:6379/0', backend='redis://localhost:6379/0')
app.conf.update(task_serializer='json', accept_content=['json'], result_serializer='json')
i = app.control.inspect(timeout=5)
active = i.active()
registered = i.registered()
print(f"Workers: {list(registered.keys()) if registered else 'NONE - still restarting'}")
print(f"Active tasks: {active}")
