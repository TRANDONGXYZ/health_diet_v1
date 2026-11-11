"""Hosted HTTP behavior against explicit Supabase/Groq doubles (no live keys)."""
import base64
import copy
import json
import os
from pathlib import Path
import secrets
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from unittest.mock import patch

import app
from mealapp import ai, backups, groq_ai, hosted, plan_jobs, store
from test_weekly import png


class SupabaseDouble:
    def __init__(self):
        self.rows, self.images, self.tokens = {}, {}, {}
        self.allowed = True
        self.fail_save = False

    def call(self, path, data=None, *, token=None, method=None, raw=False):
        if path == '/auth/v1/signup':
            uid = str(uuid.uuid4()); access = secrets.token_hex(32)
            self.tokens[access] = uid
            return {'user':{'id':uid}, 'access_token':access, 'refresh_token':'refresh', 'expires_at':time.time()+3600}
        uid = self.tokens.get(token)
        if not uid:raise ValueError('Unauthenticated')
        if path.startswith('/rest/v1/health_beta_data?'):
            self.assert_owner(path.split('eq.')[1], uid)
            return [copy.deepcopy(self.rows[uid])] if uid in self.rows else []
        if path == '/rest/v1/rpc/health_beta_save':
            if self.fail_save:raise ValueError('Storage is offline')
            revision = self.rows.get(uid, {}).get('revision', 0)
            if revision != data['expected_revision']:raise ValueError('Revision conflict')
            self.rows[uid] = {'revision':revision+1, 'document':copy.deepcopy(data['new_document'])}
            return revision+1
        if path == '/rest/v1/rpc/health_beta_take_ai':return self.allowed
        if path.startswith('/storage/v1/object/'):
            key = path.split('health-diet-images/')[1]
            self.assert_owner(key.split('/')[0], uid)
            if method == 'POST':self.images[key] = data;return b'{}'
            return self.images[key]
        raise AssertionError(path)

    def assert_owner(self, actual, uid):
        if actual != uid:raise ValueError('RLS denied')


