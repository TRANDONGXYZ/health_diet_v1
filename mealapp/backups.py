"""Versioned ZIP backups; validation precedes a locked, rollback-capable restore."""
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zipfile
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path
from . import ai, dish_photos, plan_jobs, store, weekly

TABLES=('foods','recipes','state','cache','plans')
MANIFEST='backup-v1.json'
MAX_ARCHIVE=256*1024*1024
MAX_EXPANDED=512*1024*1024
MAX_JSON=64*1024*1024
IMAGE=re.compile(r'[a-f0-9]{64}\.png\Z')


def config_path():
    from . import hosted
    if hosted.enabled():return hosted.config_path()
    return store.ROOT/'config.local.json'


def json_bytes(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode('utf-8')


def export_zip():
    from . import hosted
    hosted.ensure_all_photos()
    with store.LOCK:
        weekly.history()
        with store.db() as connection:
            tables={table:[{'id':row[0],'body':json.loads(row[1])} for row in connection.execute(f'SELECT id, body FROM {table} ORDER BY rowid')] for table in TABLES}
        images={}
        folder=store.DATA/'photos'
        if folder.exists():
            if folder.is_symlink():raise ValueError('The photo folder must be a local directory.')
            for path in sorted(folder.iterdir()):
                if IMAGE.fullmatch(path.name):
                    if path.is_symlink():raise ValueError('Photo links cannot be included in a backup.')
                    images[path.name]=path.read_bytes()
        settings=ai.config()
        from . import hosted
        if hosted.enabled():
            # Never export an operator's environment secrets with a tester backup.
            settings['usda_api_key']=store.get('state','settings',{}).get('usda_api_key','DEMO_KEY')
        manifest={'format':'meal-notebook-backup','version':1,'created_at':datetime.now(timezone.utc).isoformat(),
                  'tables':tables,'record_counts':{key:len(rows) for key,rows in tables.items()},'settings':settings,'images':list(images)}
        validate_data(manifest,images)
        data=json_bytes(manifest)
        if len(data)>MAX_JSON or len(data)+sum(map(len,images.values()))>MAX_EXPANDED:
            raise ValueError('The backup exceeds the supported 512 MB expanded size.')
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST,data)
            for name,raw in images.items():archive.writestr('images/'+name,raw)
        raw=output.getvalue()
        if len(raw)>MAX_ARCHIVE:raise ValueError('The backup exceeds the supported 256 MB ZIP size.')
        return raw


def object_pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate JSON fields in backup.')
        result[key]=value
    return result


def read_zip(raw):
    from . import hosted
    expanded_limit=48*1024*1024 if hosted.enabled() else MAX_EXPANDED
    json_limit=12*1024*1024 if hosted.enabled() else MAX_JSON
    if not raw or len(raw)>(48*1024*1024 if hosted.enabled() else MAX_ARCHIVE):raise ValueError('Choose a ZIP backup under '+('48' if hosted.enabled() else '256')+' MB.')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries=archive.infolist();names=[item.filename for item in entries]
            if len(entries)>10000 or len(names)!=len(set(names)) or sum(i.file_size for i in entries)>expanded_limit:
                raise ValueError('Duplicate entries or excessive expanded backup size.')
            if MANIFEST not in names:raise ValueError('Missing versioned backup-v1.json file.')
            for item in entries:
                name=item.filename
                if item.flag_bits&1 or (item.external_attr>>16)&0o170000==0o120000:
                    raise ValueError('Encrypted files and symbolic links are not supported.')
                if name=='images/' and item.is_dir():continue
                if name!=MANIFEST and not (name.startswith('images/') and IMAGE.fullmatch(name[7:])):
                    raise ValueError('Unexpected file or unsafe path in backup.')
                if item.is_dir() or item.compress_type not in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED):
                    raise ValueError('Unsupported archive entry.')
                if item.file_size>(json_limit if name==MANIFEST else 4_000_000):
                    raise ValueError('An archive entry is too large.')
            manifest=json.loads(archive.read(MANIFEST).decode('utf-8'),object_pairs_hook=object_pairs,
                                parse_constant=lambda value:(_ for _ in ()).throw(ValueError('Invalid number in backup.')))
            images={name[7:]:archive.read(name) for name in names if name.startswith('images/') and name!='images/'}
        validate_data(manifest,images)
        return manifest,images
    except (zipfile.BadZipFile,UnicodeError,KeyError,TypeError,RecursionError,RuntimeError,NotImplementedError) as exc:
        raise ValueError('Invalid or incomplete backup archive. Existing data was kept.') from exc


