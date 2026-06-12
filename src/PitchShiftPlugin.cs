using System;
using System.Globalization;
using System.Text.Json;
using Avalonia.Controls;
using Avalonia.Layout;
using NAudio.Wave;
using SoundBoard.PluginApi;

namespace PitchShiftPlugin;

/// <summary>
/// <see cref="IAudioSamplerPlugin"/> for pitch shifting. Shifts a signal's
/// perceived pitch up or down without changing playback speed.
/// Range: ±24 semitones. Useful for villain voices (-12), creature
/// roars (-6), pixie chatter (+12), etc.
///
/// <para>Implemented as a two-grain overlap-add granular pitch shifter
/// (50 ms grains @ 50% overlap, Hann window). Pre-allocates a ring
/// buffer, the Hann window, and grain state — no audio-thread
/// allocations. Semitone parameter is published atomically via a
/// volatile float bit-pattern; the derived <c>ratio = 2^(s/12)</c> is
/// cached the same way so the audio thread never calls <c>MathF.Pow</c>.</para>
///
/// <para><b>Latency.</b> The algorithm introduces a fixed delay of one
/// grain (~50 ms) so grains can read from past samples regardless of
/// shift direction. Bypass via the host's <c>BypassableSamplerInstance</c>
/// removes the delay (passthrough).</para>
/// </summary>
public sealed class PitchShiftPlugin : IAudioSamplerPlugin
{
    public string Id => "sampler.pitchshift";
    public string Name => "Pitch Shift";
    public string Description => "Shifts pitch up or down (±24 semitones) without changing playback speed.";
    public string Version => PluginVersion.OfAssembly(typeof(PitchShiftPlugin));
    public string Author => "Devin Sanders";

    public SamplerAttachmentPoints SupportedAttachments => SamplerAttachmentPoints.All;

    public void Initialize(IPluginContext context) { }
    public void Shutdown() { }

    public ISamplerInstance CreateInstance() => new PitchShiftInstance();
}

internal sealed class PitchShiftInstance : ISamplerInstance
{
    // 50 ms grain @ 48 kHz; 50% overlap (hop = grain / 2). Hann at 50%
    // overlap sums to unity so two staggered grains reconstruct exactly
    // when ratio = 1.
    private const int GrainSize = 2400;
    private const int HopSize = GrainSize / 2;

    // Ring buffer holds enough past input that even at ratio = 4 (the
    // +24-semitone extreme), a fresh grain can look back ratio*GrainSize
    // samples without underrunning. 16384 ≈ 341 ms @ 48 kHz — power of
    // two so we can mask instead of mod.
    private const int RingSize = 16384;
    private const int RingMask = RingSize - 1;

    // Pre-allocated, never resized. Per-instance — two attachments must
    // not share these.
    private readonly float[] _ringL = new float[RingSize];
    private readonly float[] _ringR = new float[RingSize];
    private readonly float[] _hann = new float[GrainSize];

    // Atomically-published parameter state. UI thread writes via the
    // Semitones setter (which updates both fields), audio thread reads
    // _ratioBits each Read. Cents-precision storage in the float — the
    // UI rounds to 0.5 but the algorithm has no problem with arbitrary
    // values.
    private volatile int _semitoneBits;   // float bit-pattern of semitones
    private volatile int _ratioBits;      // float bit-pattern of 2^(s/12)

    public PitchShiftInstance()
    {
        // Precompute the *periodic* Hann window: 0.5 * (1 - cos(2π i / N)).
        // The periodic form (divide by N, not N-1) is what satisfies the
        // constant-overlap-add condition at 50% overlap: hann[i] +
        // hann[i + HopSize] == 1 exactly, because HopSize/N is exactly 0.5.
        // The symmetric (N-1) form leaves a ~HopSize-rate amplitude ripple
        // and breaks bit-exact reconstruction at ratio=1.
        for (int i = 0; i < GrainSize; i++)
        {
            _hann[i] = 0.5f * (1f - MathF.Cos(2f * MathF.PI * i / GrainSize));
        }
        Semitones = 0f;
    }

    /// <summary>Semitone offset, ±24. Setter is UI-thread; the audio
    /// thread sees the change on its next Read via the volatile ratio
    /// bit-pattern.</summary>
    public float Semitones
    {
        get => BitConverter.Int32BitsToSingle(_semitoneBits);
        set
        {
            float clamped = Math.Clamp(value, -24f, 24f);
            // Compute the ratio first, then publish semitones — audio
            // thread only reads _ratioBits, so it's the visible one.
            float ratio = MathF.Pow(2f, clamped / 12f);
            _ratioBits = BitConverter.SingleToInt32Bits(ratio);
            _semitoneBits = BitConverter.SingleToInt32Bits(clamped);
        }
    }

