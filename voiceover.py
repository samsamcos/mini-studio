import os, logging, requests
from flask import Flask, request, jsonify
import ondemand

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
app = Flask(__name__)

LLAMA_URL  = os.environ.get('LLAMA_VOICE_URL', 'http://127.0.0.1:8082')
LLAMA_UNIT = os.environ.get('LLAMA_VOICE_UNIT', 'llama-gemma')
LLAMA_PORT = int(os.environ.get('LLAMA_VOICE_PORT', '8082'))

ondemand.start_reaper()


@app.route('/polish', methods=['POST'])
def polish():
    data = request.json
    raw_script = data.get('raw_script', '')

    # Gemma 2 9B is loaded on demand and released after idle timeout.
    ondemand.ensure(LLAMA_UNIT, LLAMA_PORT)

    prompt = f'You are a documentary narrator and script editor.\nRewrite the following rough spoken audio into polished documentary-style narration.\n- Remove filler words and rambling\n- Preserve the core meaning\n- Insert natural pauses with ... and cadence breaks with --\n- Keep it natural and engaging\n\nExample style:\n"The location looked abandoned... But after checking the footage -- there was actually movement inside."\n\nRaw input:\n{raw_script}\n\nReturn only the polished narration text.'
    resp = requests.post(f'{LLAMA_URL}/v1/chat/completions', json={
        'model': 'gemma',
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': 0.7
    }, timeout=300)
    resp.raise_for_status()
    ondemand.touch(LLAMA_UNIT, LLAMA_PORT)
    return jsonify({'polished_text': resp.json()['choices'][0]['message']['content'].strip()})


@app.route('/model/status')
def model_status():
    return jsonify({'unit': LLAMA_UNIT, 'loaded': ondemand._healthy(LLAMA_PORT)})


@app.route('/model/stop', methods=['POST'])
def model_stop():
    import subprocess
    subprocess.run(['systemctl', 'stop', LLAMA_UNIT], check=False)
    return jsonify({'ok': True})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9542)
