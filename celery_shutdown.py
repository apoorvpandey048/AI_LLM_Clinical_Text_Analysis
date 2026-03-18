from celery import Celery
app = Celery(broker='redis://localhost:6379/0', backend='redis://localhost:6379/0')
app.conf.update(task_serializer='json', accept_content=['json'], result_serializer='json')
# Broadcast shutdown to all workers
print("Sending shutdown broadcast...")
app.control.broadcast('shutdown')
print("Shutdown broadcast sent!")
# Also try shutdown specifically to this worker
app.control.broadcast('shutdown', destination=['celery@918a712bdc5b'])
print("Done")