    public ISampleProvider CreateEffect(ISampleProvider source)
        => new Effect(source, this);

    public string SerializeConfig()
    {
        var s = Semitones;
        return "{\"semitones\":" + s.ToString("R", CultureInfo.InvariantCulture) + "}";
    }

    public void DeserializeConfig(string json)
    {
        if (string.IsNullOrWhiteSpace(json)) { Semitones = 0f; return; }
        try
        {
            using var doc = JsonDocument.Parse(json);
            // Guard on ValueKind == Number before TryGetSingle: the Try*
            // accessors throw InvalidOperationException (not JsonException)
            // when the element isn't a number, e.g. {"semitones":"oops"}.
            if (doc.RootElement.ValueKind == JsonValueKind.Object &&
                doc.RootElement.TryGetProperty("semitones", out var el) &&
                el.ValueKind == JsonValueKind.Number &&
                el.TryGetSingle(out float s))
            {
                Semitones = s;
            }
        }
        catch (JsonException)
        {
            // Malformed input — leave current value alone. Contract says
            // tolerate empty/malformed by falling back to defaults; the
            // ctor already set 0, and once a user has dragged the knob
            // we'd rather not clobber their setting on a transient
            // parse error.
        }
    }

    public object? CreateControl()
    {
        var label = new TextBlock
        {
            Text = FormatLabel(Semitones),
            Margin = new Avalonia.Thickness(0, 0, 0, 4),
        };

        var slider = new Slider
        {
            Minimum = -24,
            Maximum = 24,
            Value = Semitones,
            TickFrequency = 1,
            IsSnapToTickEnabled = false,
            SmallChange = 0.5,
            LargeChange = 1,
            Width = 260,
        };

        // PropertyChanged fires for every value change as the user drags;
        // the host's editor tick also calls DeserializeConfig in parallel,
        // but both write through the same atomic Semitones setter so the
        // worst case is a flicker, not a tear.
        slider.PropertyChanged += (_, e) =>
        {
            if (e.Property == Slider.ValueProperty)
            {
                float v = (float)slider.Value;
                Semitones = v;
                label.Text = FormatLabel(v);
            }
        };

        var panel = new StackPanel
        {
            Orientation = Orientation.Vertical,
            Spacing = 2,
        };
        panel.Children.Add(label);
        panel.Children.Add(slider);
        return panel;
    }

    private static string FormatLabel(float semitones)
    {
        if (Math.Abs(semitones) < 0.05f) return "0 semitones (no shift)";
        string sign = semitones > 0 ? "+" : "";
        return $"{sign}{semitones.ToString("0.0", CultureInfo.InvariantCulture)} semitones";
    }

    public void Dispose() { }

    /// <summary>
    /// The actual ISampleProvider wrapped around the upstream source.
    /// Per-attachment instance state lives on the enclosing
    /// <see cref="PitchShiftInstance"/> so config edits reach the audio
    /// thread immediately; the grain pointers and ring write head live
    /// here because they're audio-thread-only.
    /// </summary>
    private sealed class Effect : ISampleProvider
    {
        private readonly ISampleProvider _source;
        private readonly PitchShiftInstance _owner;
        private readonly int _channels;

        // Ring write head (audio thread only).
        private int _writePos;

        // Two grains, staggered by HopSize so their Hann envelopes sum
        // to 1 at every output sample. *Local* is the current sample
        // index within the grain's envelope (advances by 1 per output
        // frame). *ReadPos* is the (fractional) position in the ring
        // where the grain is currently reading from (advances by the
        // pitch ratio per output frame, with linear interpolation
        // between integer samples).
        private int _grainALocal;
        private float _grainAReadPos;
        private int _grainBLocal;
        private float _grainBReadPos;

        public Effect(ISampleProvider source, PitchShiftInstance owner)
        {
            _source = source;
            _owner = owner;
            _channels = source.WaveFormat.Channels;
            // Stagger so the Hann windows sum to unity from the very
            // first sample. Grain A starts at the beginning of its
            // envelope (hann[0]=0), grain B starts at the peak
            // (hann[HopSize]=1).
            _grainALocal = 0;
            _grainBLocal = HopSize;
            _grainAReadPos = 0f;
            _grainBReadPos = 0f;
        }

        public WaveFormat WaveFormat => _source.WaveFormat;

