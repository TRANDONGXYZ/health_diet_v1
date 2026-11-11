import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from mealapp import ai


class FakeOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        self.server.payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(self.server.code)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for delay, packet in self.server.packets:
                if self.server.stopping.wait(delay):
                    return
                raw = (json.dumps(packet, ensure_ascii=False) if isinstance(packet, dict) else packet).encode() + b"\n"
                self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


def packet(content="", done=False, **extra):
    return {"message": {"content": content}, "done": done, **extra}


class StreamingAITests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        self.server.code = 200
        self.server.packets = [(0, packet('{"answer":"Đậu hũ"}', True, done_reason="stop"))]
        self.server.stopping = threading.Event()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.values = dict(ai.DEFAULTS, ollama_url=f"http://127.0.0.1:{self.server.server_port}", ai_timeout_seconds=3, ai_read_timeout_seconds=1)
        self.config = patch.object(ai, "config", return_value=self.values)
        self.config.start()

    def tearDown(self):
        self.server.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.config.stop()
        self.assertEqual(ai.progress()["phase"], "idle")
        self.assertTrue(ai.LOCK.acquire(blocking=False), "AI lock leaked after a completed or failed request")
        ai.LOCK.release()

    def ask(self):
        return ai.ask("Extract a recipe", ai.obj({"answer": ai.TEXT}), max_tokens=100)

    def test_streaming_payload_and_unicode_assembly(self):
        self.server.packets = [(0, packet('{"answer":"')), (0, packet('Đậu hũ')), (0, packet('"}')), (0, packet(done=True))]
        self.assertEqual(self.ask(), {"answer":"Đậu hũ"})
        self.assertIs(self.server.payload["stream"], True)

    def test_recipe_language_is_explicit_even_with_old_english_preference(self):
        self.values["language"]="English"
        ai.ask("Extract recipe",ai.obj({"answer":ai.TEXT}),language="Vietnamese")
        system=self.server.payload["messages"][0]["content"]
        self.assertIn("Write all natural-language output in Vietnamese",system)
        self.assertNotIn("Keep original dish and ingredient names",system)

    def test_interface_and_nutrition_helpers_stay_english(self):
        self.values["language"]="Vietnamese"
        self.ask()
        system=self.server.payload["messages"][0]["content"]
        self.assertIn("Write all natural-language output in English",system)

    def test_active_stream_can_run_longer_than_its_idle_timeout(self):
        self.values.update(ai_read_timeout_seconds=0.4)
        self.server.packets = [(0.08, packet(part)) for part in ('{', '"answer"', ':', '"Tofu"', '}')]
        self.server.packets.append((0.08, packet(done=True)))
        started = time.monotonic()
        self.assertEqual(self.ask()["answer"], "Tofu")
        self.assertGreater(time.monotonic() - started, 0.4)

    def test_idle_timeout_is_not_reported_as_unavailable(self):
        self.values["ai_read_timeout_seconds"] = 0.08
        self.server.packets = [(0.4, packet('{}', True))]
        with self.assertRaisesRegex(ai.AITimeoutError, "sent no data"):
            self.ask()

    def test_total_deadline_applies_even_while_tokens_keep_arriving(self):
        self.values["ai_timeout_seconds"] = 0.2
        self.server.packets = [(0.06, packet(part)) for part in ('{', '"answer"', ':', '"Tofu"', '}')]
        with self.assertRaisesRegex(ai.AITimeoutError, "processing limit"):
            self.ask()

    def test_missing_model_has_install_instruction(self):
        self.server.code = 404
        with self.assertRaisesRegex(ValueError, "ollama pull"):
            self.ask()

    def test_model_server_error_has_separate_message(self):
        self.server.code = 500
        with self.assertRaisesRegex(ValueError, "HTTP 500"):
            self.ask()

    def test_output_limit_is_not_a_network_error(self):
        self.server.packets = [(0, packet('{"answer":', True, done_reason="length"))]
        with self.assertRaisesRegex(ValueError, "output limit"):
            self.ask()

    def test_stream_disconnect_is_not_accepted_as_completed_json(self):
        self.server.packets = [(0, packet('{"answer":"Tofu"}'))]
        with self.assertRaisesRegex(ai.AIConnectionError, "before finishing"):
            self.ask()

    def test_invalid_stream_is_explained(self):
        self.server.packets = [(0, "not-json")]
        with self.assertRaisesRegex(ValueError, "invalid response stream"):
            self.ask()

    def test_streamed_model_execution_error(self):
        self.server.packets = [(0, {"error":"out of memory"})]
        with self.assertRaisesRegex(ValueError, "execution error"):
            self.ask()

    def test_progress_exposes_phase_and_elapsed_time_only(self):
        phases = []
        original = ai._phase
        def update(phase, started=None):
            original(phase, started)
            phases.append(ai.progress())
        with patch.object(ai, "_phase", side_effect=update):
            self.ask()
        self.assertEqual([p["phase"] for p in phases], ["loading", "generating", "idle"])
        self.assertTrue(all(set(p) == {"phase", "elapsed_seconds"} for p in phases))

    def test_tcp_connection_failure_has_separate_error_and_releases_lock(self):
        with patch.object(ai.http.client.HTTPConnection, "connect", side_effect=ConnectionRefusedError):
            with self.assertRaisesRegex(ai.AIConnectionError, "Could not connect"):
                self.ask()

    def test_connect_timeout_has_separate_error(self):
        with patch.object(ai.http.client.HTTPConnection, "connect", side_effect=TimeoutError):
            with self.assertRaisesRegex(ai.AIConnectionError, "connection to Ollama"):
                self.ask()

    def test_cancellation_interrupts_blocked_read_and_releases_model_lock(self):
        self.values.update(ai_timeout_seconds=900,ai_read_timeout_seconds=180)
        self.server.packets=[(20,packet('{"answer":"slow"}',True))]
        token=ai.Cancellation();result=[]
        def run():
            try:
                with ai.cancellation_scope(token):self.ask()
            except Exception as exc:result.append(exc)
        thread=threading.Thread(target=run);thread.start()
        deadline=time.monotonic()+2
        while not hasattr(self.server,'payload') and time.monotonic()<deadline:time.sleep(.01)
        self.assertTrue(hasattr(self.server,'payload'))
        started=time.monotonic();token.cancel();thread.join(2)
        self.assertFalse(thread.is_alive(),'Cancellation must interrupt the socket read, not wait 180 seconds')
        self.assertLess(time.monotonic()-started,2)
        self.assertIsInstance(result[0],ai.AICancelled)
        self.assertEqual(ai.progress()['phase'],'idle')

    def test_cancel_before_model_connection_never_starts_inference(self):
        token=ai.Cancellation();token.cancel()
        with self.assertRaises(ai.AICancelled),ai.cancellation_scope(token):self.ask()
        self.assertFalse(hasattr(self.server,'payload'))

    def test_cancellable_transport_also_completes_normal_responses(self):
        with ai.cancellation_scope(ai.Cancellation()):
            self.assertEqual(self.ask(),{'answer':'Đậu hũ'})

    def test_cancellable_transport_preserves_total_and_idle_deadlines(self):
        self.values.update(ai_timeout_seconds=.18,ai_read_timeout_seconds=180)
        self.server.packets=[(.06,packet(part)) for part in ('{','"answer"',':','"Tofu"','}')]
        with self.assertRaisesRegex(ai.AITimeoutError,'processing limit'),ai.cancellation_scope(ai.Cancellation()):self.ask()


if __name__ == "__main__":
    unittest.main()
