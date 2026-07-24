"""Stream a wav to the running server over the WebSocket, print partials + final.

Simulates the Mac client (see docs/protocol.md): paces 16 kHz PCM16 frames in
real time so VAD segmentation and partial cadence behave as they would live.
Prints the info handshake, vad transitions, and structured partials
(committed + live).

    python scripts/ws_client_test.py [wav] [ws_url]
"""

import sys
import ssl
import json
import time
import uuid
import asyncio

import numpy as np
import soundfile as sf
import websockets

SR = 16000
FRAME_MS = 100


def load_pcm16(path: str) -> bytes:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SR)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()


async def main():
    wav = sys.argv[1] if len(sys.argv) > 1 else "audio/clean.wav"
    url = sys.argv[2] if len(sys.argv) > 2 else "wss://localhost:25878/ws"
    pcm = load_pcm16(wav)
    frame_bytes = int(SR * FRAME_MS / 1000) * 2
    dictation_id = uuid.uuid4().hex

    ctx = ssl._create_unverified_context() if url.startswith("wss") else None
    async with websockets.connect(url, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({
            "type": "start", "id": dictation_id,
            "audio": {"rate": SR, "width": 2, "channels": 1},
        }))

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                # drop anything from a dictation that isn't ours
                if m.get("id") not in (None, "", dictation_id):
                    continue
                if m["type"] == "info":
                    print(f"[info] {m.get('server')} v{m.get('version')} "
                          f"protocol={m.get('protocol')} model={m.get('model')} "
                          f"state={m.get('state')}")
                elif m["type"] == "vad":
                    print(f"[vad] {'speech' if m.get('speech') else 'silence'}")
                elif m["type"] == "partial":
                    committed = m.get("committed", m["text"])
                    live = m.get("live", "")
                    print(f"  … {committed}▐{live}")
                elif m["type"] == "final":
                    print(f"\nFINAL: {m['text']}")
                    return
                elif m["type"] == "status":
                    print(f"[status] {m}")

        rtask = asyncio.create_task(reader())
        t0 = time.time()
        for off in range(0, len(pcm), frame_bytes):
            await ws.send(pcm[off:off + frame_bytes])
            await asyncio.sleep(FRAME_MS / 1000)
        print(f"[sent {len(pcm)/2/SR:.1f}s of audio in {time.time()-t0:.1f}s]")
        await ws.send(json.dumps({"type": "stop", "id": dictation_id}))
        await asyncio.wait_for(rtask, timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
