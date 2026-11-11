"""Cancellable plan jobs. Completion and cancellation serialize before any save."""
import threading
import uuid
import contextvars
from . import ai, weekly, store, hosted

LOCK = threading.RLock()
JOBS = {}


def view(job):
    return {key:value for key,value in job.items() if key not in {"cancel","thread","owner"}}


def owned(job):
    return job.get('owner') == hosted.owner()


def clear_current():
    for key in list(JOBS):
        if owned(JOBS[key]):del JOBS[key]


def active():
    with LOCK:
        return next((view(job) for job in JOBS.values() if owned(job) and job["status"] in {"running","cancelling"}),None)


def get(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or not owned(job):
            raise ValueError("Generation task not found. Reload to see the saved plan.")
        return view(job)


def start(data):
    weekly.history()
    context = weekly.generation_context(data)
    with LOCK:
        store.check_epoch()
        if active():
            raise ValueError("A meal plan is already generating. Cancel it or wait for it to finish.")
        token = ai.Cancellation()
        job = {"id":str(uuid.uuid4()),"status":"running","week_start":context["week_start"],"cancel":token,"owner":hosted.owner()}
        # Bound memory while leaving enough terminal tasks for reconnecting tabs.
        for key in [key for key in JOBS if owned(JOBS[key])][:-(2 if hosted.enabled() else 19)]:
            if JOBS[key]["status"] not in {"running","cancelling"}:
                del JOBS[key]
        JOBS[job["id"]] = job
        def run():
            try:
                with ai.cancellation_scope(token):
                    result = weekly.build(dict(data,method="AI",week_start=context["week_start"]))
                    with hosted.write_scope(), LOCK:
                        token.check()
                        result = weekly.commit(result,data.get("timezone_offset"))
                        hosted.persist()
                        job.update(status="completed",plan=result)
            except ai.AICancelled:
                with LOCK:
                    job.update(status="cancelled")
            except Exception as exc:
                with LOCK:
                    job.update(status="cancelled" if token.event.is_set() else "failed",error=str(exc))
        copied = contextvars.copy_context()
        job["thread"] = threading.Thread(target=lambda:copied.run(run),daemon=True,name="weekly-plan")
        job["thread"].start()
        return view(job)


def cancel(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job or not owned(job):
            raise ValueError("Generation task not found. Reload to see the saved plan.")
        if job["status"] in {"running","cancelling"}:
            job["status"] = "cancelling"
            job["cancel"].cancel()
        return view(job)


def instant(data):
    weekly.history()
    with LOCK:
        if active():
            raise ValueError("Cancel the current AI generation before generating another plan.")
        plan = weekly.build(data)
        return weekly.commit(plan,data.get("timezone_offset"))


def edit_week(data, delete=False):
    weekly.history()
    with LOCK:
        running = active()
        if running and running['week_start'] == data.get('week_start'):
            raise ValueError('Cancel AI generation for this week before changing its plan.')
        if delete:
            return weekly.delete(data.get('week_start'))
        return weekly.replace(data.get('week_start'),data.get('date'),data.get('recipe_id'))


def add_week(data):
    weekly.history()
    with LOCK:
        store.check_epoch()
        return weekly.add_week(data)