def numeric(value):
    try:
        return value is None or isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value>=0
    except OverflowError:
        return False


def validate_recipe(recipe,foods):
    if not isinstance(recipe,dict) or not isinstance(recipe.get('name'),str) or not recipe['name'].strip():
        raise ValueError('A recipe in the backup is invalid.')
    if recipe.get('vegan') is not None and not isinstance(recipe['vegan'],bool):raise ValueError('Invalid vegan status.')
    if not isinstance(recipe.get('good_for_running',False),bool):raise ValueError('Invalid running label.')
    for key in ('servings','measured_minutes','estimated_minutes'):
        if not numeric(recipe.get(key)) or recipe.get(key)==0:raise ValueError('Invalid recipe number.')
        value=recipe.get(key)
        if value is not None and not ((.01<=value<=10000) if key=='servings' else (1<=value<=10080)):
            raise ValueError('Recipe number is outside the supported range.')
    if 'ingredients' in recipe and not isinstance(recipe['ingredients'],list):raise ValueError('Invalid ingredient list.')
    if 'groups' in recipe and not isinstance(recipe['groups'],list):raise ValueError('Invalid legacy ingredient list.')
    if any(not isinstance(group,dict) or not isinstance(group.get('ingredients',[]),list) for group in recipe.get('groups',[])):
        raise ValueError('Invalid legacy ingredient group.')
    items=store.recipe_ingredients(recipe)
    if not isinstance(items,list) or len(items)>200:raise ValueError('Invalid ingredient list.')
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not item['name'].strip() or not numeric(item.get('grams')):
            raise ValueError('Invalid ingredient in backup.')
        if item.get('grams') is not None and item['grams']>1_000_000:raise ValueError('Ingredient weight is too large.')
        if item.get('food_id') and item['food_id'] not in foods:raise ValueError('A recipe references a missing food value.')
    for key in ('source_url','source_text','notes'):
        if key in recipe and not isinstance(recipe[key],str):raise ValueError('Invalid recipe text.')
    if 'calories' in recipe:
        calories=recipe['calories']
        if not isinstance(calories,dict) or any(key not in calories for key in ('total','per_serving','complete','missing')) or not isinstance(calories['complete'],bool) or not isinstance(calories['missing'],list):
            raise ValueError('Invalid recipe calorie snapshot.')
        if not numeric(calories['total']) or not numeric(calories['per_serving']):raise ValueError('Invalid calorie number.')
        if any(not isinstance(item,str) and not (isinstance(item,dict) and isinstance(item.get('name'),str)) for item in calories['missing']):
            raise ValueError('Invalid missing-calorie details.')


def validate_settings(settings):
    if not isinstance(settings,dict):raise ValueError('Invalid backup settings.')
    for key,value in settings.items():
        if key in ('text_model','vision_model','vietnamese_model','english_model') and (not isinstance(value,str) or not value.strip()):
            raise ValueError('Invalid model settings.')
        if key in ('ollama_url','usda_api_key','speech_model','speech_language') and not isinstance(value,str):raise ValueError('Invalid settings value.')
        if key in ('context_size','ai_timeout_seconds','ai_read_timeout_seconds','speech_threads','speech_timeout_seconds','speech_max_video_minutes','video_download_timeout_seconds'):
            if not numeric(value) or value is None or value<=0:raise ValueError('Invalid settings number.')
    if 'ollama_url' in settings:
        endpoint=urllib.parse.urlsplit(settings['ollama_url'])
        if endpoint.scheme not in ('http','https') or not endpoint.hostname or endpoint.username or endpoint.password:raise ValueError('Invalid Ollama URL in backup.')


