"""Hosted server against the explicit local Auth/Storage/Realtime test provider."""
import os
import runpy
import urllib.parse
import websocket
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from mealapp import speech
if not os.environ.get('HEALTH_TEST_PROVIDER'):raise SystemExit('Isolated provider required')
original=websocket.create_connection
def connection(url, **kwargs):
    return original(os.environ['HEALTH_TEST_PROVIDER'].replace('http:','ws:')+urllib.parse.urlsplit(url).path+'?'+urllib.parse.urlsplit(url).query,**kwargs)
websocket.create_connection=connection
speech.transcribe_file=lambda *_:'Fixture transcript: add rice and water, then cook until tender.'
runpy.run_path('tests/offline_hosted_server.py',run_name='__main__')
