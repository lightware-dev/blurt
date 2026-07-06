// Captures mic audio, downsamples to 16 kHz, and posts Int16 PCM frames to the
// main thread. Runs on the audio-render thread so it doesn't block the UI.
class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.targetRate = 16000;
    this.ratio = sampleRate / this.targetRate; // e.g. 48000/16000 = 3
    this._acc = 0;   // fractional read position for linear resampling
    this._buf = [];  // accumulated 16 kHz float samples
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const ch = input[0];
    if (!ch) return true;

    // Linear-resample this 128-sample block from sampleRate -> 16 kHz.
    for (let i = this._acc; i < ch.length; i += this.ratio) {
      const idx = Math.floor(i);
      const frac = i - idx;
      const s0 = ch[idx];
      const s1 = idx + 1 < ch.length ? ch[idx + 1] : s0;
      this._buf.push(s0 + (s1 - s0) * frac);
    }
    this._acc = (this._acc + Math.ceil((ch.length - this._acc) / this.ratio) * this.ratio) - ch.length;

    // Flush in ~64 ms frames (1024 samples @ 16 kHz).
    if (this._buf.length >= 1024) {
      const frame = this._buf;
      this._buf = [];
      const pcm = new Int16Array(frame.length);
      for (let i = 0; i < frame.length; i++) {
        const v = Math.max(-1, Math.min(1, frame[i]));
        pcm[i] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]);
    }
    return true;
  }
}
registerProcessor("pcm-worklet", PCMWorklet);