def validate_data(data,images):
    if not isinstance(data,dict) or data.get('format')!='meal-notebook-backup' or type(data.get('version')) is not int or data['version']!=1:
        raise ValueError('Unsupported backup format or version.')
    if not isinstance(data.get('tables'),dict) or set(data['tables'])!=set(TABLES) or 'settings' not in data:
        raise ValueError('The backup is missing required data tables or settings.')
    if not isinstance(data.get('record_counts'),dict) or set(data['record_counts'])!=set(TABLES):
        raise ValueError('The backup record counts are missing.')
    listed=data.get('images')
    if not isinstance(listed,list) or any(not isinstance(n,str) for n in listed) or len(set(listed))!=len(listed) or set(listed)!=set(images):
        raise ValueError('The backup image folder is incomplete.')
    for name,raw in images.items():
        if not IMAGE.fullmatch(name) or hashlib.sha256(raw).hexdigest()+'.png'!=name:raise ValueError('A backup image is corrupt or has the wrong filename.')
        dish_photos.validate_png(raw)
    tables={}
    for table,rows in data['tables'].items():
        if not isinstance(rows,list):raise ValueError('Invalid backup data table.')
        if type(data['record_counts'][table]) is not int or data['record_counts'][table]!=len(rows):raise ValueError('The backup is missing records.')
        items={}
        for row in rows:
            if not isinstance(row,dict) or set(row)!= {'id','body'} or not isinstance(row['id'],str) or not 0<len(row['id'])<=4096 or row['id'] in items:
                raise ValueError('Invalid or duplicate backup record.')
            items[row['id']]=row['body']
        tables[table]=items
    for key,food in tables['foods'].items():
        if not isinstance(food,dict) or food.get('id')!=key or not isinstance(food.get('name'),str) or not food['name'].strip() or not numeric(food.get('kcal')) or food.get('kcal') is None or food['kcal']>1000:
            raise ValueError('Invalid food value in backup.')
        if not isinstance(food.get('aliases',[]),list) or any(not isinstance(alias,str) for alias in food.get('aliases',[])):
            raise ValueError('Invalid food aliases in backup.')
    for key,recipe in tables['recipes'].items():
        if not isinstance(recipe,dict) or recipe.get('id')!=key:raise ValueError('Recipe ID mismatch.')
        validate_recipe(recipe,tables['foods'])
    for key,plan in tables['plans'].items():
        weekly.week_date(key)
        if not isinstance(plan,dict) or plan.get('week_start')!=key or not isinstance(plan.get('days'),list) or not 1<=len(plan['days'])<=7:
            raise ValueError('Invalid weekly plan in backup.')
        if 'warnings' in plan and (not isinstance(plan['warnings'],list) or any(not isinstance(item,str) for item in plan['warnings'])):
            raise ValueError('Invalid weekly plan warnings.')
        if 'method' in plan and not isinstance(plan['method'],str):raise ValueError('Invalid plan method.')
        for flag in ('vegan_only','prioritize_running'):
            if flag in plan and not isinstance(plan[flag],bool):raise ValueError('Invalid plan preference.')
        dates=set();start=date.fromisoformat(key)
        for day in plan['days']:
            if not isinstance(day,dict) or not isinstance(day.get('date'),str):raise ValueError('Invalid planned date.')
            scheduled=date.fromisoformat(day['date'])
            if scheduled.isoformat()!=day['date'] or not 0<=(scheduled-start).days<=6 or day['date'] in dates:raise ValueError('Invalid or duplicate planned date.')
            dates.add(day['date'])
            if day.get('recipe_id') is not None and (not isinstance(day['recipe_id'],str) or not day['recipe_id']):raise ValueError('Invalid planned dish ID.')
            if day.get('recipe') is not None:
                validate_recipe(day['recipe'],tables['foods'])
                if not isinstance(day['recipe'].get('ingredients'),list):raise ValueError('Missing planned recipe ingredients.')
                if day['recipe'].get('id')!=day.get('recipe_id'):raise ValueError('Planned dish snapshot ID mismatch.')
    if tables['state'].get('weekly_history_migrated') is not True:raise ValueError('Missing weekly history metadata.')
    if 'week_expansion' in tables['state']:
        choices=tables['state']['week_expansion']
        if not isinstance(choices,dict):raise ValueError('Invalid week expansion settings.')
        for week,expanded in choices.items():
            weekly.week_date(week)
            if not isinstance(expanded,bool):raise ValueError('Invalid week expansion choice.')
    validate_settings(data['settings'])
    if any(key not in data['settings'] for key in ('text_model','vision_model','vietnamese_model','english_model','ollama_url','usda_api_key')):raise ValueError('Missing backup settings.')
    if 'settings' in tables['state']:validate_settings(tables['state']['settings'])
    def check_photos(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='photo_url' and item:
                    match=dish_photos.URL.fullmatch(str(item))
                    if not match or match[1] not in images:raise ValueError('A record references a missing backup image.')
                else:check_photos(item)
        elif isinstance(value,list):
            for item in value:check_photos(item)
    check_photos(data['tables'])


def import_zip(raw):
    data,images=read_zip(raw)  # No changes to the live database or files before this completes.
    serialized={table:[(row['id'],json_bytes(row['body']).decode('utf-8')) for row in rows] for table,rows in data['tables'].items()}
    with plan_jobs.LOCK:
        if plan_jobs.active():raise ValueError('Cancel meal generation before importing a backup.')
        if not ai.LOCK.acquire(blocking=False):raise ValueError('Wait for the current AI task before importing a backup.')
        try:
            with store.LOCK:
                store.check_epoch()
                return apply_validated(data,images,serialized)
        finally:ai.LOCK.release()


def apply_validated(data,images,serialized):
    photos=store.DATA/'photos';config=config_path()
    if photos.is_symlink() or config.is_symlink():raise ValueError('Restore destinations must be local files and folders.')
    store.DATA.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.restore-',dir=store.DATA,ignore_cleanup_errors=True) as folder:
        stage=Path(folder);prepared=stage/'new-photos';prepared.mkdir()
        for name,raw in images.items():(prepared/name).write_bytes(raw)
        (stage/'new-config.json').write_bytes(json_bytes(data['settings']))
        old_photos=False;new_photos=False;old_config=False;new_config=False
        try:
            if photos.exists():os.replace(photos,stage/'old-photos');old_photos=True
            os.replace(prepared,photos);new_photos=True
            if config.exists():os.replace(config,stage/'old-config.json');old_config=True
            os.replace(stage/'new-config.json',config);new_config=True
            with store.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                for table in TABLES:
                    connection.execute(f'DELETE FROM {table}')
                    connection.executemany(f'INSERT INTO {table} VALUES (?,?)',serialized[table])
            store.bump_epoch()
            plan_jobs.clear_current()
            from . import hosted
            hosted.replace_photo_manifest(images)
        except Exception as exc:
            if new_config:os.replace(config,stage/'failed-config.json')
            if old_config:os.replace(stage/'old-config.json',config)
            if new_photos:os.replace(photos,stage/'failed-photos')
            if old_photos:os.replace(stage/'old-photos',photos)
            raise ValueError('Could not apply the backup. Existing data was kept.') from exc
    return {'ok':True,'recipes':len(serialized['recipes']),'weeks':len(serialized['plans']),'images':len(images)}
