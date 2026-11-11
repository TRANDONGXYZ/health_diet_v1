"""Bounded keep-alive transport for the single operator-configured API origin.

Authorization is supplied per request. Connections retain neither default auth
headers nor cookies. Writes are never automatically retried after disconnection.
"""
import http.client
import queue
import select
import threading
import time
import urllib.parse

POOLS={}
LOCK=threading.Lock()
IDLE_SECONDS=10


class Pool:
    def __init__(self):
        self.idle=queue.LifoQueue(maxsize=6)
        self.slots=threading.BoundedSemaphore(6)


def elapsed(stats,name,started):
    if stats is not None:stats[name]=stats.get(name,0)+(time.monotonic()-started)*1000


def request(url,body,headers,method=None,maximum=12*1024*1024,stats=None):
    parsed=urllib.parse.urlsplit(url)
    key=(parsed.scheme,parsed.hostname,parsed.port)
    with LOCK:pool=POOLS.setdefault(key,Pool())
    started=time.monotonic()
    if not pool.slots.acquire(timeout=25):raise TimeoutError('Storage connections are busy')
    elapsed(stats,'storage_pool_wait',started)
    connection=None;reusable=False
    try:
        while True:
            try:connection,last_used=pool.idle.get_nowait()
            except queue.Empty:break
            if time.monotonic()-last_used<IDLE_SECONDS and connection.sock is not None and not select.select([connection.sock],[],[],0)[0]:break
            connection.close();connection=None
        if connection is None:
            factory=http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
            connection=factory(parsed.hostname,parsed.port,timeout=25)
            started=time.monotonic();connection.connect();elapsed(stats,'storage_connect',started)
        else:
            if stats is not None:stats['storage_reused_calls']=stats.get('storage_reused_calls',0)+1
        path=parsed.path or '/'
        if parsed.query:path+='?'+parsed.query
        connection.request(method or ('POST' if body is not None else 'GET'),path,body=body,headers=headers)
        started=time.monotonic();response=connection.getresponse();elapsed(stats,'storage_headers',started)
        started=time.monotonic();raw=response.read(maximum+1);elapsed(stats,'storage_body',started)
        reusable=response.isclosed() and not response.will_close and connection.sock is not None
        return response.status,raw
    finally:
        if connection is not None:
            if reusable:
                try:pool.idle.put_nowait((connection,time.monotonic()))
                except queue.Full:connection.close()
            else:connection.close()
        pool.slots.release()


def close_idle():
    with LOCK:pools=list(POOLS.values());POOLS.clear()
    for pool in pools:
        while True:
            try:connection,_=pool.idle.get_nowait();connection.close()
            except queue.Empty:break
