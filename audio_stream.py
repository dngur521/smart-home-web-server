from flask import Flask, Response
from flask_cors import CORS
import subprocess, threading, time, collections, os

app = Flask(__name__)
CORS(app, origins=os.environ.get('FRONTEND_ORIGIN', 'http://localhost:5173'),
     supports_credentials=True)

ALSA_DEVICE = 'plughw:2,0'
CHUNK_SIZE  = 512
MAX_QUEUE   = 60  # 클라이언트당 버퍼 (청크 작아져서 개수 늘림)


class AudioBroadcaster:
    def __init__(self):
        self._clients = []
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                proc = subprocess.Popen(
                    ['ffmpeg',
                     '-f', 'alsa', '-i', ALSA_DEVICE,
                     '-ac', '1', '-c:a', 'libmp3lame', '-b:a', '32k',
                     '-reservoir', '0', '-flush_packets', '1',
                     '-f', 'mp3', 'pipe:1'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
                )
                while True:
                    chunk = proc.stdout.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    with self._lock:
                        for q in self._clients:
                            if len(q) < MAX_QUEUE:
                                q.append(chunk)
                proc.wait()
            except Exception:
                pass
            time.sleep(3)

    def subscribe(self):
        q = collections.deque()
        with self._lock:
            self._clients.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            try:
                self._clients.remove(q)
            except ValueError:
                pass


broadcaster = AudioBroadcaster()


@app.route('/audio')
def audio():
    q = broadcaster.subscribe()

    def generate():
        try:
            while True:
                if q:
                    yield q.popleft()
                else:
                    time.sleep(0.05)
        finally:
            broadcaster.unsubscribe(q)

    return Response(
        generate(),
        mimetype='audio/mpeg',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8082, threaded=True)