class HostedHTTPTests(unittest.TestCase):
    def setUp(self):
        self.environment=patch.dict(os.environ,{'APP_MODE':'hosted','PUBLIC_ORIGIN':'https://beta.example',
            'SUPABASE_URL':'https://project.supabase.co','SUPABASE_PUBLISHABLE_KEY':'sb_publishable_test',
            'SESSION_SECRET':'s'*64,'GROQ_API_KEY':'operator-groq-secret','USDA_API_KEY':'operator-usda-secret'})
        self.environment.start();self.addCleanup(self.environment.stop)
        self.upstream=SupabaseDouble()
        self.remote=patch.object(hosted,'remote',side_effect=self.upstream.call);self.remote.start();self.addCleanup(self.remote.stop)
        self.original_sessions=hosted.SESSIONS.copy();hosted.SESSIONS.clear();hosted.SIGNUPS.clear()
        self.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        self.sessions=[]

    def tearDown(self):
        for job in list(plan_jobs.JOBS.values()):
            job['cancel'].cancel()
            if job.get('thread'):job['thread'].join(2)
        plan_jobs.JOBS.clear()
        self.server.shutdown();self.server.server_close();self.thread.join()
        # Only disposable folders created by Session are removed.
        import shutil
        for session in self.sessions+list(hosted.SESSIONS.values()):
            if session.path.exists():
                self.assertTrue(session.path.name.startswith('health-tester-'))
                shutil.rmtree(session.path)
        hosted.SESSIONS.clear();hosted.SESSIONS.update(self.original_sessions)

    def http(self, path, data=None, *, cookie=None, csrf=None, raw=False, extra=None):
        headers={'Host':'beta.example','Origin':'https://beta.example'}
        if cookie:headers['Cookie']=cookie
        if csrf:headers['X-App-Token']=csrf
        if extra:headers.update(extra)
        if data is not None:
            headers['Content-Type']='application/zip' if raw else 'application/json'
            body=data if raw else json.dumps(data).encode()
        else:body=None
        request=urllib.request.Request(self.url+path,data=body,headers=headers)
        try:response=urllib.request.urlopen(request,timeout=12)
        except urllib.error.HTTPError as error:response=error
        with response:
            result=response.read()
            if response.headers.get_content_type()=='application/json':result=json.loads(result)
            return response.status,result,response.headers

    def create_tester(self):
        status,state,headers=self.http('/api/state');self.assertEqual(status,200,state)
        self.assertIn('Secure; HttpOnly; SameSite=Lax',headers['Set-Cookie'])
        return headers['Set-Cookie'].split(';')[0],state['token']

    def test_isolated_durable_recipes_plans_images_settings_and_zip_after_restart(self):
        a,ta=self.create_tester();b,tb=self.create_tester();self.assertNotEqual(ta,tb)
        recipe={'id':'same-id','name':'Tester A soup','servings':2,'ingredients':[{'name':'tofu','grams':200}]}
        code,photo,_=self.http('/api/dish-photo',{'filename':'dish.png','image':base64.b64encode(png()).decode()},cookie=a,csrf=ta)
        self.assertEqual(code,200,photo);recipe['photo_url']=photo['photo_url']
        self.assertEqual(self.http('/api/recipes',recipe,cookie=a,csrf=ta)[0],200)
        self.http('/api/plan/generate',{'method':'rotation'},cookie=a,csrf=ta)
        self.http('/api/plan/expansion',{'week_start':'2026-11-02','expanded':False},cookie=a,csrf=ta)
        self.assertEqual(self.http('/api/recipes',dict(recipe,name='Tester B soup',photo_url=''),cookie=b,csrf=tb)[0],200)
        self.assertEqual(self.http('/api/recipes',recipe,cookie=b,csrf=ta)[0],403)
        self.assertEqual(self.http(photo['photo_url'],cookie=b)[0],404)
        code,zipdata,_=self.http('/api/backup/export',cookie=a);self.assertEqual(code,200)
        manifest,_=backups.read_zip(zipdata)
        self.assertNotIn('operator-groq-secret',json.dumps(manifest))
        self.assertNotIn('operator-usda-secret',json.dumps(manifest))
        self.assertEqual(manifest['settings']['usda_api_key'],'DEMO_KEY')
        self.assertEqual(manifest['settings']['text_model'],groq_ai.TEXT_MODEL)
        self.sessions+=list(hosted.SESSIONS.values());hosted.SESSIONS.clear()
        # A new process has no session objects, SQLite files or photo cache. The
        # same browser cookie must reconstruct all data from the remote store.
        code,restored,_=self.http('/api/state',cookie=a);self.assertEqual(code,200,restored)
        self.assertEqual(restored['recipes'][0]['name'],'Tester A soup');self.assertTrue(restored['plans'])
        self.assertEqual(restored['week_expansion'],{'2026-11-02':False})
        self.assertEqual(self.http(photo['photo_url'],cookie=a)[1],png())
        self.assertEqual(self.http('/api/state',cookie=b)[1]['recipes'][0]['name'],'Tester B soup')
        self.http('/api/recipes/delete',{'id':'same-id'},cookie=a,csrf=ta)
        self.assertEqual(self.http('/api/backup/import',zipdata,cookie=a,csrf=ta,raw=True)[0],200)
        self.assertEqual(self.http('/api/state',cookie=a)[1]['recipes'][0]['name'],'Tester A soup')
        before=self.http('/api/state',cookie=a)[1]
        self.assertEqual(self.http('/api/backup/import',b'bad zip',cookie=a,csrf=ta,raw=True)[0],400)
        self.assertEqual(self.http('/api/state',cookie=a)[1],before)
        self.assertEqual(self.http(photo['photo_url'],cookie=b)[0],404)

    def test_no_acknowledged_save_on_storage_failure_and_manual_actions_without_ai(self):
        cookie,token=self.create_tester()
        self.upstream.fail_save=True
        recipe={'id':'dish','name':'Tofu','ingredients':[{'name':'tofu','grams':100}]}
        self.assertEqual(self.http('/api/recipes',recipe,cookie=cookie,csrf=token)[0],503)
        self.upstream.fail_save=False
        self.assertEqual(self.http('/api/state',cookie=cookie)[1]['recipes'],[])
        self.assertEqual(self.http('/api/recipes',recipe,cookie=cookie,csrf=token)[0],200)
        self.upstream.allowed=False
        code,result,_=self.http('/api/import',{'text':'Tofu. Ingredients: 100 g tofu. Fry the tofu.'},cookie=cookie,csrf=token)
        self.assertEqual(code,400);self.assertIn('usage limit',result['error'])
        for method in ('rotation','random'):
            self.assertEqual(self.http('/api/plan/generate',{'method':method},cookie=cookie,csrf=token)[0],200)
        self.assertEqual(self.http('/api/healthz',cookie=cookie)[0],404)
        self.assertEqual(self.http('/healthz')[0],200)

    def test_async_jobs_and_progress_belong_only_to_their_tester(self):
        a,ta=self.create_tester();b,tb=self.create_tester()
        self.http('/api/recipes',{'name':'Tofu','ingredients':[{'name':'tofu','grams':100}]},cookie=a,csrf=ta)
        started=threading.Event();release=threading.Event()
        def ask(task,schema,**kwargs):
            started.set();release.wait(3)
            rows=schema['properties']['days']
            return {'days':[{'recipe_id':'r1','timing':'morning','prep':'Ready','reason':'Saved dish'} for _ in range(rows['minItems'])]}
        with patch.object(ai,'ask',side_effect=ask):
            code,job,_=self.http('/api/plan/start',{},cookie=a,csrf=ta);self.assertEqual(code,200);self.assertTrue(started.wait(1))
            self.assertIsNone(self.http('/api/state',cookie=b)[1]['plan_job'])
            self.assertEqual(self.http('/api/plan/job?id='+job['id'],cookie=b)[0],404)
            self.assertEqual(self.http('/api/plan/cancel',{'id':job['id']},cookie=b,csrf=tb)[0],400)
            release.set();plan_jobs.JOBS[job['id']]['thread'].join(5)
        result=self.http('/api/plan/job?id='+job['id'],cookie=a)[1]
        self.assertEqual(result['status'],'completed',result)
        self.sessions+=list(hosted.SESSIONS.values());hosted.SESSIONS.clear()
        self.assertIsNotNone(self.http('/api/state',cookie=a)[1]['plan'])
        self.assertIsNone(self.http('/api/state',cookie=b)[1]['plan'])


