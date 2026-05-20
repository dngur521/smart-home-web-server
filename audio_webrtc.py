import asyncio, fractions, json, os, subprocess, threading, time
import av
from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer

ALSA_DEVICE     = 'plughw:2,0'
PORT            = 8083
FRONTEND_ORIGIN = os.environ.get('FRONTEND_ORIGIN', 'http://localhost:5173')
SAMPLE_RATE     = 48000
SAMPLES         = 960           # 20ms per frame (WebRTC 표준)
BYTES_PER_FRAME = SAMPLES * 2   # s16le mono

ICE_CONFIG = RTCConfiguration(iceServers=[
    RTCIceServer(urls=['stun:stun.l.google.com:19302'])
])

pcs = set()


class _Broadcaster:
    """시스템 ffmpeg 1개 → 다중 WebRTC 클라이언트에 PCM 프레임 분배"""

    def __init__(self):
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._lock = threading.Lock()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                proc = subprocess.Popen(
                    ['ffmpeg',
                     '-f', 'alsa', '-i', ALSA_DEVICE,
                     '-ac', '1', '-ar', str(SAMPLE_RATE), '-f', 's16le', 'pipe:1'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                while True:
                    data = proc.stdout.read(BYTES_PER_FRAME)
                    if len(data) < BYTES_PER_FRAME:
                        break
                    with self._lock:
                        for loop, q in self._subs:
                            if q.qsize() < 50:
                                loop.call_soon_threadsafe(q.put_nowait, data)
                proc.wait()
            except Exception:
                pass
            time.sleep(3)

    def subscribe(self, loop: asyncio.AbstractEventLoop) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subs.append((loop, q))
        return q

    def unsubscribe(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subs.remove((loop, q))
            except ValueError:
                pass


class AlsaAudioTrack(MediaStreamTrack):
    kind = 'audio'

    def __init__(self, broadcaster: _Broadcaster):
        super().__init__()
        self._loop        = asyncio.get_running_loop()
        self._broadcaster = broadcaster
        self._queue       = broadcaster.subscribe(self._loop)
        self._timestamp   = 0

    async def recv(self) -> av.AudioFrame:
        data = await self._queue.get()
        frame = av.AudioFrame(format='s16', layout='mono', samples=SAMPLES)
        frame.pts         = self._timestamp
        frame.sample_rate = SAMPLE_RATE
        frame.time_base   = fractions.Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(data)
        self._timestamp  += SAMPLES
        return frame

    def stop(self):
        super().stop()
        self._broadcaster.unsubscribe(self._loop, self._queue)


def _cors(origin: str) -> dict:
    return {
        'Access-Control-Allow-Origin':      origin,
        'Access-Control-Allow-Credentials': 'true',
        'Access-Control-Allow-Headers':     'Content-Type',
    }


async def offer(request: web.Request) -> web.Response:
    origin = request.headers.get('Origin', FRONTEND_ORIGIN)

    if request.method == 'OPTIONS':
        return web.Response(headers=_cors(origin))

    broadcaster = request.app['broadcaster']
    params      = await request.json()
    sdp_offer   = RTCSessionDescription(sdp=params['sdp'], type=params['type'])

    pc = RTCPeerConnection(configuration=ICE_CONFIG)
    pcs.add(pc)

    @pc.on('connectionstatechange')
    async def on_state_change():
        if pc.connectionState in ('failed', 'closed', 'disconnected'):
            await pc.close()
            pcs.discard(pc)

    # setRemoteDescription 먼저 → 트랜시버 방향이 offer SDP로 초기화된 뒤 track 추가
    await pc.setRemoteDescription(sdp_offer)
    pc.addTrack(AlsaAudioTrack(broadcaster))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type='application/json',
        headers=_cors(origin),
        text=json.dumps({'sdp': pc.localDescription.sdp, 'type': pc.localDescription.type}),
    )


async def on_startup(app: web.Application) -> None:
    app['broadcaster'] = _Broadcaster()


async def on_shutdown(app: web.Application) -> None:
    await asyncio.gather(*[pc.close() for pc in pcs])
    pcs.clear()


app = web.Application()
app.on_startup.append(on_startup)
app.on_shutdown.append(on_shutdown)
app.router.add_post('/offer', offer)
app.router.add_route('OPTIONS', '/offer', offer)

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=PORT)
