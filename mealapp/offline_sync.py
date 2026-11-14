"""Record-level sync. The browser outbox is authoritative until acknowledged.

Hosted operations are applied by one owner-scoped PostgreSQL transaction. Local
development uses the same protocol with durable SQLite receipts and tombstones.
No nutrition/AI work runs on the manual mutation path.
"""
import copy
import hashlib
import json
import re
import uuid
from . import ai, backups, dish_photos, hosted, store, weekly, plan_jobs

TABLES = ('foods', 'recipes', 'plans', 'state', 'cache')
KEY = re.compile(r'(?:recipes|foods)/[\s\S]{1,4096}|plans/\d{4}-\d{2}-\d{2}|meals/\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}|expansion/\d{4}-\d{2}-\d{2}|settings')


def flatten(document):
    result = {}
    for name in document.get('images', []): result['photos/'+name] = {'name':name}
    tables = document.get('tables', {})
    for table in ('recipes', 'foods'):
        for row in tables.get(table, []): result[table+'/'+row['id']] = row['body']
    for row in tables.get('plans', []):
        plan = copy.deepcopy(row['body'])
        for day in plan.pop('days', []): result['meals/'+row['id']+'/'+day['date']] = day
        result['plans/'+row['id']] = plan
    for row in tables.get('state', []):
        if row['id'] == 'settings': result['settings'] = row['body']
        if row['id'] == 'week_expansion':
            for week, expanded in row['body'].items(): result['expansion/'+week] = expanded
    return result


def document_from(entities, previous):
    doc = copy.deepcopy(previous)
    tables = doc.setdefault('tables', {})
    for table in ('recipes', 'foods'):
        tables[table] = [{'id':key[len(table)+1:], 'body':value} for key,value in entities.items() if key.startswith(table+'/') and value is not None]
    tables['plans'] = []
    for key,value in entities.items():
        if key.startswith('plans/') and value is not None:
            week = key[6:]
            days = [day for k,day in entities.items() if k.startswith('meals/'+week+'/') and day is not None]
            tables['plans'].append({'id':week, 'body':dict(value, days=sorted(days, key=lambda d:d['date']))})
    retained = [row for row in tables.get('state', []) if row['id'] not in ('settings', 'week_expansion')]
    retained.append({'id':'week_expansion', 'body':{k[10:]:v for k,v in entities.items() if k.startswith('expansion/') and v is not None}})
    if entities.get('settings') is not None: retained.append({'id':'settings', 'body':entities['settings']})
    tables['state'] = retained
    return doc


def validate_operation(op):
    uuid.UUID(op['id'])
    if op.get('kind') == 'photo':
        if not dish_photos.URL.fullmatch('/media/'+str(op.get('name'))): raise ValueError('Invalid image name.')
        return
    if op.get('kind') == 'restore':
        if not isinstance(op.get('document'), dict) or type(op.get('base')) is not int: raise ValueError('Invalid restore operation.')
        return
    changes = op.get('changes')
    if not isinstance(changes, list) or not 1 <= len(changes) <= 32: raise ValueError('Use between one and 32 record changes.')
    seen = set()
    for change in changes:
        key, value = change['key'], change['value']
        if not isinstance(key, str) or not KEY.fullmatch(key) or key in seen: raise ValueError('Invalid or duplicate sync key.')
        seen.add(key)
        if type(change.get('base')) is not int or change['base'] < 0: raise ValueError('Invalid record version.')
        if value is None: continue
        if key.startswith('recipes/'):
            # Validate without performing online nutrition or image requests.
            backups.validate_recipe(value, {i.get('food_id'):{} for i in store.recipe_ingredients(value)})
            if value.get('id') != key[8:]: raise ValueError('Recipe ID mismatch.')
        elif key.startswith('foods/'):
            if value.get('id') != key[6:] or not value.get('name'): raise ValueError('Invalid food.')
            from .nutrition import number
            number(value.get('kcal'), 'Calories per 100 g', 0, 1000)
        elif key.startswith('plans/'):
            weekly.week_date(key[6:])
            if value.get('week_start') != key[6:] or 'days' in value: raise ValueError('Invalid week metadata.')
        elif key.startswith('meals/'):
            from datetime import date
            _, week, day = key.split('/')
            start = weekly.week_date(week)
            if not isinstance(value, dict) or value.get('date') != day or not 0 <= (date.fromisoformat(day)-start).days <= 6: raise ValueError('Invalid meal date.')
            if value.get('recipe_id') is not None and not isinstance(value['recipe_id'], str): raise ValueError('Invalid dish ID.')
            if 'serving_plan' in value: backups.validate_serving_plan(value['serving_plan'])
        elif key.startswith('expansion/'):
            weekly.week_date(key[10:])
            if type(value) is not bool: raise ValueError('Invalid collapse state.')
        elif key == 'settings':
            backups.validate_settings(value)
            # Older browsers may still have this retired setting queued. Accept
            # its metadata without using it for inference or exposing an image API.
            if hosted.enabled() and any(k not in ('language','text_model','vietnamese_model','english_model','household_servings','vision_model') for k in value):
                raise ValueError('Hosted provider settings are managed by the beta operator.')


