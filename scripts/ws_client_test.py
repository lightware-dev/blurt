"""Stream a wav to the running server over the WebSocket, print partials + final.

Simulates the Mac client: paces 16 kHz PCM16 frames in real time so VAD
segmentation and partial cadence behave as they would live.

    python scripts/ws_client_test.py [wav] [ws_url]
"""

import sys
import ssl
import json
import time
import asyncio
from pathlib import Path

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
    url = sys.argv[2] if len(sys.argv) > 2 else "wss://localhost:7860/ws"
    pcm = load_pcm16(wav)
    frame_bytes = int(SR * FRAME_MS / 1000) * 2

    ctx = ssl._create_unverified_context() if url.startswith("wss") else None
    async with websockets.connect(url, ssl=ctx, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start"}))

        async def reader():
            async for raw in ws:
                m = json.loads(raw)
                if m["type"] == "partial":
                    print(f"  … {m['text']}")
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
        await ws.send(json.dumps({"type": "stop"}))
        await asyncio.wait_for(rtask, timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
