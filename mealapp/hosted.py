"""Render beta: anonymous Supabase identity and durable per-tester workspaces.

SQLite and photo files are request-local working copies. Supabase Postgres is
the authority; successful writes are acknowledged only after a revision-checked
commit there. Storage objects are private and use the tester's JWT, never an
administrative key.
"""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
from http.cookies import SimpleCookie
import json
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
SESSIONS = {}
SESSION_LOCK = threading.RLock()
SIGNUPS = {}
TABLES = ('foods', 'recipes', 'state', 'cache', 'plans')
IMAGE = re.compile(r'[a-f0-9]{64}\.png\Z')
MAX_DOCUMENT = 12 * 1024 * 1024
MAX_PHOTOS = 32 * 1024 * 1024


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
    headers = {'apikey': os.environ['SUPABASE_PUBLISHABLE_KEY'], 'Content-Type':'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    if raw:
        body = data
        headers['Content-Type'] = 'image/png'
        headers['x-upsert'] = 'true'
    else:
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if data is not None else None
    request = urllib.request.Request(os.environ['SUPABASE_URL'].rstrip('/') + path, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = response.read(MAX_DOCUMENT + 1)
            if len(result) > MAX_DOCUMENT:
                raise ValueError('Hosted data exceeds the beta storage limit. Export a backup and reduce saved data.')
            return result if raw else json.loads(result or b'null')
    except urllib.error.HTTPError as exc:
        # Do not return provider bodies, bearer tokens or configuration to clients.
        if exc.code == 429:
            raise ValueError('The beta service quota is temporarily exhausted. Please try later. Manual actions remain available when storage is reachable.') from None
        if exc.code in (401, 403):
            raise ValueError('Your anonymous session or Supabase access policy could not be verified. Reload the app; do not clear browser cookies.') from None
        if exc.code == 409:
            raise ValueError('Data changed in another deployment. Reload and retry; the newer saved data was kept.') from None
        raise ValueError('Durable storage is unavailable or not configured. Nothing was acknowledged as saved. Please retry.') from None
    except (OSError, TimeoutError, json.JSONDecodeError):
        raise ValueError('Durable storage is unavailable. Please retry; do not clear browser cookies.') from None


class Session:
    def __init__(self, identity):
        self.uid = str(uuid.UUID(identity['uid']))
        self.identity = identity
        self.path = Path(tempfile.mkdtemp(prefix='health-tester-'))
        self.lock = threading.RLock()
        self.epoch = 0
        self.revision = 0
        self.document = None
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
        raise ValueError('The tester session cookie is invalid. Restore the original cookie or use a separate browser profile for a new session.') from None
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
                raise ValueError('Too many new tester sessions. Reuse your browser session or try again later.')
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


def load(session):
    from . import store, dish_photos
    rows = remote('/rest/v1/health_beta_data?select=revision,document&user_id=eq.' + session.uid, token=session.token)
    document = rows[0]['document'] if rows else None
    session.revision = rows[0]['revision'] if rows else 0
    session.document = document
    # Reconstruct from the durable source on every request, including after a
    # failed local mutation or a successful write by an overlapping deployment.
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
    for name in names:
        path = photos / name
        if not path.exists():
            raw = remote('/storage/v1/object/authenticated/health-diet-images/' + session.uid + '/' + name, token=session.token, raw=True)
            if hashlib.sha256(raw).hexdigest() + '.png' != name:
                raise ValueError('A saved dish image could not be verified. Please retry.')
            dish_photos.validate_png(raw)
            path.write_bytes(raw)
    if not document:
        store.initialize()


def persist():
    if not enabled():
        return
    from . import store
    session = current()
    with store.db() as connection:
        tables = {table:[{'id':row[0], 'body':json.loads(row[1])} for row in connection.execute(f'SELECT id,body FROM {table} ORDER BY rowid')] for table in TABLES}
    config = config_path()
    files = sorted((session.path / 'photos').glob('*.png'))
    document = {'tables':tables, 'config':json.loads(config.read_text(encoding='utf-8')) if config.exists() else None, 'images':[p.name for p in files]}
    if document == session.document:
        return
    if len(json.dumps(document, ensure_ascii=False).encode()) > MAX_DOCUMENT or sum(p.stat().st_size for p in files) > MAX_PHOTOS:
        raise ValueError('Beta storage limit reached (12 MB records, 32 MB photos per tester). Export a backup before reducing data.')
    known = set((session.document or {}).get('images', []))
    for path in files:
        if path.name not in known:
            remote('/storage/v1/object/health-diet-images/' + session.uid + '/' + path.name, path.read_bytes(), token=session.token, method='POST', raw=True)
    result = remote('/rest/v1/rpc/health_beta_save', {'expected_revision':session.revision, 'new_document':document}, token=session.token)
    session.revision = result
    session.document = document


@contextmanager
def write_scope():
    if not enabled():
        yield
        return
    session = current()
    with session.lock:
        load(session)
        try:yield
        finally:session.document=None


def reserve_ai(audio=False):
    if enabled():
        allowed = remote('/rest/v1/rpc/health_beta_take_ai', {'audio_request':audio}, token=current().token)
        if not allowed:
            raise ValueError('The public beta AI usage limit has been reached. Try later. Manual editing, Simple Rotation and Randomize are still available.')


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
    try:
        session = identity_for(handler, create=handler.command == 'GET' and route == '/api/state')
        scope = CURRENT.set(session)
        try:
            with session.lock, tempfile.SpooledTemporaryFile(max_size=1024*1024) as response:
                load(session)
                handler.wfile = response
                callback()
                if getattr(handler, 'response_code', 500) < 400:
                    persist()
                response.seek(0)
                handler.wfile = original
                while block := response.read(65536):
                    original.write(block)
                session.document=None
        finally:
            CURRENT.reset(scope)
    except (ValueError, OSError, KeyError, TypeError):
        handler.wfile = original
        handler._headers_buffer = []
        handler.reply({'error':'The hosted session or durable storage is unavailable. Reload and retry; no successful save was acknowledged.'}, 503)
    finally:
        handler.wfile = original
