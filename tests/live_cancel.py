"""Opt-in real Ollama cancellation and scheduler-release check. No recipe writes."""
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from mealapp import ai

if not ai.status()['connected']:
    raise SystemExit('Ollama is offline; live cancellation check needs the local server.')
values=ai.config();values.update(ai_timeout_seconds=45,ai_read_timeout_seconds=30)
token=ai.Cancellation();outcome=[]


def run():
    try:
        with ai.cancellation_scope(token):
            ai.ask('Return a list of exactly 1000 short kitchen item names.',ai.obj({'words':{'type':'array','minItems':1000,'maxItems':1000,'items':ai.TEXT}}),max_tokens=4000)
        outcome.append('completed')
    except Exception as exc:outcome.append(exc)


with patch.object(ai,'config',return_value=values):
    thread=threading.Thread(target=run);thread.start()
    deadline=time.monotonic()+40
    while ai.progress()['phase']!='generating' and thread.is_alive() and time.monotonic()<deadline:time.sleep(.05)
    started=time.monotonic();token.cancel();thread.join(3)
    assert not thread.is_alive(),'The cancelled worker did not stop promptly'
    assert outcome and isinstance(outcome[0],ai.AICancelled),outcome
    assert ai.progress()['phase']=='idle'
    cancelled_in=time.monotonic()-started
    started=time.monotonic()
    answer=ai.ask('Return {"answer":"ok"}.',ai.obj({'answer':ai.TEXT}),max_tokens=30)
    elapsed=time.monotonic()-started
    assert answer.get('answer') and elapsed<30,'Ollama did not accept a fresh task promptly after cancellation'
    print(f'PASS: real Ollama request cancelled in {cancelled_in:.2f}s; a fresh request completed in {elapsed:.1f}s. No recipes saved.')
