"""Real hosted HTTP handler with a loopback PostgreSQL/provider fixture only."""
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import app
from mealapp import hosted, supabase_http

if not os.environ.get('HEALTH_TEST_PROVIDER'): raise SystemExit('Test provider required')


def request(url,body,headers,method,max_bytes,timings=None):
    target=os.environ['HEALTH_TEST_PROVIDER']+urllib.parse.urlsplit(url).path
    if urllib.parse.urlsplit(url).query:target+='?'+urllib.parse.urlsplit(url).query
    try:
        with urllib.request.urlopen(urllib.request.Request(target,data=body,headers=headers,method=method),timeout=10) as response:
            return response.status,response.read(max_bytes+1)
    except urllib.error.HTTPError as exc:
        return exc.code,exc.read()


supabase_http.request=request
app.main()
