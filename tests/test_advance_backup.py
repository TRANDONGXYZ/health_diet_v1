import copy
import io
import json
import tempfile
import unittest
import zipfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
import app
from mealapp import ai, backups, dish_photos, plan_jobs, store, weekly
from test_weekly import png


class AdvanceAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.root=Path(self.folder.name)
        self.patches=[patch.object(store,'DATA',self.root/'data'),patch.object(ai,'ROOT',self.root),
                      patch.object(backups,'config_path',return_value=self.root/'config.local.json'),
                      patch.object(weekly,'today',side_effect=lambda offset=None:self.day)]
        for item in self.patches:item.start()
        self.day=date(2026,11,11);store.initialize();plan_jobs.JOBS.clear()
        self.recipe=app.clean_recipe({'name':'Bún đậu','vegan':True,'servings':2,'ingredients':[{'name':'tofu','grams':200}]})
        store.put('recipes',self.recipe['id'],self.recipe)

    def tearDown(self):
        for job in plan_jobs.JOBS.values():
            job['cancel'].cancel()
            if job.get('thread'):job['thread'].join(2)
        plan_jobs.JOBS.clear()
        for item in reversed(self.patches):item.stop()
        self.folder.cleanup()

    def model(self,task,schema,**kwargs):
        count=schema['properties']['days']['minItems']
        return {'days':[{'recipe_id':'r1','timing':'morning','prep':'Weigh ingredients.','reason':'Saved dish.'} for _ in range(count)]}

    def test_add_week_has_seven_empty_dates_and_does_not_touch_current(self):
        current=plan_jobs.instant({'method':'rotation'})
        added=app.post('/api/plan/add',{'week_start':'2026-11-16'})['plan']
        self.assertEqual([d['date'] for d in added['days']],['2026-11-16','2026-11-17','2026-11-18','2026-11-19','2026-11-20','2026-11-21','2026-11-22'])
        self.assertTrue(all(d['recipe_id'] is None for d in added['days']))
        self.assertEqual(store.get('plans',current['week_start']),current)
        with self.assertRaisesRegex(ValueError,'already exists'):app.post('/api/plan/add',{'week_start':'2026-11-16'})
        self.assertEqual(store.get('plans',added['week_start']),added)

    def test_eight_future_weeks_excludes_current_and_ninth_is_rejected(self):
        plan_jobs.instant({'method':'rotation'})
        monday=date(2026,11,9)
        for i in range(1,9):app.post('/api/plan/add',{'week_start':(monday+timedelta(weeks=i)).isoformat()})
        self.assertEqual(len(store.all_items('plans')),9)
        for week in ('2026-11-09','2026-11-02','2027-01-11','2026-11-17'):
            with self.assertRaises(ValueError):app.post('/api/plan/add',{'week_start':week})
        self.assertEqual(len(store.all_items('plans')),9)

    def test_all_methods_generate_seven_future_days_and_only_five_current_days(self):
        app.post('/api/plan/add',{'week_start':'2026-11-16'})
        current=plan_jobs.instant({'method':'rotation'})
        for method in ('rotation','random','AI'):
            with patch.object(ai,'ask',side_effect=self.model):
                if method=='AI':
                    job=plan_jobs.start({'week_start':'2026-11-16'});plan_jobs.JOBS[job['id']]['thread'].join(2)
                    result=plan_jobs.get(job['id']);self.assertEqual(result['status'],'completed');plan=result['plan']
                else:plan=app.post('/api/plan/generate',{'method':method,'week_start':'2026-11-16'})
            self.assertEqual(len(plan['days']),7)
            self.assertEqual(plan['days'][0]['date'],'2026-11-16');self.assertEqual(plan['days'][-1]['date'],'2026-11-22')
            self.assertEqual(store.get('plans',current['week_start']),current)
        with patch.object(ai,'ask',side_effect=self.model):
            self.assertEqual(len(weekly.build({'method':'AI'})['days']),5)

    def test_empty_slot_accepts_saved_dish_and_keeps_other_slots_empty(self):
        before=app.post('/api/plan/add',{'week_start':'2026-11-16'})['plan']
        changed=app.post('/api/plan/replace',{'week_start':before['week_start'],'date':'2026-11-18','recipe_id':self.recipe['id']})['plan']
        self.assertEqual(changed['days'][2]['recipe']['name'],'Bún đậu')
        self.assertEqual(changed['days'][:2],before['days'][:2]);self.assertEqual(changed['days'][3:],before['days'][3:])

    def test_current_generation_preserves_earlier_meal_and_empty_slot_for_all_methods(self):
        self.day=date(2026,11,9)
        original=plan_jobs.instant({'method':'rotation'})
        original['days'][1]={'date':'2026-11-10','recipe_id':None}
        original['days'][0]['prep']='Keep this exact original advice.'
        store.put('plans',original['week_start'],original)
        earlier=copy.deepcopy(original['days'][:2])
        changed=dict(self.recipe,name='Changed recipe',ingredients=[{'name':'tofu','grams':100}])
        store.put('recipes',changed['id'],changed)
        self.day=date(2026,11,11)
        for method in ('rotation','random','AI'):
            with patch.object(ai,'ask',side_effect=self.model):
                if method=='AI':
                    job=plan_jobs.start({});plan_jobs.JOBS[job['id']]['thread'].join(2)
                    self.assertEqual(plan_jobs.get(job['id'])['status'],'completed')
                    result=plan_jobs.get(job['id'])['plan']
                else:result=plan_jobs.instant({'method':method})
            self.assertEqual(len(result['days']),7)
            self.assertEqual(result['days'][:2],earlier)
            self.assertEqual(result['days'][2]['date'],'2026-11-11')
            self.assertTrue(all(d['recipe']['name']=='Changed recipe' for d in result['days'][2:]))

    def test_collapse_choices_persist_per_week_and_roundtrip_in_backup(self):
        self.prepare_backup()
        choices={'2026-11-02':False,'2026-10-26':True,'2026-11-16':False,'2026-11-09':False}
        for week,expanded in choices.items():app.post('/api/plan/expansion',{'week_start':week,'expanded':expanded})
        raw=backups.export_zip()
        store.put('state','week_expansion',{'2026-11-16':True})
        backups.import_zip(raw)
        self.assertEqual(store.get('state','week_expansion'),choices)
        before=self.capture()
        invalid=self.repack(raw,modify=lambda data:next(row['body'] for row in data['tables']['state'] if row['id']=='week_expansion').update({'2026-11-16':'false'}))
        with self.assertRaisesRegex(ValueError,'expansion choice'):backups.import_zip(invalid)
        self.assertEqual(self.capture(),before)
        for data in ({'week_start':'2026-11-17','expanded':False},{'week_start':'2026-11-16','expanded':'false'}):
            with self.assertRaises(ValueError):app.post('/api/plan/expansion',data)

    def test_future_generation_requires_created_week_and_respects_horizon(self):
        for week in ('2026-11-02','2026-11-16','2027-01-11'):
            with self.assertRaises(ValueError):weekly.build({'week_start':week,'method':'rotation'})
        self.assertEqual(store.all_items('plans'),[])

    def prepare_backup(self):
        import base64
        image=dish_photos.save({'filename':'dish.png','image':base64.b64encode(png()).decode()})
        self.recipe['photo_url']=image['photo_url'];store.put('recipes',self.recipe['id'],self.recipe)
        self.day=date(2026,11,2);plan_jobs.instant({'method':'rotation'})
        self.day=date(2026,11,11);plan_jobs.instant({'method':'rotation'})
        app.post('/api/plan/add',{'week_start':'2026-11-16'})
        app.post('/api/plan/replace',{'week_start':'2026-11-16','date':'2026-11-16','recipe_id':self.recipe['id']})
        store.put('state','settings',{'text_model':'qwen2.5:1.5b','vision_model':'qwen3.5:0.8b','english_model':'llama3.2:1b','vietnamese_model':'qwen2.5:1.5b','usda_api_key':'backup-private-key'})
        store.put('cache','speech:test',{'text':'Nguồn tiếng Việt được lưu.'})
        backups.config_path().write_text(json.dumps({'context_size':4096,'speech_threads':2,'custom_option':'preserve-me'}),encoding='utf-8')
        return backups.export_zip()

    def capture(self):
        with store.db() as connection:
            tables={table:sorted((row[0],row[1]) for row in connection.execute(f'SELECT id,body FROM {table}')) for table in backups.TABLES}
        folder=store.DATA/'photos'
        return tables,{p.name:p.read_bytes() for p in folder.iterdir()} if folder.exists() else {},backups.config_path().read_bytes() if backups.config_path().exists() else None

    def repack(self,raw,modify=None,missing=None,extra=None):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:files={name:archive.read(name) for name in archive.namelist()}
        if modify:
            data=json.loads(files[backups.MANIFEST]);modify(data);files[backups.MANIFEST]=json.dumps(data,ensure_ascii=False).encode()
        if missing:files.pop(missing)
        if extra:files.update(extra)
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
            for name,body in files.items():archive.writestr(name,body)
        return output.getvalue()

    def test_backup_roundtrip_restores_all_tables_images_and_effective_settings(self):
        raw=self.prepare_backup();data,images=backups.read_zip(raw)
        self.assertEqual(data['version'],1);self.assertEqual(data['settings']['context_size'],4096)
        self.assertEqual(data['settings']['usda_api_key'],'backup-private-key')
        self.assertEqual(len(data['tables']['plans']),3);self.assertTrue(images)
        expected=copy.deepcopy(data['tables'])
        with store.db() as connection:
            for table in backups.TABLES:connection.execute(f'DELETE FROM {table}')
        for path in (store.DATA/'photos').iterdir():path.unlink()
        backups.config_path().write_text('{"context_size":8192}',encoding='utf-8')
        result=backups.import_zip(raw)
        self.assertEqual(result['weeks'],3);self.assertEqual(result['images'],1)
        self.assertEqual(ai.config()['context_size'],4096);self.assertEqual(ai.config()['custom_option'],'preserve-me')
        self.assertEqual(ai.config()['usda_api_key'],'backup-private-key')
        for name,body in images.items():self.assertEqual((store.DATA/'photos'/name).read_bytes(),body)
        with store.db() as connection:
            for table,rows in expected.items():
                actual={row[0]:json.loads(row[1]) for row in connection.execute(f'SELECT id,body FROM {table}')}
                self.assertEqual(actual,{row['id']:row['body'] for row in rows})
        self.assertEqual(store.get('plans','2026-11-16')['days'][0]['recipe']['photo_url'],self.recipe['photo_url'])
        self.assertIsNone(store.get('plans','2026-11-16')['days'][1]['recipe_id'])

    def test_invalid_or_incomplete_archive_never_changes_live_data(self):
        raw=self.prepare_backup();_,images=backups.read_zip(raw);name=next(iter(images));before=self.capture();epoch=store.EPOCH
        variants=[b'not a zip',raw[:-20],self.repack(raw,missing=backups.MANIFEST),
                  self.repack(raw,missing='images/'+name),self.repack(raw,extra={'../outside.txt':b'bad'}),
                  self.repack(raw,extra={'images/'+name:b'bad image'}),
                  self.repack(raw,modify=lambda d:d.update(version=2)),
                  self.repack(raw,modify=lambda d:d['tables'].pop('foods')),
                  self.repack(raw,modify=lambda d:d['tables']['cache'].clear()),
                  self.repack(raw,modify=lambda d:d.update(tables=list(backups.TABLES))),
                  self.repack(raw,modify=lambda d:d['tables']['recipes'][0]['body'].update(id='wrong')),
                  self.repack(raw,modify=lambda d:d['tables']['recipes'][0]['body'].update(ingredients='bad')),
                  self.repack(raw,modify=lambda d:d['tables']['foods'][0]['body'].update(aliases=None)),
                  self.repack(raw,modify=lambda d:d['settings'].update(text_model=None)),
                  self.repack(raw,modify=lambda d:d['settings'].pop('ollama_url'))]
        for invalid in variants:
            with self.subTest(size=len(invalid)),self.assertRaises(ValueError):backups.import_zip(invalid)
            self.assertEqual(self.capture(),before);self.assertEqual(store.EPOCH,epoch)
        self.assertFalse((self.root/'outside.txt').exists())

    def test_missing_image_reference_is_rejected_even_if_manifest_list_is_changed(self):
        raw=self.prepare_backup();_,images=backups.read_zip(raw);name=next(iter(images));before=self.capture()
        invalid=self.repack(raw,modify=lambda d:d.update(images=[]),missing='images/'+name)
        with self.assertRaisesRegex(ValueError,'missing backup image'):backups.import_zip(invalid)
        self.assertEqual(self.capture(),before)

    def test_apply_failure_rolls_back_database_images_and_config(self):
        raw=self.prepare_backup();store.put('state','extra-live',{'keep':'this'})
        backups.config_path().write_text('{"speech_threads":3}',encoding='utf-8')
        with store.db() as connection:
            connection.execute("CREATE TRIGGER fail_restore BEFORE INSERT ON recipes BEGIN SELECT RAISE(FAIL,'simulated failure'); END")
        before=self.capture();epoch=store.EPOCH
        with self.assertRaisesRegex(ValueError,'Existing data was kept'):backups.import_zip(raw)
        self.assertEqual(self.capture(),before);self.assertEqual(store.EPOCH,epoch)

    def test_restore_rejects_active_ai_and_stale_requests_cannot_write_after_restore(self):
        raw=self.prepare_backup();before=self.capture()
        plan_jobs.JOBS['active']={'id':'active','status':'running','week_start':'2026-11-09','cancel':ai.Cancellation()}
        with self.assertRaisesRegex(ValueError,'Cancel meal generation'):backups.import_zip(raw)
        self.assertEqual(self.capture(),before);plan_jobs.JOBS.clear()
        with store.request_scope():
            backups.import_zip(raw)
            with self.assertRaisesRegex(ValueError,'Data was restored'):store.put('state','stale','must not persist')
        self.assertIsNone(store.get('state','stale'))

    def test_duplicate_zip_members_and_duplicate_json_keys_are_rejected(self):
        raw=self.prepare_backup();before=self.capture()
        invalid=self.repack(raw,extra={backups.MANIFEST:b'{"version":1,"version":1}'})
        with self.assertRaises(ValueError):backups.import_zip(invalid)
        output=io.BytesIO()
        import warnings
        with zipfile.ZipFile(output,'w') as archive,warnings.catch_warnings():
            warnings.simplefilter('ignore',UserWarning)
            archive.writestr(backups.MANIFEST,b'{}');archive.writestr(backups.MANIFEST,b'{}')
        with self.assertRaises(ValueError):backups.import_zip(output.getvalue())
        self.assertEqual(self.capture(),before)
