"""Render beta: anonymous Supabase identity and durable per-tester workspaces.

SQLite and photo files are cached per-tester working copies. Supabase Postgres is
the authority; successful writes are acknowledged only after a revision-checked
commit there. Storage objects are private and use the tester's JWT, never an
administrative key.
"""
import base64
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
import hashlib
import hmac
import http.client
from http.cookies import SimpleCookie
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

CURRENT = ContextVar('health_tester', default=None)
TIMINGS = ContextVar('health_request_timings', default=None)
SESSIONS = {}
SESSION_LOCK = threading.RLock()
SIGNUPS = {}
TABLES = ('foods', 'recipes', 'state', 'cache', 'plans')
IMAGE = re.compile(r'[a-f0-9]{64}\.png\Z')
MAX_DOCUMENT = 12 * 1024 * 1024
MAX_PHOTOS = 32 * 1024 * 1024
LOG = logging.getLogger('mealapp.hosted')
PROCESS_STARTED=time.monotonic()


class HostedServiceError(ValueError):
    """A deliberately safe public message, never a raw provider response."""
    def __init__(self, message, code='hosted_unavailable', status=503):
        super().__init__(message)
        self.code, self.status = code, status


def operation(path):
    if path.startswith('/auth/v1/signup'):return 'anonymous_signup'
    if path.startswith('/auth/v1/token'):return 'session_refresh'
    if path.startswith('/rest/v1/rpc/health_beta_save'):return 'records_save'
    if path.startswith('/rest/v1/rpc/health_beta_delete_week'):return 'week_delete'
    if path.startswith('/rest/v1/rpc/health_beta_take_ai'):return 'ai_budget'
    if path.startswith('/rest/v1/health_beta_data'):return 'records_read'
    if path.startswith('/storage/'):return 'image_storage'
    return 'supabase_request'


def provider_error(path, status, payload):
    """Map only known codes to our own text; discard provider messages/URLs."""
    stage=operation(path)
    code=str(payload.get('error_code') or payload.get('code') or '') if isinstance(payload,dict) else ''
    known={'anonymous_provider_disabled','signup_disabled','captcha_failed','over_request_rate_limit',
           'over_anonymous_request_rate_limit','PGRST202','PGRST205','42P01','42501','PGRST106','PGRST301','PGRST302'}
    safe_code=code if code in known else 'unrecognized'
    LOG.warning('Supabase request failed: operation=%s http_status=%s code=%s',stage,status,safe_code)
    if code=='anonymous_provider_disabled':
        return HostedServiceError('Anonymous Sign-Ins are disabled in Supabase. In your Supabase project, open Authentication > Sign In / Providers, enable Anonymous Sign-Ins and save, then reload this page.', 'anonymous_signins_disabled')
    if code=='signup_disabled':
        return HostedServiceError('Supabase is blocking new tester sessions. Enable new user sign-ups and Anonymous Sign-Ins in the project authentication settings, then reload.', 'signups_disabled')
    if code=='captcha_failed':
        return HostedServiceError('Supabase requires a CAPTCHA for anonymous sign-in, but this beta does not send CAPTCHA tokens. The operator must reconcile the Supabase CAPTCHA setting with this beta setup.', 'captcha_required')
    if status==429:
        return HostedServiceError('Supabase temporarily limited requests or new anonymous sessions. Reuse your existing session and try later. Repeated reloads will not bypass the limit.', 'supabase_rate_limited',429)
    if status==401 and stage=='anonymous_signup':
        return HostedServiceError('Supabase rejected the project API key. In Render, set SUPABASE_PUBLISHABLE_KEY to the publishable key from the same Supabase project as SUPABASE_URL, then redeploy.', 'supabase_key_rejected')
    if code in {'PGRST205','42P01'} or status==404 and stage=='records_read':
        return HostedServiceError('The Supabase health_beta_data table is missing from the Data API. Run the complete updated supabase/beta.sql in the project matching SUPABASE_URL and make sure the public schema is exposed.', 'supabase_table_missing')
    if code=='PGRST202' or status==404 and stage in {'records_save','ai_budget'}:
        return HostedServiceError('A required Supabase beta function is missing from the Data API. Run the complete updated supabase/beta.sql in the project matching SUPABASE_URL, then retry.', 'supabase_function_missing')
    if status==404 and stage=='week_delete':
        return HostedServiceError('This weekly plan no longer exists. Reload the page.', 'week_not_found',400)
    if code=='42501' or status==403 and stage in {'records_read','records_save','week_delete','image_storage'}:
        return HostedServiceError('Supabase denied access to this tester workspace. Run the complete supabase/beta.sql to install the authenticated-user grants and owner policies. Do not make tables or the image bucket public.', 'supabase_access_denied')
    if code=='PGRST106':
        return HostedServiceError('Supabase is not exposing the required public schema. Enable the Data API for public in this project and rerun supabase/beta.sql.', 'supabase_schema_not_exposed')
    if status in (401,403):
        return HostedServiceError('Supabase could not verify this anonymous session. Check that the project URL and publishable key belong to the same project. Keep SESSION_SECRET unchanged and preserve browser cookies.', 'supabase_session_rejected')
    if status==409:
        return HostedServiceError('Data changed in another deployment. Reload and retry; the newer saved data was kept.', 'supabase_revision_conflict',409)
    return HostedServiceError('Supabase failed during '+stage.replace('_',' ')+' (HTTP '+str(status)+'). Check the matching safe error in Render Logs and the Supabase project status. No successful save was acknowledged.', 'supabase_request_failed')


