import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import app
from mealapp import ai, backups, offline_sync as sync, store, weekly
from test_weekly import png


class OfflineSyncTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.patches=[patch.object(store,'DATA',Path(self.folder.name)),patch.object(ai,'ROOT',Path(self.folder.name)),patch.object(backups,'config_path',lambda:Path(self.folder.name)/'config.local.json')]
        for p in self.patches:p.start()
        store.initialize();weekly.history();self.initial=sync.pull(0)

    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.folder.cleanup()

    def change(self,key,value,base=0,since=None):
        op={'id':str(uuid.uuid4()),'changes':[{'key':key,'value':value,'base':base}]}
        if since is not None:op['since']=since
        return op

    def recipe(self,name='Offline recipe'):
        return app.clean_recipe({'name':name,'id':str(uuid.uuid4()),'ingredients':[{'name':'unknown offline ingredient','grams':100}]})

    def test_household_setting_and_planned_quantities_roundtrip(self):
        sync.push(self.change('settings',{'household_servings':3}))
        recipe=self.recipe();recipe['servings']=5
        sync.push(self.change('recipes/'+recipe['id'],recipe))
        portion={'servings':3,'recipe_servings':5,'ingredients':[{'name':'rice','grams':300,'quantity':'300 g'},{'name':'salt','grams':None,'quantity':'to taste'}],
                 'calories':{'total':0,'per_serving':None,'complete':False,'missing':['rice','salt']}}
        week='2026-11-16';slot={'date':week,'recipe_id':recipe['id'],'serving_plan':portion}
        sync.push({'id':str(uuid.uuid4()),'changes':[{'key':'plans/'+week,'base':0,'value':{'week_start':week}},{'key':'meals/'+week+'/'+week,'base':0,'value':slot}]})
        saved=store.get('plans',week)['days'][0]
        self.assertEqual(saved['serving_plan'],portion)
        self.assertEqual(saved['recipe']['servings'],5)
        setting=next(row for row in sync.pull(0)['changes'] if row['key']=='settings')
        sync.push(self.change('settings',{'household_servings':4},setting['version']))
        self.assertEqual(store.get('plans',week)['days'][0]['serving_plan'],portion)
        data,_=backups.read_zip(backups.export_zip())
        self.assertEqual(next(r['body'] for r in data['tables']['plans'] if r['id']==week)['days'][0]['serving_plan'],portion)

    def test_household_and_planned_serving_validation(self):
        for bad in (0,-1,1.5,True,'3'):
            with self.subTest(bad=bad),self.assertRaises(ValueError):backups.validate_settings({'household_servings':bad})
        for good in (None,1,3):backups.validate_settings({'household_servings':good})
        with patch('mealapp.hosted.enabled',return_value=True):
            sync.validate_operation(self.change('settings',{'household_servings':3}))
            sync.validate_operation(self.change('settings',{'household_servings':3,'vision_model':'retired-model'}))
            with self.assertRaises(ValueError):sync.validate_operation(self.change('settings',{'household_servings':0}))

    def test_manual_save_never_looks_up_nutrition(self):
        r=self.recipe()
        with patch('mealapp.nutrition.resolve',side_effect=AssertionError('No network on manual save')):
            result=sync.push(self.change('recipes/'+r['id'],r,since=self.initial['cursor']))
        self.assertEqual(len(result['changes']),1)
        self.assertEqual(store.get('recipes',r['id'])['name'],r['name'])
        self.assertFalse(store.calories(r)['complete'])

    def test_receipt_and_conflict_survive_fresh_calls(self):
        r=self.recipe();op=self.change('recipes/'+r['id'],r)
        first=sync.push(op);self.assertEqual(sync.push(op),first)
        changed=copy.deepcopy(op);changed['changes'][0]['value']['name']='Wrong retry'
        with self.assertRaises(ValueError):sync.push(changed)
        self.assertTrue(sync.push(self.change('recipes/'+r['id'],dict(r,name='Stale edit')))['conflict'])
        self.assertEqual(store.get('recipes',r['id'])['name'],r['name'])

    def test_recipe_deletion_tombstones_every_date_atomically(self):
        r=self.recipe();created=sync.push(self.change('recipes/'+r['id'],r))
        for week in ('2026-11-02','2026-11-09','2026-11-16'):
            store.put('plans',week,{'week_start':week,'days':[{'date':week,'recipe_id':r['id'],'recipe':r}]})
        before=sync.pull(0)
        result=sync.push(self.change('recipes/'+r['id'],None,created['cursor'],before['cursor']))
        self.assertEqual(len(result['changes']),4)
        for p in store.all_items('plans'):self.assertEqual(p['days'][0],{'date':p['week_start'],'recipe_id':r['id'],'deleted':True})
        self.assertTrue(sync.push(self.change('recipes/'+r['id'],r,result['cursor']))['conflict'])

    def test_slot_does_not_transfer_other_records_and_preserves_calories(self):
        r=self.recipe();sync.push(self.change('recipes/'+r['id'],r));week='2026-11-16'
        store.put('plans',week,{'week_start':week,'days':[{'date':week,'recipe_id':None}]});before=sync.pull(0)
        prior=next(c for c in before['changes'] if c['key']=='meals/'+week+'/'+week)
        c={'total':123,'per_serving':61.5,'complete':True,'missing':[]}
        result=sync.push(self.change(prior['key'],{'date':week,'recipe_id':r['id'],'calories':c},prior['version'],before['cursor']))
        self.assertEqual([x['key'] for x in result['changes']],[prior['key']])
        self.assertEqual(result['changes'][0]['value']['recipe']['calories'],c)

    def test_restore_validates_all_records_before_commit(self):
        before=sync.pull(0)
        op={'id':str(uuid.uuid4()),'kind':'restore','base':before['cursor'],'document':{'tables':{},'images':[],'config':{}}}
        with self.assertRaises(ValueError):sync.validate_restore(op)
        self.assertEqual(sync.pull(0),before)

    def test_photo_backup_restore_preserves_only_declared_images(self):
        raw=png();photo=sync.save_photo({'id':str(uuid.uuid4()),'name':__import__('hashlib').sha256(raw).hexdigest()+'.png','image':base64.b64encode(raw).decode(),'since':self.initial['cursor']})
        self.assertEqual(len(photo['changes']),1)
        data,images=backups.read_zip(backups.export_zip())
        doc={'tables':data['tables'],'config':data['settings'],'images':list(images)}
        op={'id':str(uuid.uuid4()),'kind':'restore','base':sync.pull(0)['cursor'],'document':doc}
        sync.validate_restore(op);result=sync.push(op);self.assertIn(next(iter(images)),sync.pull(0)['images'])
        blank=copy.deepcopy(doc);blank['images']=[]
        sync.push({'id':str(uuid.uuid4()),'kind':'restore','base':result['cursor'],'document':blank})
        self.assertEqual(sync.pull(0)['images'],[])

    def test_legacy_write_is_visible_incrementally(self):
        r=self.recipe();store.put('recipes',r['id'],r)
        self.assertEqual(sync.pull(self.initial['cursor'])['changes'][0]['value']['id'],r['id'])


if __name__=='__main__':unittest.main()