def read_document(connection):
    saved=connection.execute("SELECT value FROM sync_meta WHERE id='images'").fetchone()
    return {'tables':{table:[{'id':row[0], 'body':json.loads(row[1])} for row in connection.execute('SELECT id,body FROM '+table+' ORDER BY rowid')] for table in TABLES},
            'images':json.loads(saved[0]) if saved else sorted(p.name for p in (store.DATA/'photos').glob('*.png')), 'config':None}


def local_prepare(c):
    c.execute('CREATE TABLE IF NOT EXISTS sync_records (key TEXT PRIMARY KEY, body TEXT, revision INTEGER NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS sync_ops (id TEXT PRIMARY KEY, hash TEXT NOT NULL, result TEXT NOT NULL)')
    c.execute('CREATE TABLE IF NOT EXISTS sync_meta (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
    c.execute("INSERT OR IGNORE INTO sync_meta VALUES ('uid',?)", (str(uuid.uuid4()),))
    cursor = int((c.execute("SELECT value FROM sync_meta WHERE id='cursor'").fetchone() or ['0'])[0])
    entities = flatten(read_document(c))
    old = {row['key']:json.loads(row['body']) for row in c.execute('SELECT * FROM sync_records')}
    changed = {key:entities.get(key) for key in entities.keys() | old.keys() if entities.get(key) != old.get(key) or key not in old}
    if changed:
        cursor += 1
        for key,body in changed.items():
            c.execute('INSERT INTO sync_records VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET body=excluded.body,revision=excluded.revision', (key,json.dumps(body,ensure_ascii=False),cursor))
        c.execute("INSERT OR REPLACE INTO sync_meta VALUES ('cursor',?)", (str(cursor),))
    return cursor


def profile(handler):
    if hosted.enabled(): uid = hosted.current().uid
    else:
        with store.db() as c:
            local_prepare(c)
            uid = c.execute("SELECT value FROM sync_meta WHERE id='uid'").fetchone()[0]
    return {'uid':uid, 'hosted':hosted.enabled(), 'token':hosted.csrf() if hosted.enabled() else handler.local_token}


def pull(since):
    if hosted.enabled():
        return hosted.remote('/rest/v1/rpc/health_beta_sync_pull', {'after_revision':since}, token=hosted.current().token)
    with store.db() as c:
        cursor = local_prepare(c)
        result = {'cursor':cursor, 'changes':[{'key':r['key'], 'value':json.loads(r['body']), 'version':r['revision']} for r in c.execute('SELECT * FROM sync_records WHERE revision>? ORDER BY revision,key',(since,))]}
        if since == 0: result['images'] = read_document(c)['images']
        return result


def bootstrap(handler):
    if hosted.enabled():
        with hosted.current().lock:
            hosted.load(hosted.current())
            weekly.history()
            hosted.persist()
    else: weekly.history()
    result = pull(0)
    settings = ai.config()
    if hosted.enabled(): settings['usda_api_key'] = 'DEMO_KEY'
    return dict(result, profile=profile(handler), defaults=settings)


def apply_entities(entities, changes):
    """Deletion wins over stale references. The SQL RPC implements the same rules."""
    result = copy.deepcopy(entities)
    for change in changes: result[change['key']] = copy.deepcopy(change['value'])
    for change in changes:
        key,value = change['key'],change['value']
        if key.startswith('meals/') and value is not None:
            week = key.split('/')[1]
            if result.get('plans/'+week) is None: raise ValueError('This week was deleted. Your change is still saved locally.')
            if value.get('recipe_id') and not value.get('deleted'):
                recipe = result.get('recipes/'+value['recipe_id'])
                if recipe is None: raise ValueError('This dish was deleted. Select another dish.')
                value['recipe'] = copy.deepcopy(recipe)
                value['recipe'].pop('source_text',None)
                if 'calories' in value: value['recipe']['calories']=value.pop('calories')
                result[key] = value
                if result['plans/'+week].get('vegan_only') and recipe.get('vegan') is not True:
                    result['plans/'+week]['vegan_only']=False
        if key.startswith('recipes/') and value is None:
            for slot,day in list(result.items()):
                if slot.startswith('meals/') and day and day.get('recipe_id') == key[8:]: result[slot] = {'date':day['date'], 'recipe_id':key[8:], 'deleted':True}
        if key.startswith('plans/') and value is None:
            for slot in list(result):
                if slot.startswith('meals/'+key[6:]+'/'): result[slot] = None
    return result


def push(op):
    validate_operation(op)
    if hosted.enabled():
        result = hosted.remote('/rest/v1/rpc/health_beta_sync_push', {'operation':op}, token=hosted.current().token)
        hosted.invalidate(hosted.current())
        return result
    with store.db() as c:
        cursor = local_prepare(c)
        digest = hashlib.sha256(json.dumps(op,sort_keys=True).encode()).hexdigest()
        receipt = c.execute('SELECT * FROM sync_ops WHERE id=?',(op['id'],)).fetchone()
        if receipt:
            if receipt['hash'] != digest: raise ValueError('An operation ID cannot be reused for a different change.')
            return json.loads(receipt['result'])
        rows = {r['key']:{'key':r['key'],'value':json.loads(r['body']),'version':r['revision']} for r in c.execute('SELECT * FROM sync_records')}
        changes = op.get('changes', [])
        conflicts = [rows.get(x['key'],dict(key=x['key'],value=None,version=0)) for x in changes if rows.get(x['key'],{}).get('version',0) != x['base'] or x['key'].startswith('recipes/') and x['value'] is not None and rows.get(x['key'],{}).get('value','new') is None]
        if op.get('kind') == 'restore' and op['base'] != cursor: conflicts = list(rows.values())
        if conflicts: return {'conflict':True,'changes':conflicts,'message':'This record changed elsewhere. Your local edit is retained.'}
        old = read_document(c)
        if op.get('kind') == 'restore':
            doc = copy.deepcopy(op['document'])
            # Keep local model/config restoration inside the same SQLite commit
            # rather than adding a nontransactional config-file write.
            config=dict(doc.get('config') or {})
            config.update(next((r['body'] for r in doc['tables']['state'] if r['id']=='settings'),{}))
            doc['tables']['state']=[r for r in doc['tables']['state'] if r['id']!='settings']+[{'id':'settings','body':config}]
        elif op.get('kind') == 'photo': doc = old
        else: doc = document_from(apply_entities({k:v['value'] for k,v in rows.items()},changes),old)
        for table in TABLES:
            c.execute('DELETE FROM '+table)
            c.executemany('INSERT INTO '+table+' VALUES (?,?)',[(r['id'],json.dumps(r['body'],ensure_ascii=False)) for r in doc['tables'][table]])
        if op.get('kind')=='restore':
            c.execute("INSERT OR REPLACE INTO sync_meta VALUES ('images',?)",(json.dumps(doc['images']),))
        local_prepare(c)
        cursor = int((c.execute("SELECT value FROM sync_meta WHERE id='cursor'").fetchone() or ['0'])[0])
        affected = [dict(key=r['key'],value=json.loads(r['body']),version=r['revision']) for r in c.execute('SELECT * FROM sync_records WHERE revision>?',(op.get('since',cursor-1),))]
        result = {'id':op['id'],'cursor':cursor,'changes':affected}
        c.execute('INSERT INTO sync_ops VALUES (?,?,?)',(op['id'],digest,json.dumps(result,ensure_ascii=False)))
        return result


def save_photo(data):
    uuid.UUID(data['id'])
    staged=data.get('staged',False)
    if type(staged) is not bool: raise ValueError('Invalid image staging flag.')
    if staged and not hosted.enabled():
        with store.db() as c:
            local_prepare(c)
            c.execute("INSERT OR IGNORE INTO sync_meta VALUES ('images',?)",(json.dumps(read_document(c)['images']),))
    result = dish_photos.save(dict(data,filename='crop.png',_staged=staged))
    name = result['photo_url'][7:]
    if data.get('name') != name: raise ValueError('Image checksum mismatch.')
    if hosted.enabled():
        session = hosted.current()
        raw = (session.path/'photos'/name).read_bytes()
        hosted.remote('/storage/v1/object/health-diet-images/'+session.uid+'/'+name, raw, token=session.token, raw=True)
        session.image_names.add(name)
        session.image_sizes[name] = len(raw)
    return push({'id':data['id'],'kind':'photo','name':name,'staged':staged,'size':(store.DATA/'photos'/name).stat().st_size,'since':data.get('since',0)})


def note_photo(name):
    if hosted.enabled(): return
    with store.db() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='sync_meta'").fetchone(): return
        row=c.execute("SELECT value FROM sync_meta WHERE id='images'").fetchone()
        if row:
            names=set(json.loads(row[0]));names.add(name)
            c.execute("UPDATE sync_meta SET value=? WHERE id='images'",(json.dumps(sorted(names)),))


def validate_restore(op):
    doc=op.get('document',{})
    images={}
    for name in doc.get('images',[]):
        if not dish_photos.URL.fullmatch('/media/'+name): raise ValueError('Invalid backup image.')
        if hosted.enabled(): hosted.ensure_photo(name,allow_staged=True)
        path=store.DATA/'photos'/name
        if not path.is_file(): raise ValueError('Upload all backup images before restoring.')
        images[name]=path.read_bytes()
    if hosted.enabled() and (sum(map(len,images.values()))>hosted.MAX_PHOTOS or len(json.dumps(doc).encode())>hosted.MAX_DOCUMENT):
        raise ValueError('The hosted beta supports 12 MB of records and 32 MB of photos per tester. Existing data was kept.')
    manifest={'format':'meal-notebook-backup','version':1,'tables':doc.get('tables'),'settings':doc.get('config'),'images':doc.get('images'),
              'record_counts':{key:len(rows) for key,rows in doc.get('tables',{}).items()}}
    backups.validate_data(manifest,images)


def online(route,data):
    """AI results are suggestions, never implicit workspace mutations."""
    if hosted.enabled():
        # Provider config is fixed server-side. Only initialize a disposable
        # cache, not a workspace download or a full-document durable save.
        store.initialize()
    if route=='/import':
        source=str(data.get('text',''))[:30000]
        if len(source)<30: raise ValueError('Paste ingredients and instructions first.')
        recipe=ai.extract_recipe(source)
        recipe.update(source_text=source,source_url=str(data.get('url',''))[:2000])
        return {'recipe':store.normalize_recipe(recipe),'resolved':[]}
    if route=='/plan/start':
        rows=data.get('candidates')
        if not isinstance(rows,list) or not 1<=len(rows)<=12: raise ValueError('Send up to twelve matching saved dishes.')
        compact=[]
        for row in rows:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not row.get('name'): raise ValueError('Invalid saved dish.')
            compact.append({'id':row['id'][:100],'name':str(row['name'])[:200],'good_for_running':bool(row.get('good_for_running')),'vegan':row.get('vegan'),
                'measured_minutes':row.get('minutes'),'ingredients':[{'name':str(n)[:100]} for n in row.get('ingredients',[])[:12]]})
        return plan_jobs.start(dict(data,client_only=True,client_recipes=compact))
    allowed={'/extract','/source-language','/resolve','/plan/cancel'}
    if route not in allowed: raise ValueError('Unknown online feature.')
    from app import post
    return post('/api'+route,data)
