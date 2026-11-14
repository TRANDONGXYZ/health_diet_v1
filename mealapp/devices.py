"""Browser identities, approval-based pairing and same-origin live notifications.

Database RLS is the authority. No privileged key or browser session is shared.
Realtime carries revision hints, never recipes or private image bytes.
"""
import base64
import io
import json
import os
import secrets
import select
import socket
import time
import urllib.parse
import uuid
from . import hosted


def enabled():
    return hosted.enabled() and os.environ.get('DEVICE_PAIRING_ENABLED') == '1'


def label(agent):
    platform = 'iPhone' if 'iPhone' in agent else 'iPad' if 'iPad' in agent else 'Android' if 'Android' in agent else 'Windows' if 'Windows' in agent else 'Mac' if 'Macintosh' in agent else 'computer'
    browser = 'Edge' if 'Edg/' in agent else 'Firefox' if any(x in agent for x in ('Firefox/', 'FxiOS/')) else 'Chrome' if any(x in agent for x in ('Chrome/', 'CriOS/')) else 'Safari' if 'Safari/' in agent else 'Browser'
    return browser + ' on ' + platform


def rpc(name, data=None):
    try:return hosted.remote('/rest/v1/rpc/health_beta_'+name, data or {}, token=hosted.current().token)
    except hosted.HostedServiceError as exc:
        if exc.status==400 and name=='pair':
            raise hosted.HostedServiceError('Pairing was not accepted. The code may have expired, been used, or been canceled. Create a fresh QR code and compare both confirmation codes.','pair_denied',400) from None
        if exc.code=='supabase_function_missing':
            raise hosted.HostedServiceError('Device setup is incomplete. The operator must run supabase/devices.sql after beta.sql and offline.sql.','devices_migration_missing') from None
        raise


def context(handler, allow_disconnected=False):
    session = hosted.current()
    result = rpc('device_context', {'device_label':label(handler.headers.get('User-Agent', ''))})
    workspace, epoch = str(uuid.UUID(result['workspace_id'])), str(uuid.UUID(result['device_epoch']))
    if (session.workspace_id, session.device_epoch) != (workspace, epoch):
        hosted.invalidate(session)
        session.document_hash = None
        session.image_names = set()
        # Cached files must never cross workspace boundaries.
        session.path = hosted.Path(hosted.tempfile.mkdtemp(prefix='health-tester-'))
    session.workspace_id, session.device_epoch, session.device_context = workspace, epoch, result
    if not allow_disconnected:
        if not result['active']:
            raise hosted.HostedServiceError('This device has been disconnected. Your downloaded data and pending edits are kept locally. Rejoin using a fresh QR code.', 'device_disconnected', 403)
        if result.get('pending_pair'):
            raise hosted.HostedServiceError('Finish or cancel device pairing before syncing.', 'pairing_pending', 409)
        expected = handler.headers.get('X-Workspace-ID')
        expected_epoch = handler.headers.get('X-Device-Epoch')
        if (expected and expected != workspace) or (expected_epoch and expected_epoch != epoch) or (result['joined'] and handler.command == 'POST' and (not expected or not expected_epoch)):
            raise hosted.HostedServiceError('This browser is using an older workspace. Its pending edits were kept; reopen pairing before syncing.', 'workspace_changed', 409)
    return result


def profile():
    if not enabled(): return {'pairing':False}
    s = hosted.current()
    return dict(s.device_context, pairing=True)


def check_pinned(handler):
    state=context(handler)
    if hosted.REQUEST_WORKSPACE.get() != (state['workspace_id'],state['device_epoch']):
        raise hosted.HostedServiceError('The workspace changed during this request. Reload before retrying.','workspace_changed',409)


def pair_action(handler, action, ticket, secret):
    # Legacy read-cache/bootstrap work in this same process must finish before
    # switching its browser's workspace. Delta writes also check epochs in SQL.
    with hosted.current().lock:
        result=rpc('pair', {'action':action,'ticket':ticket,'secret':secret})
        if action=='activate':
            context(handler, True)
            from .offline_sync import profile as device_profile
            result['profile']=device_profile(handler)
        return result


def post(handler, data):
    if not enabled(): raise ValueError('Device pairing requires the hosted HTTPS app with device pairing enabled.')
    route = urllib.parse.urlsplit(handler.path).path
    if route == '/api/devices/list': return {'devices':rpc('device_list')}
    if route == '/api/devices/disconnect':
        rpc('device_revoke', {'target':str(uuid.UUID(data['id']))})
        return {'ok':True}
    if route != '/api/devices/pair': raise ValueError('Unknown device action.')
    action = data.get('action')
    if action not in ('create','request','status','approve','reject','stage','activate','finish','cancel'): raise ValueError('Unknown pairing action.')
    ticket = str(uuid.UUID(data['ticket'])) if data.get('ticket') else None
    secret = secrets.token_hex(32) if action == 'create' else data.get('secret') if action == 'request' else None
    if secret is not None and (not isinstance(secret,str) or len(secret)!=64 or any(c not in 'abcdef0123456789' for c in secret)): raise ValueError('Invalid pairing code.')
    result = pair_action(handler, action, ticket, secret)
    if action == 'create':
        import qrcode
        import qrcode.image.svg
        url = os.environ['PUBLIC_ORIGIN'].rstrip('/') + '/#pair=' + secret
        output = io.BytesIO()
        qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=8).save(output)
        result.update(url=url, qr='data:image/svg+xml;base64,'+base64.b64encode(output.getvalue()).decode())
    return result


