"""Hosted timing fixture: real dispatcher, explicit delayed Supabase double."""
import json
import logging
import os
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import app
from mealapp import hosted
from test_hosted import SupabaseDouble

if not os.environ.get('MEAL_APP_DATA'):raise SystemExit('Disposable test folder required')
root=Path(os.environ['MEAL_APP_DATA'])
port=os.environ['PORT']
hosted.origin_allowed=lambda handler:handler.headers.get('Host')=='localhost:'+port and handler.headers.get('Origin','http://localhost:'+port)=='http://localhost:'+port
provider=SupabaseDouble()
def remote(path,*args,**kwargs):
    started=time.monotonic()
    try:
        time.sleep(float(os.environ.get('TEST_STORAGE_DELAY','.35')))
        return provider.call(path,*args,**kwargs)
    finally:
        stats=hosted.TIMINGS.get()
        if stats is not None:
            name=hosted.operation(path)
            stats[name]=stats.get(name,0)+(time.monotonic()-started)*1000
            stats[name+'_calls']=stats.get(name+'_calls',0)+1
hosted.remote=remote
original_init=hosted.Session.__init__
def initialize(self,identity):
    original_init(self,identity);self.path.rmdir();self.path=root/self.uid;self.path.mkdir()
hosted.Session.__init__=initialize
logging.basicConfig(level=logging.INFO,format='%(message)s')
app.main()
