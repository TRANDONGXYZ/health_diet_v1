"""Optional live handoff checks. Keeps all identity tokens in process memory."""
import argparse
import base64
import http.cookiejar
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from mealapp import hosted
from test_weekly import png


def browser(origin):
    opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    token=None
    def call(route,data=None,raw=False,allow_error=False):
        nonlocal token
        headers={'Origin':origin}
        if token:headers['X-App-Token']=token
        if data is not None:headers['Content-Type']='application/zip' if raw else 'application/json'
        body=data if raw else json.dumps(data).encode() if data is not None else None
        request=urllib.request.Request(origin+route,data=body,headers=headers)
        try:response=opener.open(request,timeout=240)
        except urllib.error.HTTPError as error:
            if allow_error:
                with error:
                    value=error.read()
                    return error.code,json.loads(value) if error.headers.get_content_type()=='application/json' else None
            raise RuntimeError('Live beta returned HTTP '+str(error.code)+' for '+route.split('?')[0]) from None
        with response:
            value=response.read()
            if response.headers.get_content_type()=='application/json':value=json.loads(value)
            if route=='/api/state':token=value['token']
            return response.code,value
    call('/api/state')
    return call


def check_url(origin,restart=False,ai=False):
    if not origin.startswith('https://'):raise ValueError('Use the deployed HTTPS origin.')
    a,b=browser(origin),browser(origin)
    marker='Beta check '+uuid.uuid4().hex[:10]
    image=png()
    _,photo=a('/api/dish-photo',{'filename':'dish.png','image':base64.b64encode(image).decode()})
    _,result=a('/api/recipes',{'name':marker,'servings':2,'photo_url':photo['photo_url'],'ingredients':[{'name':'tofu','grams':200}]})
    a('/api/plan/generate',{'method':'rotation'})
    _,before=a('/api/state')
    assert not b('/api/state')[1]['recipes']
    assert b(photo['photo_url'],allow_error=True)[0]==404
    assert a(photo['photo_url'])[1]==image
    _,archive=a('/api/backup/export')
    a('/api/recipes/delete',{'id':result['recipe']['id']})
    a('/api/backup/import',archive,raw=True)
    assert a('/api/state')[1]['recipes']==before['recipes']
    assert a('/api/state')[1]['plans']==before['plans']
    if restart:
        input('Restart or redeploy the Render service now. When it is live again, press Enter here: ')
        _,after=a('/api/state')
        assert after['recipes']==before['recipes'] and after['plans']==before['plans']
        assert a(photo['photo_url'])[1]==image
    if ai:
        for text in ('Đậu hũ chiên. Nguyên liệu: 200 g đậu hũ. Chiên đậu hũ cho vàng.',
                     'Fried tofu. Ingredients: 200 g tofu. Fry the tofu until golden.'):
            _,recipe=a('/api/import',{'text':text})
            assert recipe['recipe']['ingredients']
            assert recipe['recipe']['content_language']==('vi' if text.startswith('Đậu') else 'en')
    print('PASS: live isolated tester sessions, private images, recipes/plans, ZIP restore'+(', Render restart' if restart else '')+('. Real text AI requests sent.' if ai else '. No AI quota used.'))


def check_rls():
    for name in ('SUPABASE_URL','SUPABASE_PUBLISHABLE_KEY'):
        if not os.environ.get(name):raise ValueError('Set '+name+' in the environment first.')
    a=hosted.remote('/auth/v1/signup',{})
    b=hosted.remote('/auth/v1/signup',{})
    uid=a['user']['id'];ta=a['access_token'];tb=b['access_token']
    hosted.remote('/rest/v1/rpc/health_beta_save',{'expected_revision':0,'new_document':{'test':'private RLS handoff record'}},token=ta)
    assert hosted.remote('/rest/v1/health_beta_data?select=revision&user_id=eq.'+uid,token=tb)==[]
    assert hosted.remote('/rest/v1/health_beta_data?select=revision&user_id=eq.'+uid,token=ta)
    import hashlib
    image=png();name=hashlib.sha256(image).hexdigest()+'.png'
    key=uid+'/'+name
    hosted.remote('/storage/v1/object/health-diet-images/'+key,image,token=ta,method='POST',raw=True)
    assert hosted.remote('/storage/v1/object/authenticated/health-diet-images/'+key,token=ta,raw=True)==image
    try:hosted.remote('/storage/v1/object/authenticated/health-diet-images/'+key,token=tb,raw=True)
    except ValueError:pass
    else:raise AssertionError('Storage policy exposed another tester image')
    try:hosted.remote('/rest/v1/health_beta_data',{'user_id':uid,'revision':2,'document':{'test':'forged owner'}},token=tb,method='POST')
    except ValueError:pass
    else:raise AssertionError('Row policy allowed a forged owner')
    print('PASS: live Supabase row and image owner policies; two disposable anonymous users created. No tokens printed.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url');parser.add_argument('--rls',action='store_true')
    parser.add_argument('--restart',action='store_true');parser.add_argument('--ai',action='store_true')
    args=parser.parse_args()
    if args.rls:check_rls()
    if args.url:check_url(args.url.rstrip('/'),args.restart,args.ai)
    if not(args.rls or args.url):parser.error('Choose --rls and/or --url HTTPS_ORIGIN')