def staged_photo(handler):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    ticket, name = query.get('ticket',[''])[0], query.get('name',[''])[0]
    if not hosted.IMAGE.fullmatch(name): raise ValueError('Invalid photo.')
    state = rpc('pair', {'action':'status','ticket':str(uuid.UUID(ticket))})
    if state['state'] != 'approved': raise ValueError('Pairing approval required.')
    # Storage's separate read-only approval policy validates the bound joiner.
    raw = hosted.remote('/storage/v1/object/authenticated/health-diet-images/'+state['workspace_id']+'/'+name, token=hosted.current().token,raw=True)
    handler.send_response(200); handler.headers_common('image/png'); handler.send_header('Content-Length',str(len(raw))); handler.end_headers(); handler.wfile.write(raw)


def realtime(session, workspace):
    """Yield only after Postgres Changes subscription is acknowledged (no polling)."""
    import websocket
    address = os.environ['SUPABASE_URL'].rstrip('/').replace('https://','wss://') + '/realtime/v1/websocket?vsn=1.0.0&apikey=' + urllib.parse.quote(os.environ['SUPABASE_PUBLISHABLE_KEY'])
    ws = websocket.create_connection(address, timeout=10, suppress_origin=True)
    topic = 'realtime:health-'+secrets.token_hex(8)
    changes=[{'event':'*','schema':'public','table':'health_beta_signals','filter':'device_id=eq.'+session.uid}]
    if workspace:changes.append({'event':'*','schema':'public','table':'health_beta_events','filter':'workspace_id=eq.'+workspace})
    config = {'broadcast':{'ack':False,'self':False},'presence':{'enabled':False},'postgres_changes':changes}
    ws.send(json.dumps({'topic':topic,'event':'phx_join','payload':{'config':config,'access_token':session.token},'ref':'1'}))
    joined = False; started = time.monotonic(); heartbeat = started
    try:
        ws.settimeout(1)
        while time.time() < session.identity['expires_at']-60:
            now = time.monotonic()
            if not joined and now-started>15: raise OSError('Realtime subscription timed out')
            if now-heartbeat>=15:
                ws.send(json.dumps({'topic':'phoenix','event':'heartbeat','payload':{},'ref':str(now)}));heartbeat=now
            try: message = json.loads(ws.recv())
            except websocket.WebSocketTimeoutException:
                yield 'keepalive', {};continue
            if message.get('event')=='phx_reply' and message.get('ref')=='1':
                payload=message.get('payload',{})
                if payload.get('status')!='ok' or len(payload.get('response',{}).get('postgres_changes',[]))!=len(changes): raise OSError('Realtime subscription rejected')
                joined=True; yield 'ready', {}
            elif message.get('event')=='postgres_changes' and joined:
                row=message.get('payload',{}).get('data',{})
                if row.get('table')=='health_beta_signals':yield 'devices', {}
                elif row.get('table')=='health_beta_events':yield 'change', {'cursor':row.get('record',{}).get('revision',0)}
            elif message.get('event') in ('phx_error','phx_close') or message.get('event')=='system' and message.get('payload',{}).get('status')=='error':
                raise OSError('Realtime channel unavailable')
    finally:
        ws.close()


def stream(handler):
    if not enabled(): return handler.reply({'error':'Live sync is unavailable in local mode.'},400)
    session=hosted.current(); workspace,epoch=session.workspace_id,session.device_epoch
    ticket=urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query).get('pair',[''])[0]
    if ticket:
        state=rpc('pair',{'action':'status','ticket':str(uuid.UUID(ticket))})
        if state['state'] not in ('issued','requested','approved'):return handler.reply({'error':'Pairing is no longer available.'},400)
    subscribed_workspace=workspace if session.device_context.get('active') and not session.device_context.get('pending_pair') else None
    handler.send_response(200);handler.headers_common('text/event-stream');handler.send_header('X-Accel-Buffering','no');handler.end_headers()
    handler.close_connection=True
    def emit(event,data):
        handler.wfile.write(('event: '+event+'\ndata: '+json.dumps(data,separators=(',',':'))+'\n\n').encode());handler.wfile.flush()
    last_heartbeat=0
    try:
        for event,data in realtime(session,subscribed_workspace):
            # Detect a closed/hidden browser without doing periodic database reads.
            if select.select([handler.connection],[],[],0)[0] and not handler.connection.recv(1,socket.MSG_PEEK):break
            if event in ('ready','devices'):
                state=context(handler,True)
                if not state['active'] and (not ticket or subscribed_workspace):
                    emit('revoked',{});break
                if not ticket and (state['workspace_id']!=workspace or state['device_epoch']!=epoch or state.get('pending_pair')):
                    emit('workspace',{});break
            if event=='keepalive':
                if time.monotonic()-last_heartbeat<15:continue
                handler.wfile.write(b': keepalive\n\n');handler.wfile.flush();last_heartbeat=time.monotonic()
            else: emit(event,data)
    except Exception:
        # Never return provider URLs, tokens or database errors in the stream.
        try:emit('unavailable',{})
        except OSError:pass
