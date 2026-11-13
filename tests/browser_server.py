"""Isolated browser fixture: fixed calendar and explicit AI stub, never production."""
import json
import os
import sys
import time
from datetime import date, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import app
from mealapp import ai, store, weekly, backups, nutrition

if not os.environ.get('MEAL_APP_DATA'):raise SystemExit('Run the browser check with an isolated temporary MEAL_APP_DATA folder.')
ai.ROOT=store.DATA
backups.config_path=lambda:store.DATA/'config.local.json'
DAY=date.fromisoformat(os.environ.get('MEAL_TEST_TODAY','2026-11-12'))
MODE='normal'
weekly.today=lambda offset=None:DAY


def ask(task,schema,**kwargs):
    token=getattr(ai.REQUEST,'cancel',None)
    until=time.monotonic()+(30 if MODE=='hold' else .2)
    while time.monotonic()<until:
        if token:token.check()
        time.sleep(.02)
    if MODE=='fail':raise ValueError('Test model failure; saved plan kept.')
    rows=schema['properties']['days']
    ids=rows['items']['properties']['recipe_id']['enum']
    return {'days':[{'recipe_id':ids[i%len(ids)],'timing':'morning','prep':'Weigh the listed ingredients.','reason':'Chosen from saved dishes.'} for i in range(rows['minItems'])]}


ai.ask=ask
nutrition.resolve=lambda names:[{'name':name,'food':store.find_food(name)} if store.find_food(name) else {'name':name,'error':'Test reference unavailable; enter calories manually.'} for name in names]


class Handler(app.Handler):
    def do_POST(self):
        global DAY,MODE
        if self.path.startswith('/test/'):
            if not self.local_request() or self.headers.get('X-App-Token')!=app.TOKEN:
                return self.reply({'error':'Forbidden'},403)
            data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/test/mode':MODE=data['mode']
            if self.path=='/test/date':DAY=date.fromisoformat(data['date'])
            if self.path=='/test/remove-photos':
                folder=(store.DATA/'photos').resolve()
                assert folder.is_relative_to(store.DATA.resolve())
                for image in folder.glob('*.png'):image.unlink()
            return self.reply({'ok':True})
        return super().do_POST()


store.initialize()
archived=app.clean_recipe({'name':'Archived tofu','id':'archived-only','servings':2,'measured_minutes':15,'ingredients':[{'name':'tofu','grams':200}]})
for start in ('2026-11-02','2026-10-26'):
    monday=date.fromisoformat(start)
    plan={'week_start':start,'generated_on':start,'method':'AI','has_ai':True,'days':[{'recipe_id':archived['id'],'date':(monday+timedelta(days=i)).isoformat(),'recipe':weekly.snapshot(archived),'timing':'morning','prep':'Archived preparation.','reason':'Saved history.'} for i in range(7)]}
    store.put('plans',start,plan)
if os.environ.get('MEAL_TEST_LEGACY_SNAPSHOTS'):
    import copy
    import base64
    from test_weekly import png
    image=app.dish_photos.save({'filename':'legacy.png','image':base64.b64encode(png()).decode()})['photo_url']
    legacy={'id':'000-legacy','name':'First legacy soup','vegan':True,'servings':2,'photo_url':image,'notes':('Saved cooking detail.\n'*180),
        'groups':[{'title':'Broth','instructions':'Keep these cooking details.','ingredients':[{'name':'tofu','grams':200,'quantity':'200 g'}]}]}
    store.put('recipes',legacy['id'],legacy)
    for n in range(30):store.put('recipes',f'list-{n:02}',dict(legacy,id=f'list-{n:02}',name=f'Saved dish {n:02}',ingredients=[{'name':'tofu','grams':100}]))
    plan=store.get('plans','2026-11-02')
    for index,kind in enumerate(('groups','legacy_groups','library','unavailable')):
        snapshot={'id':legacy['id'] if kind!='unavailable' else 'missing-recipe','name':'Historical '+kind,'vegan':True,'servings':3,'photo_url':image,'notes':'Historical details stay unchanged.',
            'calories':{'total':111,'per_serving':37,'complete':False,'missing':['Historical estimate']}}
        if kind in ('groups','legacy_groups'):snapshot[kind]=[{'title':'Original ingredients','instructions':'Original technique.','ingredients':[{'name':'tofu','grams':100,'quantity':'original 100 g'}]}]
        plan['days'][index]={'date':plan['days'][index]['date'],'recipe_id':snapshot['id'],'recipe':snapshot,'prep':'Historical preparation.','timing':'night before','reason':'Historical reason.'}
    plan['days'][4]={'date':plan['days'][4]['date'],'recipe_id':legacy['id'],'recipe':dict(snapshot,id=legacy['id'],name='Explicit empty list',ingredients=[],groups=legacy['groups'])}
    plan['days'][5]={'date':plan['days'][5]['date'],'recipe_id':'deleted-old','deleted':True}
    plan['days'][6]={'date':plan['days'][6]['date'],'recipe_id':None}
    store.put('plans',plan['week_start'],plan)

server=ThreadingHTTPServer(('127.0.0.1',int(os.environ.get('MEAL_TEST_PORT','8768'))),Handler)
try:server.serve_forever()
finally:server.server_close()