        public int Read(float[] buffer, int offset, int count)
        {
            // Pull the upstream first into the output buffer in-place;
            // we overwrite each frame as we process it. No allocations
            // on this path.
            int read = _source.Read(buffer, offset, count);
            if (read == 0) return 0;

            float ratio = BitConverter.Int32BitsToSingle(_owner._ratioBits);

            // Local copies of mutable state so the JIT can keep them in
            // registers across the hot loop.
            var ringL = _owner._ringL;
            var ringR = _owner._ringR;
            var hann = _owner._hann;
            int writePos = _writePos;
            int aLocal = _grainALocal;
            float aReadPos = _grainAReadPos;
            int bLocal = _grainBLocal;
            float bReadPos = _grainBReadPos;
            int channels = _channels;

            int frames = read / channels;

            for (int i = 0; i < frames; i++)
            {
                int frameBase = offset + i * channels;

                // De-interleave one frame into ring channels. For mono
                // the right channel mirrors left so the per-grain sampling
                // below doesn't branch on channel count.
                float inL = buffer[frameBase];
                float inR = channels == 2 ? buffer[frameBase + 1] : inL;
                ringL[writePos] = inL;
                ringR[writePos] = inR;

                // Sample both grains with linear interpolation between
                // adjacent ring entries. Casting a positive float to int
                // truncates toward zero, which matches floor() since
                // grain read positions are always wrapped into
                // [0, RingSize). Negative values can't occur — wrapping
                // below adds RingSize before any subsequent advance.
                int a0 = (int)aReadPos & RingMask;
                int a1 = (a0 + 1) & RingMask;
                float aFrac = aReadPos - MathF.Floor(aReadPos);
                float aL = ringL[a0] + aFrac * (ringL[a1] - ringL[a0]);
                float aR = ringR[a0] + aFrac * (ringR[a1] - ringR[a0]);
                float aWin = hann[aLocal];

                int b0 = (int)bReadPos & RingMask;
                int b1 = (b0 + 1) & RingMask;
                float bFrac = bReadPos - MathF.Floor(bReadPos);
                float bL = ringL[b0] + bFrac * (ringL[b1] - ringL[b0]);
                float bR = ringR[b0] + bFrac * (ringR[b1] - ringR[b0]);
                float bWin = hann[bLocal];

                float outL = aL * aWin + bL * bWin;
                float outR = aR * aWin + bR * bWin;

                buffer[frameBase] = outL;
                if (channels == 2) buffer[frameBase + 1] = outR;
                // Mono falls through — left already written.

                // Advance state. Grain read pointers step by `ratio`
                // (so ratio>1 reads faster than write → pitch up;
                // ratio<1 reads slower → pitch down). Local indices
                // step by 1 per output frame; when they hit GrainSize
                // we restart the grain at a position behind the write
                // head, far enough back that the entire upcoming grain
                // reads from past data the ring already holds.
                aReadPos += ratio;
                bReadPos += ratio;
                if (aReadPos >= RingSize) aReadPos -= RingSize;
                if (bReadPos >= RingSize) bReadPos -= RingSize;

                aLocal++;
                bLocal++;
                writePos = (writePos + 1) & RingMask;

                if (aLocal >= GrainSize)
                {
                    aLocal = 0;
                    aReadPos = RestartReadPos(writePos, ratio);
                }
                if (bLocal >= GrainSize)
                {
                    bLocal = 0;
                    bReadPos = RestartReadPos(writePos, ratio);
                }
            }

            // Publish back to the instance fields.
            _writePos = writePos;
            _grainALocal = aLocal;
            _grainAReadPos = aReadPos;
            _grainBLocal = bLocal;
            _grainBReadPos = bReadPos;

            return read;
        }

        // Place a freshly-restarted grain so it can read its full
        // ratio*GrainSize span without overrunning the write head.
        // For ratio ≥ 1 (pitch up): grain consumes ratio*GrainSize
        // input samples but the write head only advances GrainSize
        // during the grain's lifetime, so we start the read pointer
        // ratio*GrainSize samples behind the write head; by the end of
        // the grain the read pointer has just caught up to the *new*
        // write head, never overtaking.
        // For ratio < 1 (pitch down): the read pointer falls behind
        // the write head over the grain's life, so starting at
        // writePos - GrainSize keeps the audio reasonably fresh.
        private static float RestartReadPos(int writePos, float ratio)
        {
            float lookback = ratio > 1f ? ratio * GrainSize : GrainSize;
            float pos = writePos - lookback;
            // Wrap into [0, RingSize). lookback ≤ 4*GrainSize = 9600
            // and writePos ∈ [0, RingSize), so a single add suffices.
            if (pos < 0f) pos += RingSize;
            return pos;
        }
    }
}
