import base64
import copy
import json
import struct
import tempfile
import threading
import time
import unittest
import zlib
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch
import app
from mealapp import ai, dish_photos, nutrition, plan_jobs, store, weekly


def png(width=500,height=500):
    def chunk(kind,body):
        return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))+chunk(b'IDAT',zlib.compress((b'\x00'+b'\x10\x80\x20\xff'*width)*height))+chunk(b'IEND',b'')


class WeeklyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.data=patch.object(store,'DATA',Path(self.folder.name));self.data.start();store.initialize()
        self.config_patch=patch.object(ai,'ROOT',Path(self.folder.name));self.config_patch.start()
        self.day=date(2026,11,12)
        self.clock=patch.object(weekly,'today',side_effect=lambda offset=None:self.day);self.clock.start()
        self.recipe=app.clean_recipe({'name':'Tofu','servings':2,'ingredients':[{'name':'tofu','grams':200}]})
        store.put('recipes',self.recipe['id'],self.recipe)
        plan_jobs.JOBS.clear()

    def tearDown(self):
        for job in list(plan_jobs.JOBS.values()):
            job['cancel'].cancel();job['thread'].join(2)
        plan_jobs.JOBS.clear();self.clock.stop();self.config_patch.stop();self.data.stop();self.folder.cleanup()

    def model(self,task,schema,**kwargs):
        days=schema['properties']['days']
        self.assertEqual(days['minItems'],days['maxItems'])
        return {'days':[{'recipe_id':'r1','timing':'morning','prep':'Weigh ingredients.','reason':'A saved dish.'} for _ in range(days['minItems'])]}

    def finish(self,job):
        if job.get('id'):
            plan_jobs.JOBS[job['id']]['thread'].join(3)
            return plan_jobs.get(job['id'])
        return job

    def test_all_generation_methods_cover_today_through_sunday(self):
        for current,count in [(date(2026,11,9),7),(date(2026,11,11),5),(date(2026,11,12),4),(date(2026,11,14),2),(date(2026,11,15),1)]:
            self.day=current
            for method in ('rotation','random','AI'):
                with self.subTest(day=current,method=method),patch.object(ai,'ask',side_effect=self.model):
                    plan=weekly.build({'method':method})
                self.assertEqual(len(plan['days']),count)
                self.assertEqual(plan['week_start'],'2026-11-09')
                self.assertEqual(plan['days'][0]['date'],current.isoformat())
                self.assertEqual(plan['days'][-1]['date'],'2026-11-15')
                self.assertEqual({d['recipe_id'] for d in plan['days']},{self.recipe['id']})

    def test_user_timezone_controls_date_at_utc_week_boundary(self):
        self.clock.stop()
        with patch.object(weekly,'datetime') as clock:
            clock.now.return_value=datetime(2026,11,8,18,30,tzinfo=timezone.utc)
            self.assertEqual(weekly.calendar(-420)['today'],'2026-11-09')
            self.assertEqual(len(weekly.calendar(-420)['dates']),7)
            self.assertEqual(weekly.calendar(0)['today'],'2026-11-08')
            self.assertEqual(len(weekly.calendar(0)['dates']),1)
        self.clock.start()

    def test_rotation_and_random_save_without_ai_or_popup_confirmation(self):
        with patch.object(ai,'ask',side_effect=AssertionError('No AI')):
            first=app.post('/api/plan/generate',{'method':'rotation'})
            second=app.post('/api/plan/generate',{'method':'random'})
        self.assertEqual(len(store.all_items('plans')),1)
        self.assertEqual(store.get('plans','2026-11-09'),second)
        self.assertEqual(first['method'],'rotation');self.assertEqual(second['method'],'random')

    def test_random_uses_random_selection_from_saved_dishes(self):
        other=app.clean_recipe(dict(self.recipe,id='other',name='Other tofu'))
        store.put('recipes',other['id'],other)
        with patch.object(weekly.random,'sample',side_effect=lambda items,count:list(reversed(items))) as sample:
            result=weekly.build({'method':'random'})
        sample.assert_called_once()
        self.assertEqual([d['recipe_id'] for d in result['days']],[other['id'],self.recipe['id'],other['id'],self.recipe['id']])

    def test_every_ai_click_runs_inference_and_replaces_only_current_week(self):
        with patch.object(ai,'ask',side_effect=self.model) as model:
            first=self.finish(plan_jobs.start({}))
            self.assertEqual(first['status'],'completed')
            second=self.finish(plan_jobs.start({}))
            self.assertNotEqual(first['id'],second['id']);self.assertEqual(model.call_count,2)
            third=self.finish(plan_jobs.start({'regenerate':False}))
            self.assertEqual(third['status'],'completed');self.assertEqual(model.call_count,3)
        self.assertEqual(len(store.all_items('plans')),1)
        self.assertEqual(store.get('plans','2026-11-09')['method'],'AI')

    def test_new_week_creates_new_plan_and_keeps_history(self):
        with patch.object(ai,'ask',side_effect=self.model):
            first=self.finish(plan_jobs.start({}))['plan']
            self.day=date(2026,11,19)
            second=self.finish(plan_jobs.start({}))['plan']
        self.assertEqual([p['week_start'] for p in weekly.history()],['2026-11-16','2026-11-09'])
        self.assertEqual(store.get('plans','2026-11-09'),first)
        self.assertEqual(second['days'][0]['date'],'2026-11-19')

    def test_past_and_future_weeks_cannot_be_generated_or_saved(self):
        for week in ('2026-11-02','2026-11-16'):
            for endpoint in ('/api/plan/start','/api/plan/generate'):
                with self.assertRaisesRegex(ValueError,'current week|Past weeks|Add this future week'):
                    app.post(endpoint,{'week_start':week,'method':'rotation'})
        with self.assertRaisesRegex(ValueError,'save automatically'):
            app.post('/api/plan/save',{'week_start':'2026-11-02'})

    def test_history_snapshot_survives_recipe_edits_and_calorie_override(self):
        first=plan_jobs.instant({'method':'rotation'})
        saved=copy.deepcopy(first)
        changed=dict(self.recipe,name='Changed',ingredients=[{'name':'tofu','grams':50}])
        store.put('recipes',changed['id'],changed)
        food=store.find_food('tofu');store.put('foods',food['id'],dict(food,kcal=100))
        self.day=date(2026,11,19)
        archived=weekly.history()[0]
        self.assertEqual(archived,saved)
        self.assertEqual(archived['days'][0]['recipe']['calories']['per_serving'],144)
        self.assertEqual(archived['days'][0]['recipe']['name'],'Tofu')

    def test_old_singleton_migrates_once_with_dates_and_snapshots(self):
        old={'week_start':'2026-11-02','method':'AI','days':[{'recipe_id':self.recipe['id'],'timing':'morning'} for _ in range(7)]}
        store.put('state','plan',old)
        migrated=weekly.history()[0]
        self.assertEqual(len(migrated['days']),7);self.assertEqual(migrated['days'][-1]['date'],'2026-11-08')
        self.assertEqual(migrated['days'][0]['recipe']['name'],'Tofu')
        store.put('state','plan',dict(old,week_start='2026-10-26'))
        self.assertEqual(len(weekly.history()),1)

    def test_cancel_keeps_saved_plan_and_blocks_late_model_result(self):
        before=plan_jobs.instant({'method':'rotation'});started=threading.Event();release=threading.Event()
        def paused(*args,**kwargs):
            started.set();release.wait(2)
            return self.model(*args,**kwargs)
        with patch.object(ai,'ask',side_effect=paused):
            job=plan_jobs.start({'regenerate':True});self.assertTrue(started.wait(1))
            self.assertEqual(plan_jobs.cancel(job['id'])['status'],'cancelling')
            release.set();result=self.finish(job)
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(store.get('plans','2026-11-09'),before)

    def test_failed_regeneration_does_not_replace_saved_plan(self):
        before=plan_jobs.instant({'method':'rotation'})
        with patch.object(ai,'ask',side_effect=ValueError('Model failed')):
            result=self.finish(plan_jobs.start({'regenerate':True}))
        self.assertEqual(result['status'],'failed')
        self.assertEqual(store.get('plans','2026-11-09'),before)

    def test_week_rollover_during_generation_does_not_modify_past_week(self):
        self.day=date(2026,11,15);before=plan_jobs.instant({'method':'rotation'})
        def rollover(*args,**kwargs):
            result=self.model(*args,**kwargs);self.day=date(2026,11,16);return result
        with patch.object(ai,'ask',side_effect=rollover):
            result=self.finish(plan_jobs.start({'regenerate':True}))
        self.assertEqual(result['status'],'failed');self.assertIn('week changed',result['error'])
        self.assertEqual(store.get('plans','2026-11-09'),before)

    def test_photos_validate_dimensions_and_formats_and_survive_in_history(self):
        image=base64.b64encode(png()).decode()
        for extension in ('jpg','jpeg','png','JPG'):
            photo=dish_photos.save({'filename':'dish.'+extension,'image':image})
        path=dish_photos.path_for(photo['photo_url']);self.assertEqual(path.read_bytes(),png())
        self.assertEqual(len(list(path.parent.iterdir())),1,'Identical photos deduplicate')
        recipe=app.clean_recipe(dict(self.recipe,photo_url=photo['photo_url']));store.put('recipes',recipe['id'],recipe)
        plan=plan_jobs.instant({'method':'rotation'});store.remove_recipe(recipe['id'])
        self.assertEqual(plan['days'][0]['recipe']['photo_url'],photo['photo_url']);self.assertTrue(path.is_file())
        for name,raw in [('dish.gif',png()),('dish.webp',png()),('dish.png',png(499,500)),('dish.jpg',b'not an image'),('dish.png',png()[:-12])]:
            with self.subTest(name=name),self.assertRaises(ValueError):
                dish_photos.save({'filename':name,'image':base64.b64encode(raw).decode()})
        for url in ('https://example.com/photo.png','/media/../../app.py','/media/fake.png'):
            with self.assertRaises(ValueError):app.clean_recipe(dict(self.recipe,photo_url=url))

    def test_delete_week_keeps_recipes_photos_and_other_weeks(self):
        photo=dish_photos.save({'filename':'dish.png','image':base64.b64encode(png()).decode()})
        self.recipe['photo_url']=photo['photo_url'];store.put('recipes',self.recipe['id'],self.recipe)
        old=plan_jobs.instant({'method':'rotation'})
        self.day=date(2026,11,19);current=plan_jobs.instant({'method':'rotation'})
        recipes=store.all_items('recipes')
        app.post('/api/plan/delete',{'week_start':old['week_start']})
        self.assertEqual(store.all_items('recipes'),recipes)
        self.assertTrue(dish_photos.path_for(photo['photo_url']).is_file())
        self.assertEqual(weekly.history(),[current])
        app.post('/api/plan/delete',{'week_start':current['week_start']})
        self.assertEqual(weekly.history(),[])
        with self.assertRaises(ValueError):app.post('/api/plan/delete',{'week_start':current['week_start']})

    def test_replace_past_meal_changes_only_that_date_and_uses_new_snapshot(self):
        old=plan_jobs.instant({'method':'rotation'});untouched=copy.deepcopy(old)
        other=app.clean_recipe(dict(self.recipe,id='new-dish',name='New dish',measured_minutes=45,ingredients=[{'name':'tofu','grams':100}]))
        store.put('recipes',other['id'],other)
        self.day=date(2026,11,19);current=plan_jobs.instant({'method':'rotation'})
        selected=old['days'][1]['date']
        with patch.object(ai,'ask',side_effect=AssertionError('No model for replacements')),patch.object(nutrition,'resolve',side_effect=AssertionError('No lookup for saved dishes')):
            saved=app.post('/api/plan/replace',{'week_start':old['week_start'],'date':selected,'recipe_id':other['id']})['plan']
        self.assertEqual(saved['days'][0],untouched['days'][0]);self.assertEqual(saved['days'][2:],untouched['days'][2:])
        self.assertEqual(saved['days'][1]['date'],selected)
        self.assertEqual(saved['days'][1]['recipe_id'],other['id'])
        self.assertEqual(saved['days'][1]['recipe']['calories']['per_serving'],72)
        self.assertEqual(saved['days'][1]['timing'],'night before')
        self.assertIn('Manually selected',saved['days'][1]['reason'])
        self.assertEqual(store.get('plans',current['week_start']),current)
        store.remove_recipe(other['id'])
        self.assertEqual(store.get('plans',old['week_start'])['days'][1],{'date':selected,'recipe_id':other['id'],'deleted':True})

    def test_current_replacement_lists_are_not_restricted_by_generation_filters(self):
        self.recipe['vegan']=True;store.put('recipes',self.recipe['id'],self.recipe)
        before=plan_jobs.instant({'method':'rotation','vegan_only':True})
        other=app.clean_recipe(dict(self.recipe,id='non-vegan',vegan=False));store.put('recipes',other['id'],other)
        saved=app.post('/api/plan/replace',{'week_start':before['week_start'],'date':before['days'][0]['date'],'recipe_id':other['id']})['plan']
        self.assertFalse(saved['vegan_only'])
        self.assertEqual(saved['days'][0]['recipe_id'],other['id'])

    def test_invalid_replacement_never_modifies_saved_week(self):
        before=plan_jobs.instant({'method':'rotation'})
        for data in ({'week_start':before['week_start'],'date':'2026-11-11','recipe_id':self.recipe['id']},
                     {'week_start':before['week_start'],'date':before['days'][0]['date'],'recipe_id':'deleted'},
                     {'week_start':'2026-11-10','date':before['days'][0]['date'],'recipe_id':self.recipe['id']}):
            with self.assertRaises(ValueError):app.post('/api/plan/replace',data)
            self.assertEqual(store.get('plans',before['week_start']),before)

    def test_current_week_mutations_cannot_race_an_active_generation(self):
        before=plan_jobs.instant({'method':'rotation'});started=threading.Event();release=threading.Event()
        def paused(*args,**kwargs):
            started.set();release.wait(2);return self.model(*args,**kwargs)
        with patch.object(ai,'ask',side_effect=paused):
            job=plan_jobs.start({});self.assertTrue(started.wait(1))
            for endpoint in ('/api/plan/delete','/api/plan/replace'):
                with self.assertRaisesRegex(ValueError,'Cancel AI generation'):
                    app.post(endpoint,{'week_start':before['week_start'],'date':before['days'][0]['date'],'recipe_id':self.recipe['id']})
            plan_jobs.cancel(job['id']);release.set();self.finish(job)
        app.post('/api/plan/delete',{'week_start':before['week_start']})
        self.assertEqual(weekly.history(),[])
