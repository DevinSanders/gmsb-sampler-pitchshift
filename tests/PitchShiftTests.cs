using System.Globalization;
using System.Text.Json;
using FluentAssertions;
using NAudio.Wave;
using SoundBoard.PluginApi;
using Xunit;

// The plugin's namespace and its entry type share the name "PitchShiftPlugin";
// alias the type so test code reads unambiguously.
using PitchShiftFactory = PitchShiftPlugin.PitchShiftPlugin;

namespace PitchShiftTests;

// Stateful-effect bypass caveat: the pitch shifter owns ring buffers and
// overlap-add grain state. The host wraps this in BypassableSamplerInstance,
// which freezes the wet chain while bypassed — so on un-bypass you'll briefly
// hear stale grains from when bypass engaged. The tests below exercise the
// wet DSP directly via CreateEffect (bypassing the wrapper); the freeze is a
// host-tier concern documented in the main app's CLAUDE.md §"FX Chain v1
// limitations."

/// <summary>
/// A deterministic <see cref="ISampleProvider"/>: every sample is produced
/// by <paramref name="fn"/> from a monotonically increasing interleaved
/// sample index. For stereo, index 0 = frame0-L, 1 = frame0-R, 2 = frame1-L…
/// </summary>
sealed class SignalProvider(WaveFormat fmt, Func<long, float> fn) : ISampleProvider
{
    long _n;
    public WaveFormat WaveFormat => fmt;
    public int Read(float[] buf, int off, int count)
    {
        for (int i = 0; i < count; i++) buf[off + i] = fn(_n++);
        return count;
    }
}

public class PitchShiftTests
{
    static readonly WaveFormat Fmt = WaveFormat.CreateIeeeFloatWaveFormat(48000, 2);

    // Mirrors the private const in the plugin; the steady-state delay at
    // ratio=1 is exactly this many frames.
    const int GrainSize = 2400;

    // ── Helpers ──────────────────────────────────────────────────────────

    static ISamplerInstance NewInstance(float semitones)
    {
        var inst = new PitchShiftFactory().CreateInstance();
        inst.DeserializeConfig(
            "{\"semitones\":" + semitones.ToString(CultureInfo.InvariantCulture) + "}");
        return inst;
    }

    static float ExtractSemitones(string json)
    {
        using var doc = JsonDocument.Parse(json);
        return doc.RootElement.GetProperty("semitones").GetSingle();
    }

    // Run `frames` stereo frames of `fn` through the instance's effect and
    // return the interleaved output buffer.
    static float[] RunEffect(ISamplerInstance inst, Func<long, float> fn, int frames)
    {
        var fx = inst.CreateEffect(new SignalProvider(Fmt, fn));
        var buf = new float[frames * 2];
        int total = 0;
        while (total < buf.Length)
        {
            int got = fx.Read(buf, total, buf.Length - total);
            if (got == 0) break;
            total += got;
        }
        return buf;
    }

    static float[] GenerateInterleaved(Func<long, float> fn, int frames)
    {
        var buf = new float[frames * 2];
        for (int i = 0; i < buf.Length; i++) buf[i] = fn(i);
        return buf;
    }

    // Count sign changes on the left channel (even sample indices) between
    // two frame bounds — a cheap fundamental-frequency proxy.
    static int CountZeroCrossingsLeft(float[] interleaved, int startFrame, int endFrame)
    {
        int count = 0;
        float prev = interleaved[startFrame * 2];
        for (int f = startFrame + 1; f < endFrame; f++)
        {
            float cur = interleaved[f * 2];
            if ((prev < 0f) != (cur < 0f)) count++;
            prev = cur;
        }
        return count;
    }

    // A 220 Hz tone, identical on both channels of each frame.
    static float Tone220(long interleavedIndex)
        => MathF.Sin(2f * MathF.PI * 220f * (interleavedIndex / 2) / 48000f);

    // ── Plugin metadata ──────────────────────────────────────────────────

    [Fact]
    public void Plugin_metadata_is_populated()
    {
        var p = new PitchShiftFactory();
        p.Id.Should().Be("sampler.pitchshift");
        p.Name.Should().Be("Pitch Shift");
        p.Author.Should().NotBeNullOrWhiteSpace();
        // Version is read from the assembly (PluginVersion.OfAssembly), not a
        // literal — just assert it resolved to something non-empty.
        p.Version.Should().NotBeNullOrWhiteSpace();
        p.SupportedAttachments.Should().Be(SamplerAttachmentPoints.All);
    }

    // ── Factory isolation ────────────────────────────────────────────────

    [Fact]
    public void Factory_produces_independent_instances()
    {
        var plugin = new PitchShiftFactory();
        var a = plugin.CreateInstance();
        var b = plugin.CreateInstance();

        a.Should().NotBeSameAs(b);

        // Configuring one must not bleed into the other.
        a.DeserializeConfig("{\"semitones\":7}");
        b.DeserializeConfig("{\"semitones\":-5}");
        a.SerializeConfig().Should().NotBe(b.SerializeConfig());
        ExtractSemitones(a.SerializeConfig()).Should().BeApproximately(7f, 1e-3f);
        ExtractSemitones(b.SerializeConfig()).Should().BeApproximately(-5f, 1e-3f);
    }

    // ── Config round-trip ────────────────────────────────────────────────