def enabled():
    return os.environ.get('APP_MODE') == 'hosted'


def current():
    value = CURRENT.get()
    if enabled() and value is None:
        raise ValueError('An anonymous tester session is required. Reload the app.')
    return value


def owner():
    return current().uid if enabled() else None


def validate_environment():
    required = ('SUPABASE_URL', 'SUPABASE_PUBLISHABLE_KEY', 'SESSION_SECRET', 'PUBLIC_ORIGIN')
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError('Missing hosted environment variables: ' + ', '.join(missing))
    if len(os.environ['SESSION_SECRET']) < 32:
        raise ValueError('SESSION_SECRET must contain at least 32 random characters.')
    for key in ('SUPABASE_URL', 'PUBLIC_ORIGIN'):
        url = urllib.parse.urlsplit(os.environ[key])
        if url.scheme != 'https' or not url.hostname or url.path not in ('', '/') or url.query or url.fragment or url.username:
            raise ValueError(key + ' must be an HTTPS origin without a path.')
    if not os.environ['SUPABASE_PUBLISHABLE_KEY'].startswith(('sb_publishable_', 'eyJ')):
        raise ValueError('Use the Supabase publishable key or legacy anon key, never a secret/service-role key.')
    key = os.environ['SUPABASE_PUBLISHABLE_KEY']
    if key.startswith('eyJ'):
        try:
            payload = json.loads(base64.urlsafe_b64decode(key.split('.')[1] + '==='))
            if payload.get('role') != 'anon':
                raise ValueError()
        except Exception:
            raise ValueError('The legacy Supabase key must have the anon role, not service_role.') from None


def origin_allowed(handler):
    origin = os.environ.get('PUBLIC_ORIGIN', '').rstrip('/')
    host = urllib.parse.urlsplit(origin).netloc
    return handler.headers.get('Host', '').lower() == host.lower() and handler.headers.get('Origin', origin) == origin


def remote(path, data=None, *, token=None, method=None, raw=False):
    started=time.monotonic()
    try:return _remote_request(path,data,token=token,method=method,raw=raw)
    finally:
        stats=TIMINGS.get()
        if stats is not None:
            name=operation(path)
            stats[name]=stats.get(name,0)+(time.monotonic()-started)*1000
            stats[name+'_calls']=stats.get(name+'_calls',0)+1


