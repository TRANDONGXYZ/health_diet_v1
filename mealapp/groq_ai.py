"""Small Groq adapter. Operator credentials never enter config or backups."""
import http.client
import json
import os
import socket
import time
from pathlib import Path
import secrets
from . import hosted
from .ai_transport import InterruptibleSocket

TEXT_MODEL = 'qwen/qwen3.8-27b'
AUDIO_MODEL = 'whisper-large-v3-turbo'


def request(path, body, content_type='application/json'):
    from . import ai
    key = os.environ.get('GROQ_API_KEY', '')
    if not key:
        raise ValueError('Hosted AI is unavailable. The beta operator needs to configure Groq. Manual editing, Simple Rotation and Randomize are still available.')
    hosted.reserve_ai(audio=path.endswith('/transcriptions'))
    cancel = getattr(ai.REQUEST, 'cancel', None)
    started = time.monotonic()
    ai._phase('generating', started)
    connection = http.client.HTTPSConnection('api.groq.com', timeout=15)
    try:
        if cancel:cancel.check()
        connection.connect()
        transport = connection.sock
        if cancel:
            def remaining():
                seconds=started+180-time.monotonic()
                if seconds<=0:raise TimeoutError('Hosted AI deadline exceeded')
                return seconds
            transport = InterruptibleSocket(transport, cancel, remaining, 180)
            connection.sock = transport
            cancel.bind(transport)
        transport.settimeout(180)
        connection.request('POST', '/openai/v1' + path, body=body, headers={
            'Authorization':'Bearer ' + key, 'Content-Type':content_type})
        with connection.getresponse() as response:
            if response.status == 429:
                raise ValueError('The Groq quota is exhausted. Please try again later. Manual editing, Simple Rotation and Randomize are still available.')
            if response.status != 200:
                raise ValueError('Hosted AI is temporarily unavailable (Groq HTTP ' + str(response.status) + '). Please retry later or continue manually. Your source text and saved plan were kept.')
            raw = response.read(2_000_001)
            if len(raw)>2_000_000:raise ValueError('Hosted AI returned an oversized result. Try a shorter source.')
            if cancel:cancel.check()
            return json.loads(raw)
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        if cancel:cancel.check()
        raise ValueError('Hosted AI is unavailable or timed out. Try later; manual editing, Simple Rotation and Randomize still work. Your source text and saved plan were kept.') from None
    finally:
        if cancel:
            with cancel.lock:cancel.transport=None
        connection.close()
        ai._phase('idle')


def chat(system, task, schema, image=None, max_tokens=3000):
    content = task
    if image:
        content = [{'type':'text','text':task}, {'type':'image_url','image_url':{'url':'data:image/jpeg;base64,' + image}}]
    payload = {'model':TEXT_MODEL, 'messages':[{'role':'system','content':system},{'role':'user','content':content}],
               'temperature':0, 'reasoning_effort':'none', 'max_completion_tokens':min(max_tokens,4096),
               'response_format':{'type':'json_schema','json_schema':{'name':'recipe_result','strict':True,'schema':schema}}}
    response = request('/chat/completions', json.dumps(payload, ensure_ascii=False).encode())
    try:
        choice = response['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ValueError('The hosted model reached its output limit. Try a shorter source; your source text was kept.')
        result = json.loads(choice['message']['content'])
        if not isinstance(result,dict):raise TypeError()
        return result
    except (KeyError, TypeError, IndexError, json.JSONDecodeError):
        raise ValueError('Hosted AI returned an incomplete result. Retry with a shorter source; nothing was saved.') from None


def transcribe(path):
    file = Path(path)
    if file.stat().st_size>24*1024*1024:
        raise ValueError('Audio exceeds the hosted 24 MB limit. Use a shorter file or paste a transcript.')
    boundary = 'health-' + secrets.token_hex(16)
    fields = {'model':AUDIO_MODEL,'response_format':'json','temperature':'0'}
    # No language argument and no translation endpoint: preserve Vietnamese/English.
    chunks = []
    for name,value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    chunks.extend([f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.flac"\r\nContent-Type: audio/flac\r\n\r\n'.encode(),file.read_bytes(),f'\r\n--{boundary}--\r\n'.encode()])
    result = request('/audio/transcriptions', b''.join(chunks), 'multipart/form-data; boundary=' + boundary)
    text = str(result.get('text','')).strip()
    if not text:raise ValueError('No speech was recognized. Paste a transcript or use a clearer recording.')
    return text
