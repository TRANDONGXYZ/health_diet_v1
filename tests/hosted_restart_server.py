"""Test-only provider double; startup and HTTP dispatch use the real app entrypoint."""
import base64
import json
import os
from pathlib import Path
import sys
import threading
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import app
from mealapp import hosted
from test_hosted import SupabaseDouble

root=Path(os.environ['HEALTH_BETA_TEST_DIR']).resolve()
if not root.name.startswith('health-beta-restart-'):raise SystemExit('Disposable test folder required')
path=root/'provider.json'
provider=SupabaseDouble()
if path.exists():
    saved=json.loads(path.read_text())
    provider.rows=saved['rows'];provider.tokens=saved['tokens']
    provider.images={key:base64.b64decode(value) for key,value in saved['images'].items()}
lock=threading.RLock()
def remote(*args,**kwargs):
    with lock:
        result=provider.call(*args,**kwargs)
        path.write_text(json.dumps({'rows':provider.rows,'tokens':provider.tokens,'images':{key:base64.b64encode(value).decode() for key,value in provider.images.items()}}))
        return result
hosted.remote=remote
app.main()
