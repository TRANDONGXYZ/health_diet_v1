import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from mealapp import supabase_http


class API(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def do_GET(self):
        self.server.seen.append((self.command,self.headers.get('Authorization'),self.client_address[1]))
        body=json.dumps({'ok':True}).encode()
        self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        self.server.seen.append((self.command,self.headers.get('Authorization'),self.client_address[1]))
        self.rfile.read(int(self.headers.get('Content-Length',0)))
        self.close_connection=True
        self.connection.shutdown(socket.SHUT_RDWR);self.connection.close()
    def log_message(self,*args):pass


class SupabaseTransportTests(unittest.TestCase):
    def setUp(self):
        supabase_http.close_idle()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),API);self.server.seen=[]
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}/api'
    def tearDown(self):
        supabase_http.close_idle();self.server.shutdown();self.server.server_close();self.thread.join()
    def test_reuses_socket_but_never_another_testers_authentication(self):
        first={};second={}
        for token,stats in [('tester-A',first),('tester-B',second)]:
            status,body=supabase_http.request(self.url,None,{'Authorization':'Bearer '+token},stats=stats)
            self.assertEqual(status,200);self.assertEqual(json.loads(body),{'ok':True})
        self.assertEqual(self.server.seen[0][2],self.server.seen[1][2])
        self.assertEqual([item[1] for item in self.server.seen],['Bearer tester-A','Bearer tester-B'])
        self.assertIn('storage_connect',first);self.assertNotIn('storage_connect',second)
        self.assertEqual(second['storage_reused_calls'],1)
    def test_idle_connections_expire_before_first_write_after_inactivity(self):
        supabase_http.request(self.url,None,{})
        with patch.object(supabase_http,'IDLE_SECONDS',-1):supabase_http.request(self.url,None,{})
        self.assertNotEqual(self.server.seen[0][2],self.server.seen[1][2])
    def test_ambiguous_failed_post_is_not_retried(self):
        import http.client
        with self.assertRaises((OSError,http.client.HTTPException)):
            supabase_http.request(self.url,b'{}',{'Content-Type':'application/json'})
        self.assertEqual(len(self.server.seen),1)