    [Theory]
    [InlineData(0)]
    [InlineData(7)]
    [InlineData(-12.5)]
    [InlineData(24)]
    [InlineData(-24)]
    public void Config_round_trips(double semitones)
    {
        var first = NewInstance((float)semitones);
        var json = first.SerializeConfig();

        var second = new PitchShiftFactory().CreateInstance();
        second.DeserializeConfig(json);

        second.SerializeConfig().Should().Be(json);
        ExtractSemitones(json).Should().BeApproximately((float)semitones, 1e-3f);
    }

    [Theory]
    [InlineData(100, 24)]
    [InlineData(-100, -24)]
    [InlineData(24.5, 24)]
    public void Out_of_range_values_clamp(double input, float expected)
    {
        var inst = NewInstance((float)input);
        ExtractSemitones(inst.SerializeConfig()).Should().BeApproximately(expected, 1e-3f);
    }

    [Theory]
    [InlineData("")]
    [InlineData("   ")]
    [InlineData("not json at all")]
    [InlineData("{}")]
    [InlineData("{\"semitones\":\"oops\"}")]
    [InlineData("[1,2,3]")]
    [InlineData("null")]
    public void Malformed_config_neither_throws_nor_breaks_audio(string json)
    {
        var inst = new PitchShiftFactory().CreateInstance();

        inst.Invoking(i => i.DeserializeConfig(json)).Should().NotThrow();

        // The audio path must keep working after bad config.
        var fx = inst.CreateEffect(new SignalProvider(Fmt, _ => 0.1f));
        var buf = new float[960];
        inst.Invoking(_ => fx.Read(buf, 0, buf.Length)).Should().NotThrow();
    }

    // ── Format preservation ──────────────────────────────────────────────

    [Fact]
    public void Effect_preserves_source_waveformat()
    {
        var inst = new PitchShiftFactory().CreateInstance();
        var src = new SignalProvider(Fmt, _ => 0f);
        var fx = inst.CreateEffect(src);

        fx.WaveFormat.SampleRate.Should().Be(48000);
        fx.WaveFormat.Channels.Should().Be(2);
        fx.WaveFormat.Encoding.Should().Be(WaveFormatEncoding.IeeeFloat);
    }

    // ── DSP behaviour ────────────────────────────────────────────────────

    [Fact]
    public void At_zero_semitones_output_equals_input_delayed_by_one_grain()
    {
        const int frames = 24000;
        var inst = NewInstance(0f);

        var input = GenerateInterleaved(Tone220, frames);
        var output = RunEffect(inst, Tone220, frames);

        // At ratio=1 the read pointers land on integer positions, so the
        // linear interpolation is exact: output[f] == input[f - GrainSize].
        // Compare past the warmup (both grains locked after ~2 grains).
        int delaySamples = GrainSize * 2; // frames → interleaved samples
        float maxErr = 0f;
        for (int i = 2 * delaySamples; i < output.Length; i++)
            maxErr = MathF.Max(maxErr, MathF.Abs(output[i] - input[i - delaySamples]));

        maxErr.Should().BeLessThan(1e-4f);
    }

    [Fact]
    public void Pitch_up_roughly_doubles_the_fundamental()
    {
        const int frames = 48000;
        const int analysisStart = 6000; // past 2-grain warmup (4800 frames)

        var input = GenerateInterleaved(Tone220, frames);
        int zcIn = CountZeroCrossingsLeft(input, analysisStart, frames);

        var up = RunEffect(NewInstance(12f), Tone220, frames); // ratio 2.0
        int zcUp = CountZeroCrossingsLeft(up, analysisStart, frames);

        ((double)zcUp / zcIn).Should().BeInRange(1.7, 2.3);
    }

    [Fact]
    public void Pitch_down_roughly_halves_the_fundamental()
    {
        const int frames = 48000;
        const int analysisStart = 6000;

        var input = GenerateInterleaved(Tone220, frames);
        int zcIn = CountZeroCrossingsLeft(input, analysisStart, frames);

        var down = RunEffect(NewInstance(-12f), Tone220, frames); // ratio 0.5
        int zcDown = CountZeroCrossingsLeft(down, analysisStart, frames);

        ((double)zcDown / zcIn).Should().BeInRange(0.35, 0.65);
    }

    [Fact]
    public void Produces_no_nan_or_infinity_at_extremes()
    {
        foreach (var s in new[] { -24f, -7f, 0f, 7f, 24f })
        {
            var output = RunEffect(NewInstance(s), Tone220, 12000);
            output.Should().OnlyContain(v => !float.IsNaN(v) && !float.IsInfinity(v),
                $"semitones={s} must stay finite");
        }
    }

    // ── Live-config thread-safety smoke ──────────────────────────────────

    [Fact]
    public void Concurrent_deserialize_during_read_does_not_throw()
    {
        var inst = new PitchShiftFactory().CreateInstance();
        var fx = inst.CreateEffect(new SignalProvider(Fmt, n => 0.2f * MathF.Sin(n * 0.01f)));

        var rnd = new Random(1234);
        Exception? writerEx = null;
        bool stop = false;

        var writer = new Thread(() =>
        {
            try
            {
                while (!Volatile.Read(ref stop))
                {
                    inst.DeserializeConfig(
                        "{\"semitones\":" + rnd.Next(-24, 25) + "}");
                }
            }
            catch (Exception e) { writerEx = e; }
        });

        writer.Start();
        var buf = new float[960];
        Action readLoop = () =>
        {
            for (int i = 0; i < 5000; i++) fx.Read(buf, 0, buf.Length);
        };

        readLoop.Should().NotThrow();

        Volatile.Write(ref stop, true);
        writer.Join();
        writerEx.Should().BeNull();
    }
}
