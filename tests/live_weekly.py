"""Real Ollama plan generation, auto-save and fresh inference on every click in a temp database."""
import sys
import tempfile
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import app
from mealapp import ai, plan_jobs, store, weekly

values=ai.config()
with tempfile.TemporaryDirectory() as folder,patch.object(store,'DATA',Path(folder)),patch.object(ai,'config',return_value=values):
    store.initialize()
    recipe=app.clean_recipe({'name':'Tofu bowl','vegan':True,'good_for_running':True,'servings':2,'measured_minutes':15,'ingredients':[{'name':'tofu','grams':200}]})
    store.put('recipes',recipe['id'],recipe)
    print(f"Testing real AI plan for {len(weekly.calendar()['dates'])} remaining days.",flush=True)
    job=plan_jobs.start({'prioritize_running':True})
    try:
        worker=plan_jobs.JOBS[job['id']]['thread'];worker.join(90)
        if worker.is_alive():
            plan_jobs.cancel(job['id']);worker.join(3)
            raise AssertionError('Live check exceeded 90 seconds; its task was cancelled.')
        result=plan_jobs.get(job['id'])
        assert result['status']=='completed',result.get('error')
        plan=result['plan']
        assert [d['date'] for d in plan['days']]==weekly.calendar()['dates']
        assert all(d['recipe_id']==recipe['id'] for d in plan['days'])
        assert len(store.all_items('plans'))==1
        next_job=plan_jobs.start({})
        assert next_job['id']!=job['id']
        second=plan_jobs.JOBS[next_job['id']]['thread'];second.join(90)
        if second.is_alive():
            plan_jobs.cancel(next_job['id']);second.join(3)
            raise AssertionError('The second fresh generation exceeded the live-check limit.')
        assert plan_jobs.get(next_job['id'])['status']=='completed'
        assert len(store.all_items('plans'))==1
        current_saved=store.get('plans',plan['week_start'])
        future=(date.fromisoformat(weekly.calendar()['week_start'])+timedelta(weeks=1)).isoformat()
        plan_jobs.add_week({'week_start':future})
        future_job=plan_jobs.start({'week_start':future})
        future_thread=plan_jobs.JOBS[future_job['id']]['thread'];future_thread.join(90)
        if future_thread.is_alive():
            plan_jobs.cancel(future_job['id']);future_thread.join(3)
            raise AssertionError('Future generation exceeded the live-check limit.')
        future_result=plan_jobs.get(future_job['id'])
        assert future_result['status']=='completed',future_result.get('error')
        assert len(future_result['plan']['days'])==7
        assert store.get('plans',plan['week_start'])==current_saved
        print(f"PASS: two fresh current-week requests with {len(plan['days'])} meals; seven future meals saved independently; temporary data only.",flush=True)
    finally:
        for record in plan_jobs.JOBS.values():
            record['cancel'].cancel();record['thread'].join(3)
