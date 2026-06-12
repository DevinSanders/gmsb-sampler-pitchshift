# gmsb-sampler-pitchshift

Pitch-shift FX plugin for
[Game Master Sound Board](https://github.com/DevinSanders/game-master-soundboard).
±24 semitones, speed-independent.

Implemented as a two-grain overlap-add granular shifter (50 ms grains,
50% overlap, Hann-windowed). Audio-thread allocation-free; ~50 ms
fixed latency.

## Install

**Paid plugin.** The source is open here for reference, but the pre-built
binary is distributed pay-what-you-want on itch.io:

**→ https://dsand64.itch.io/gmsb-sampler-pitchshift**

Download the `.zip` from that page and drop it onto **Settings → Plugin
Manager** in Game Master Sound Board. Restart when prompted, then enable it under **Settings → Plugins**.

## Manifest

| Field     | Value                       |
|-----------|-----------------------------|
| publisher | `github.DevinSanders`       |
| id        | `sampler.pitchshift`        |
| entryDll  | `PitchShiftPlugin.dll`      |

## Bypass behavior

This plugin holds DSP state (grain ring + lookback). The host's BypassableSamplerInstance wrapper toggles bypass by flipping a flag — it does NOT rebuild the chain. While bypassed the wet path isn't clocked, so on un-bypass you'll briefly hear stale material from the moment bypass engaged. For an interactive soundboard with on-demand FX this is fine; for a long-tail stateful effect (large hall reverb, slow chorus) the artifact is more noticeable. Workaround: leave bypass off and use the wet/dry knob (or the plugin's own dry/wet mix where exposed) to mute the effect contribution.

## License

Released under the [MIT License](LICENSE).

Third-party components used by this plugin:

- No third-party DSP — original granular overlap-add implementation.