using NAudio.Wave;

namespace Blurt;

/// Captures the default mic and emits 16 kHz mono Int16 PCM frames (little-endian),
/// exactly what the server's WebSocket expects — the twin of the Mac client's
/// AudioCapture.swift. We ask WaveInEvent (WinMM over WASAPI shared mode) for
/// 16 kHz/mono/16-bit directly; Windows resamples from the device format for us,
/// so no manual AVAudioConverter step is needed. Little-endian is native on x64,
/// so the bytes are already in the wire format.
internal sealed class AudioCapture : IDisposable
{
    private WaveInEvent? _waveIn;

    /// Called on an audio thread with a chunk of Int16 PCM bytes. The byte array
    /// is only valid for the duration of the call — copy if you retain it.
    public Action<byte[], int>? OnFrame;

    public void Start()
    {
        Stop();
        var waveIn = new WaveInEvent
        {
            WaveFormat = new WaveFormat(16000, 16, 1),
            BufferMilliseconds = 100, // ~3.2 KB frames — matches the server's partial cadence
        };
        waveIn.DataAvailable += (_, e) =>
        {
            // e.Buffer may be larger than the valid region; only send BytesRecorded.
            if (e.BytesRecorded > 0) OnFrame?.Invoke(e.Buffer, e.BytesRecorded);
        };
        waveIn.StartRecording();
        _waveIn = waveIn;
    }

    public void Stop()
    {
        if (_waveIn is null) return;
        try { _waveIn.StopRecording(); } catch { /* already stopped */ }
        _waveIn.Dispose();
        _waveIn = null;
    }

    /// True when at least one input device is present — used by onboarding to tell
    /// the user up front if there's no mic to hear them.
    public static bool HasInputDevice => WaveInEvent.DeviceCount > 0;

    public void Dispose() => Stop();
}