def _remote_request(path, data=None, *, token=None, method=None, raw=False):
    headers = {'apikey': os.environ['SUPABASE_PUBLISHABLE_KEY'], 'Content-Type':'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if raw:
        body = data
        headers['Content-Type'] = 'image/png'
        headers['x-upsert'] = 'true'
    else:
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if data is not None else None
    from . import supabase_http
    try:
        status,result=supabase_http.request(os.environ['SUPABASE_URL'].rstrip('/')+path,body,headers,method,MAX_DOCUMENT,TIMINGS.get())
        if status>=400:
            try:payload=json.loads(result[:16384])
            except ValueError:payload={}
            raise provider_error(path,status,payload)
        if not 200<=status<300:raise HostedServiceError('Supabase returned an unexpected redirect. Check SUPABASE_URL is the project API origin.','supabase_redirect')
        if len(result)>MAX_DOCUMENT:raise ValueError('Hosted data exceeds the beta storage limit. Export a backup and reduce saved data.')
        return result if raw else json.loads(result or b'null')
    except (OSError, TimeoutError, http.client.HTTPException, json.JSONDecodeError):
        LOG.warning('Supabase connection or response failed: operation=%s',operation(path))
        raise HostedServiceError('Could not reach Supabase during '+operation(path).replace('_',' ')+'. Check that the project is active and SUPABASE_URL is its API origin (https://PROJECT_REF.supabase.co), then retry. Keep browser cookies.', 'supabase_unreachable') from None


class Session:
    def __init__(self, identity):
        self.uid = str(uuid.UUID(identity['uid']))
        self.identity = identity
        self.path = Path(tempfile.mkdtemp(prefix='health-tester-'))
        self.lock = threading.RLock()
        self.epoch = 0
        self.revision = 0
        self.document = None
        self.document_hash = None
        self.image_names = set()
        self.image_sizes = {}
        self.cache_valid = False
        self.cache_generation = 0
        self.snapshot_generation = 0
        self.photo_locks = {}
        self.progress = {'phase':'idle', 'started':None}

    @property
    def token(self):
        return self.identity['access_token']


def sign(value):
    return hmac.new(os.environ['SESSION_SECRET'].encode(), value.encode(), hashlib.sha256).hexdigest()


def csrf():
    return sign('csrf:' + owner())


def cookie(identity):
    payload = base64.urlsafe_b64encode(json.dumps(identity, separators=(',', ':')).encode()).decode().rstrip('=')
    value = payload + '.' + sign(payload)
    return '__Host-health_session=' + value + '; Path=/; Secure; HttpOnly; SameSite=Lax; Max-Age=31536000'


def identity_for(handler, create=False):
    jar = SimpleCookie()
    try:
        jar.load(handler.headers.get('Cookie', ''))
        saved = jar.get('__Host-health_session')
        if saved:
            payload, signature = saved.value.rsplit('.', 1)
            if not hmac.compare_digest(sign(payload), signature):
                raise ValueError()
            identity = json.loads(base64.urlsafe_b64decode(payload + '==='))
            uuid.UUID(identity['uid'])
        else:
            identity = None
    except Exception:
        raise HostedServiceError('The tester session cookie is invalid. Keep SESSION_SECRET identical to its previous deployment value and reload. Preserve browser cookies to avoid losing access to an existing workspace.', 'session_cookie_invalid') from None
    if identity and identity['expires_at'] <= time.time() + 90:
        result = remote('/auth/v1/token?grant_type=refresh_token', {'refresh_token':identity['refresh_token']})
        if result['user']['id'] != identity['uid']:
            raise ValueError('Anonymous session identity changed. Reload the app.')
        identity = identity_from_auth(result)
        handler.session_cookie = cookie(identity)
    if identity is None:
        if not create:
            raise ValueError('Open the app first to create an anonymous tester session.')
        # A small extra signup throttle, in addition to Supabase Auth's limits.
        address = handler.client_address[0]
        with SESSION_LOCK:
            now = time.monotonic()
            recent = [t for t in SIGNUPS.get(address, []) if t > now - 3600]
            if len(recent) >= 20:
                raise HostedServiceError('Too many new tester sessions were attempted from this connection. Try again after an hour. Check that Supabase Anonymous Sign-Ins are enabled before retrying.', 'signup_rate_limited',429)
            SIGNUPS[address] = recent + [now]
        identity = identity_from_auth(remote('/auth/v1/signup', {}))
        handler.session_cookie = cookie(identity)
    with SESSION_LOCK:
        session = SESSIONS.get(identity['uid'])
        if session is None:
            if len(SESSIONS) >= 100:
                raise ValueError('The public beta is at capacity. Please try again later.')
            session = SESSIONS[identity['uid']] = Session(identity)
        session.identity = identity
    return session


def identity_from_auth(result):
    return {'uid':str(uuid.UUID(result['user']['id'])), 'access_token':result['access_token'],
            'refresh_token':result['refresh_token'], 'expires_at':result.get('expires_at', time.time() + result.get('expires_in', 3600))}


def config_path():
    return current().path / 'config.local.json'


def fingerprint(document):
    return hashlib.sha256(json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def invalidate(session):
    session.cache_generation+=1
    session.cache_valid=False


def ensure_photo(name):
    """Fetch only the requested private image; never hydrate the workspace here."""
    from . import dish_photos
    session=current()
    if not IMAGE.fullmatch(name):return None
    path=session.path/'photos'/name
    with SESSION_LOCK:lock=session.photo_locks.setdefault(name,threading.Lock())
    with lock:
        if path.is_file():return path
        if session.document_hash is not None and name not in session.image_names:return None
        raw=remote('/storage/v1/object/authenticated/health-diet-images/'+session.uid+'/'+name,token=session.token,raw=True)
        if hashlib.sha256(raw).hexdigest()+'.png'!=name:raise ValueError('A saved dish image could not be verified. Please retry.')
        dish_photos.validate_png(raw)
        if session.document_hash is not None and name not in session.image_names:return None
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        session.image_sizes[name]=len(raw)
        return path


def ensure_all_photos():
    if enabled():
        for name in sorted(current().image_names):ensure_photo(name)


def replace_photo_manifest(names):
    if enabled():
        current().image_names=set(names)
        current().image_sizes={name:len(raw) for name,raw in names.items()}


def load(session):
    from . import store, dish_photos
    generation=session.cache_generation
    rows = remote('/rest/v1/health_beta_data?select=revision,document&user_id=eq.' + session.uid, token=session.token)
    document = rows[0]['document'] if rows else None
    session.revision = rows[0]['revision'] if rows else 0
    session.document = document
    session.document_hash=fingerprint(document) if document else None
    # Explicit state reloads and invalid caches reconstruct from the durable
    # source. Cached writes still have to pass Supabase's revision check.
    with store.db() as connection:
        for table in TABLES:
            connection.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            connection.execute(f'DELETE FROM {table}')
            if document:
                connection.executemany(f'INSERT INTO {table} VALUES (?,?)', [(r['id'], json.dumps(r['body'], ensure_ascii=False)) for r in document['tables'][table]])
    config = session.path / 'config.local.json'
    if document and document.get('config') is not None:
        config.write_text(json.dumps(document['config']), encoding='utf-8')
    elif config.exists():
        config.unlink()
    photos = session.path / 'photos'
    photos.mkdir(exist_ok=True)
    names = document.get('images', []) if document else []
    if any(not IMAGE.fullmatch(name) for name in names):
        raise ValueError('Invalid hosted image manifest. Restore a valid backup.')
    for path in photos.glob('*.png'):
        if path.name not in names:
            path.unlink()
    session.image_names=set(names)
    session.image_sizes={name:size for name,size in (document or {}).get('image_sizes',{}).items() if name in session.image_names and type(size) is int and 0<=size<=4_000_000}
    if not document:
        store.initialize()
    session.snapshot_generation=generation
    session.cache_valid=generation==session.cache_generation


def persist():
    if not enabled():
        return
    from . import store
    session = current()
    with store.db() as connection:
        tables = {table:[{'id':row[0], 'body':json.loads(row[1])} for row in connection.execute(f'SELECT id,body FROM {table} ORDER BY rowid')] for table in TABLES}
    config = config_path()
    files = sorted((session.path / 'photos').glob('*.png'))
    if any(path.name not in session.image_names for path in files):
        for name in session.image_names-session.image_sizes.keys():ensure_photo(name)
    sizes={**session.image_sizes,**{p.name:p.stat().st_size for p in files}}
    document = {'tables':tables, 'config':json.loads(config.read_text(encoding='utf-8')) if config.exists() else None, 'images':sorted(session.image_names|{p.name for p in files})}
    if sizes:document['image_sizes']={name:size for name,size in sizes.items() if name in document['images']}
    digest=fingerprint(document)
    if digest == session.document_hash:
        return
    if len(json.dumps(document, ensure_ascii=False).encode()) > MAX_DOCUMENT or sum(sizes.get(name,0) for name in document['images']) > MAX_PHOTOS:
        raise ValueError('Beta storage limit reached (12 MB records, 32 MB photos per tester). Export a backup before reducing data.')
    known = session.image_names
    for path in files:
        if path.name not in known:
            remote('/storage/v1/object/health-diet-images/' + session.uid + '/' + path.name, path.read_bytes(), token=session.token, method='POST', raw=True)
    result = remote('/rest/v1/rpc/health_beta_save', {'expected_revision':session.revision, 'new_document':document}, token=session.token)
    session.revision = result
    session.document_hash=digest
    session.image_names=set(document['images'])
    session.image_sizes=document.get('image_sizes',{})
    session.cache_valid=session.snapshot_generation==session.cache_generation
    session.document=None


@contextmanager
def write_scope():
    if not enabled():
        yield
        return
    session = current()
    with session.lock:
        load(session)
        try:yield
        except Exception:
            invalidate(session)
            raise
        finally:session.document=None


def reserve_ai(audio=False):
    if enabled():
        allowed = remote('/rest/v1/rpc/health_beta_take_ai', {'audio_request':audio}, token=current().token)
        if not allowed:
            raise ValueError('The public beta AI usage limit has been reached. Try later. Manual editing, Simple Rotation and Randomize are still available.')


def delete_week(week):
    from . import weekly, plan_jobs
    weekly.week_date(week)
    def check_job():
        job=plan_jobs.active()
        if job and job['week_start']==week:
            raise ValueError('Cancel AI generation for this week before changing its plan.')
    with plan_jobs.LOCK:
        check_job()
        try:
            result=remote('/rest/v1/rpc/health_beta_delete_week',{'target_week':week},token=current().token)
            invalidate(current())
            return result
        except HostedServiceError as exc:
            if exc.code!='supabase_function_missing':raise
    # Backwards-compatible during rollout: a project that has not run the new
    # SQL keeps the old, slower durable path. It never reports a local-only save.
    with write_scope(),plan_jobs.LOCK:
        check_job()
        weekly.history()
        result=weekly.delete(week)
        persist()
        return result


def send_timed_response(response, output, stats):
    """Add timings after durable commit; callback headers were buffered earlier."""
    response.seek(0)
    lines=[]
    while True:
        line=response.readline(65536)
        if line in (b'\r\n',b'\n',b''):break
        lines.append(line)
    timings=', '.join(name+';dur='+format(value,'.1f') for name,value in stats.items() if not name.endswith('_calls'))
    output.write(b''.join(lines)+('Server-Timing: '+timings+'\r\n\r\n').encode('ascii'))
    while block:=response.read(65536):output.write(block)


def serve(handler, callback):
    """Buffer the response until the user's durable commit succeeds."""
    route = urllib.parse.urlsplit(handler.path).path
    if handler.command=='GET' and route == '/healthz':
        return handler.reply({'ok':True, 'mode':'hosted'})
    if not origin_allowed(handler):
        return handler.reply({'error':'Only same-origin access is allowed.'}, 403)
    if handler.command == 'GET' and route in ('/', '/index.html', '/app.js', '/style.css'):
        return callback()
    original = handler.wfile
    fast_delete=handler.command=='POST' and route=='/api/plan/delete'
    fast_read=handler.command=='GET' and (route in {'/api/ai-progress','/api/status','/api/plan/job'} or route.startswith('/media/'))
    isolated=fast_delete or fast_read
    stats={}
    timing_scope=TIMINGS.set(stats)
    started=time.monotonic()
    cpu_started=time.thread_time()
    session=None
    try:
        stage=time.monotonic()
        session = identity_for(handler, create=handler.command == 'GET' and route == '/api/state')
        stats['identity']=(time.monotonic()-stage)*1000
        scope = CURRENT.set(session)
        try:
            stage=time.monotonic()
            with (nullcontext() if isolated else session.lock), tempfile.SpooledTemporaryFile(max_size=1024*1024) as response:
                stats['lock_wait']=(time.monotonic()-stage)*1000
                stage=time.monotonic()
                if not isolated:
                    if route in {'/api/state','/api/backup/export','/api/export'} or not session.cache_valid:load(session)
                stats['load']=(time.monotonic()-stage)*1000
                handler.wfile = response
                stage=time.monotonic()
                callback()
                stats['action']=(time.monotonic()-stage)*1000
                stage=time.monotonic()
                if not isolated and getattr(handler, 'response_code', 500) < 400:
                    persist()
                elif not isolated and handler.command=='POST':invalidate(session)
                stats['persist']=(time.monotonic()-stage)*1000
                stats['total']=(time.monotonic()-started)*1000
                stats['process_age']=(time.monotonic()-PROCESS_STARTED)*1000
                stats['thread_cpu']=(time.thread_time()-cpu_started)*1000
                handler.wfile = original
                send_timed_response(response,original,stats)
                if not fast_delete:session.document=None
        finally:
            CURRENT.reset(scope)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        if session is not None:invalidate(session)
        handler.wfile = original
        handler._headers_buffer = []
        if isinstance(exc,HostedServiceError):
            handler.reply({'error':str(exc),'code':exc.code},exc.status)
        else:
            # No raw exception text: malformed records can contain private data.
            LOG.warning('Hosted workspace failed: exception_type=%s',type(exc).__name__)
            handler.reply({'error':'The hosted workspace could not be loaded or saved. Check Render Logs for the safe error category; no successful save was acknowledged.','code':'hosted_workspace_failed'},503)
    finally:
        handler.wfile = original
        if route in {'/api/plan/delete','/api/plan/add','/api/recipes'}:
            LOG.info('Workspace timing route=%s: %s',route,json.dumps({name:round(value,1) for name,value in stats.items()},sort_keys=True))
        TIMINGS.reset(timing_scope)
