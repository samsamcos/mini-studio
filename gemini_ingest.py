import os, time, subprocess, logging, json
from flask import Flask, request, jsonify
from google import genai

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
log = logging.getLogger(__name__)
app = Flask(__name__)

# Free tier: 15 RPM / 1500 RPD / ~300 tokens per second of video at 1 FPS
STRUCTURED_PROMPT = (
    'Analyse this video clip and return a JSON object with exactly these keys:\n'
    '{"objects": ["..."], "location": "...", "actions": ["..."]}\n'
    'Return JSON only, no markdown.'
)


def make_proxy(input_path):
    proxy = input_path.rsplit('.', 1)[0] + '_proxy.mp4'
    log.info(f'FFmpeg: 480p/1fps proxy -> {proxy}')
    subprocess.run([
        'ffmpeg', '-y', '-i', input_path,
        '-vf', 'scale=-2:480,fps=1',
        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '30',
        '-c:a', 'aac', '-b:a', '64k', '-ac', '1', proxy
    ], check=True, capture_output=True)
    size_mb = os.path.getsize(proxy) / 1_048_576
    log.info(f'Proxy created: {size_mb:.1f} MB')
    return proxy


def generate_with_retry(client, uploaded, prompt, retries=3):
    for attempt in range(retries):
        try:
            return client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[uploaded, prompt]
            )
        except Exception as e:
            if '503' in str(e) or 'UNAVAILABLE' in str(e) or 'quota' in str(e).lower():
                wait = 15 * (attempt + 1)
                log.warning(f'Rate/503 (attempt {attempt+1}/{retries}) retry in {wait}s')
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue
            raise


@app.route('/inspect', methods=['POST'])
def inspect():
    data = request.json
    video_path = data.get('video_path')
    prompt     = data.get('prompt', '')
    structured = data.get('structured', True)

    if not video_path or not os.path.exists(video_path):
        return jsonify({'error': f'video_path not found: {video_path}'}), 400

    if not prompt:
        prompt = STRUCTURED_PROMPT if structured else 'Describe what is happening in this video clip.'

    client   = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    proxy    = None
    uploaded = None

    try:
        proxy = make_proxy(video_path)

        log.info('Uploading proxy to Gemini File API...')
        uploaded = client.files.upload(file=proxy)

        log.info(f'Polling for ACTIVE (current: {uploaded.state.name})')
        while uploaded.state.name == 'PROCESSING':
            time.sleep(4)
            uploaded = client.files.get(name=uploaded.name)
            log.info(f'  state: {uploaded.state.name}')

        if uploaded.state.name == 'FAILED':
            raise RuntimeError('Gemini processing failed on Google backend.')
        if uploaded.state.name != 'ACTIVE':
            raise RuntimeError(f'Unexpected Gemini file state: {uploaded.state.name}')

        log.info('File ACTIVE — calling gemini-2.5-flash')
        response = generate_with_retry(client, uploaded, prompt)

        client.files.delete(name=uploaded.name)
        log.info('Remote file deleted')
        uploaded = None

        text = response.text.strip()

        if structured:
            try:
                clean = text.strip('`')
                if clean.startswith('json'):
                    clean = clean[4:].strip()
                return jsonify({'result': json.loads(clean)})
            except json.JSONDecodeError:
                pass

        return jsonify({'result': text})

    except Exception as e:
        log.error(f'Error: {e}')
        return jsonify({'error': str(e)}), 500

    finally:
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
                log.info('Remote file deleted (failure cleanup)')
            except Exception:
                pass
        if proxy and os.path.exists(proxy):
            os.remove(proxy)
            log.info(f'Local proxy deleted: {proxy}')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=9543)