class GroqAdapterTests(unittest.TestCase):
    def test_quota_and_provider_errors_are_clear_without_exposing_keys(self):
        from unittest.mock import MagicMock
        for code in (429,401,503):
            connection=MagicMock()
            response=connection.getresponse.return_value.__enter__.return_value
            response.status=code
            with patch.dict(os.environ,{'GROQ_API_KEY':'private-key'}),patch.object(hosted,'reserve_ai'),patch.object(groq_ai.http.client,'HTTPSConnection',return_value=connection):
                with self.assertRaises(ValueError) as raised:groq_ai.request('/chat/completions',b'{}')
            message=str(raised.exception)
            self.assertNotIn('private-key',message)
            self.assertIn('quota' if code==429 else 'unavailable',message)
            connection.close.assert_called_once()

    def test_hosted_audio_uses_ffmpeg_and_groq_without_local_whisper(self):
        import tempfile
        from types import SimpleNamespace
        from mealapp import speech
        with tempfile.TemporaryDirectory() as folder,patch.object(hosted,'enabled',return_value=True),patch.object(speech,'ensure_assets',side_effect=AssertionError('No local Whisper in hosted mode')),patch('shutil.which',side_effect=lambda value:value),patch.object(speech,'run',return_value=SimpleNamespace(stdout='12.0')) as command,patch.object(groq_ai,'transcribe',return_value='Đậu hũ') as transcribe:
            self.assertEqual(speech.transcribe_file(Path(folder)/'input.mp4',folder),'Đậu hũ')
            self.assertEqual(command.call_args_list[0].args[0][0],'ffmpeg')
            self.assertEqual(command.call_args_list[1].args[0][0],'ffprobe')
            self.assertEqual(transcribe.call_args.args[0].suffix,'.flac')

    def test_text_vision_and_audio_use_requested_models_and_native_language(self):
        result={'choices':[{'finish_reason':'stop','message':{'content':'{"name":"Đậu hũ"}'}}]}
        with patch.object(groq_ai,'request',return_value=result) as request:
            self.assertEqual(groq_ai.chat('Preserve Vietnamese','source',ai.obj({'name':ai.TEXT}))['name'],'Đậu hũ')
            payload=json.loads(request.call_args.args[1]);self.assertEqual(payload['model'],'qwen/qwen3.8-27b')
            self.assertEqual(payload['response_format']['json_schema']['strict'],True)
            groq_ai.chat('Preserve English','source',ai.obj({'name':ai.TEXT}),image='abcd')
            payload=json.loads(request.call_args.args[1]);self.assertEqual(payload['messages'][1]['content'][1]['type'],'image_url')
        import tempfile
        with tempfile.TemporaryDirectory() as folder,patch.object(groq_ai,'request',return_value={'text':'Đậu hũ'}) as request:
            file=Path(folder)/'audio.flac';file.write_bytes(b'audio')
            self.assertEqual(groq_ai.transcribe(file),'Đậu hũ')
            self.assertEqual(request.call_args.args[0],'/audio/transcriptions')
            self.assertIn(b'whisper-large-v3-turbo',request.call_args.args[1]);self.assertNotIn(b'name="language"',request.call_args.args[1])
