import copy
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app
from mealapp import ai, backups, store, weekly


class RecipeDeletionTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        for item in (patch.object(store, 'DATA', root / 'data'),
                     patch.object(ai, 'ROOT', root),
                     patch.object(backups, 'config_path', return_value=root / 'config.local.json'),
                     patch.object(weekly, 'today', return_value=date(2026, 11, 11))):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.folder.cleanup)
        store.initialize()
        self.dish = app.clean_recipe({'id':'deleted-dish', 'name':'Tofu soup', 'servings':2,
            'measured_minutes':15, 'notes':'Original details', 'ingredients':[{'name':'tofu', 'grams':200}]})
        self.other = app.clean_recipe(dict(self.dish, id='other-dish', name='Another soup'))
        for recipe in (self.dish, self.other):
            store.put('recipes', recipe['id'], recipe)
        weekly.history()
        for week in ('2026-11-02', '2026-11-09', '2026-11-16'):
            start = date.fromisoformat(week)
            days = []
            for i in range(7):
                day = {'date':(start + timedelta(days=i)).isoformat(), 'recipe_id':None}
                if i != 1:
                    recipe = self.other if i == 3 else self.dish
                    day.update(recipe_id=recipe['id'], recipe=weekly.snapshot(recipe),
                        prep='Original preparation', timing='morning', reason='Original advice')
                days.append(day)
            store.put('plans', week, {'week_start':week, 'method':'manual', 'days':days})

    def test_delete_marks_every_occurrence_preserving_dates_other_dishes_and_empty_slots(self):
        before = copy.deepcopy(weekly.history())
        app.post('/api/recipes/delete', {'id':self.dish['id']})
        self.assertIsNone(store.get('recipes', self.dish['id']))
        self.assertEqual(store.get('recipes', self.other['id']), self.other)
        store.initialize()
        after = weekly.history()
        for old, new in zip(before, after):
            expected = copy.deepcopy(old)
            for i, day in enumerate(old['days']):
                if day['recipe_id'] == self.dish['id']:
                    expected['days'][i] = {'date':day['date'], 'recipe_id':self.dish['id'], 'deleted':True}
            self.assertEqual(new, expected)
        # A recreated ID cannot silently revive a scheduled deleted occurrence.
        store.put('recipes', self.dish['id'], self.dish)
        self.assertEqual(weekly.history(), after)
        target = after[0]
        result = weekly.replace(target['week_start'], target['days'][0]['date'], self.other['id'])['plan']
        self.assertNotIn('deleted', result['days'][0])
        self.assertEqual(result['days'][0]['recipe_id'], self.other['id'])
        self.assertEqual(result['days'][1:], target['days'][1:])

    def test_delete_is_atomic_if_updating_plans_fails(self):
        before = copy.deepcopy(weekly.history())
        with store.db() as connection:
            connection.execute("CREATE TRIGGER reject_plan_update BEFORE UPDATE ON plans BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            app.post('/api/recipes/delete', {'id':self.dish['id']})
        self.assertEqual(store.get('recipes', self.dish['id']), self.dish)
        self.assertEqual(weekly.history(), before)

    def test_regeneration_keeps_earlier_deleted_slots_and_uses_remaining_recipes(self):
        app.post('/api/recipes/delete', {'id':self.dish['id']})
        earlier = store.get('plans', '2026-11-09')['days'][:2]
        from mealapp import plan_jobs
        result = plan_jobs.instant({'method':'rotation'})
        self.assertEqual(result['days'][:2], earlier)
        self.assertTrue(result['days'][0]['deleted'])
        self.assertTrue(all(day['recipe_id']==self.other['id'] and not day.get('deleted') for day in result['days'][2:]))

    def test_legacy_occurrences_and_backup_restore_keep_deleted_state(self):
        store.put('state', 'weekly_history_migrated', False)
        store.put('state', 'plan', {'week_start':'2026-10-26', 'days':[
            {'recipe_id':self.dish['id'], 'recipe':weekly.snapshot(self.dish), 'prep':'Old instructions'}]})
        app.post('/api/recipes/delete', {'id':self.dish['id']})
        legacy = weekly.history()[-1]
        self.assertEqual(legacy['days'], [{'date':'2026-10-26', 'recipe_id':self.dish['id'], 'deleted':True}])
        before = copy.deepcopy(weekly.history())
        archive = backups.export_zip()
        store.put('recipes', self.dish['id'], self.dish)
        weekly.replace('2026-11-16', '2026-11-16', self.other['id'])
        backups.import_zip(archive)
        self.assertIsNone(store.get('recipes', self.dish['id']))
        self.assertEqual(weekly.history(), before)
