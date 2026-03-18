from celery import Celery
app = Celery(broker='redis://localhost:6379/0', backend='redis://localhost:6379/0')
app.conf.update(task_serializer='json', accept_content=['json'], result_serializer='json')
# Inspect what tasks workers have registered
i = app.control.inspect(timeout=3)
registered = i.registered()
reserved = i.reserved()
active = i.active()
print(f"Registered tasks: {registered}")
print(f"Reserved tasks: {reserved}")
print(f"Active tasks: {active}")
