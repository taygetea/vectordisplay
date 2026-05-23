# Vector Display Simulator

## What This Is

GPU-driven simulator of classic vector CRT displays (HP 1345A-style) in Rust + wgpu. The goal is physical accuracy — simulate what the beam and phosphor actually do, don't fake it with post-processing. The real displays looked good because the physics looked good.

## Design Philosophy

**Simulate, don't fake.** Use the speed of modern computers to model the actual beam and phosphor physics:

- The beam sweeps sequentially through vectors, taking real time. Lines drawn early in the refresh have been decaying longer than lines drawn late. This emerges naturally from `beam_speed` and `phosphor_tc`.
- Phosphor absorption follows capacitor-charging physics — absorption decreases as excitation approaches saturation. No hard clamps.
- Brightness, flicker, and persistence trails are all emergent from physical parameters, not tuned aesthetic constants.
- Bloom is subtle (0.10) — the phosphor glow IS the visual, the bloom just catches the halo you'd see from light scatter in the glass.

## Current Status: WORKING on Windows

Running natively on Windows with Vulkan backend on RTX 3070. Stable, no crashes. Originally developed on WSL2 (extremely unstable GL backend), now fully migrated.

### Building

Rust is installed via scoop (not rustup — rustup was removed due to shim conflicts). Rust 1.93, wgpu 25.

```
cargo build --release
cargo run --release
```

### Network Architecture

The display is a TCP server (port 5001 by default). External programs connect and send beam commands. When no client is connected, it shows a built-in Lissajous demo. On disconnect, falls back to demo.

```
[Content Generator]  --TCP-->  [Display Server :5001]
 (any language)                 (Rust + wgpu)
```

Wire protocol (little-endian): `[u32 payload_byte_length][commands...]`
- MoveTo: `[0u8][f32 x][f32 y]` (9 bytes)
- DrawTo: `[1u8][f32 x][f32 y][f32 intensity]` (13 bytes)

Python example:
```python
import struct, socket
s = socket.create_connection(('localhost', 5001))
payload = struct.pack('<Bff', 0, 0.0, 0.0)        # MoveTo
payload += struct.pack('<Bfff', 1, 0.5, 0.5, 1.0)  # DrawTo
s.send(struct.pack('<I', len(payload)) + payload)
```

## Architecture

### GPU pipeline per frame

```
1. LINE PASS:      Render beam energy → excitation_texture (additive blend for overlapping lines)
2. MERGE PASS:     Read persistence[src] + excitation → persistence[dst]
                   (phosphor decay + capacitor-model absorption)
3. BLOOM H PASS:   Read persistence[dst] → bloom_a (horizontal gaussian blur, half res)
4. BLOOM V PASS:   Read bloom_a → bloom_b (vertical gaussian blur)
5. COMPOSITE PASS: Read persistence[dst] + bloom_b → swapchain (tone mapping + P31 tint)
6. Swap ping-pong indices
```

Key: lines do NOT write directly to persistence. They write to a separate excitation texture, and the merge pass combines excitation with decayed persistence using physics-based absorption. This prevents brightness accumulation and models real phosphor saturation.

### Merge pass physics (decay.wgsl)

```
decayed = persistence[src] * exp(-dt / phosphor_tc)
absorption = max(1.0 - decayed / phosphor_max, 0.0)
persistence[dst] = decayed + excitation * absorption
```

This is the capacitor-charging model: cold phosphor absorbs all beam energy, saturated phosphor absorbs none. Steady-state brightness converges naturally below phosphor_max.

### Intra-frame beam timing (line.wgsl)

Each LineInstance carries a `time_offset` — how long ago (in seconds) the beam drew this line within the current refresh cycle. Computed from beam_speed and path length in resolve_commands(). The fragment shader applies `exp(-time_offset / phosphor_tc)` to the beam energy.

Effect: with many vectors or slow beam speed, early lines visibly dim relative to late ones. This is how real vector displays flicker when overloaded — it's emergent, not faked.

### Key design decisions

- **Rgba16Float throughout** — HDR for additive blending + multi-frame persistence
- **Ping-pong textures** — wgpu can't read+write same texture in one pass
- **Separate excitation texture** — decouples beam energy from persistence, enabling physics-correct absorption
- **Instanced quads** — each line segment = 1 GPU instance, 6 vertices per quad
- **erf() in WGSL** — Abramowitz & Stegun approximation for gaussian beam profile
- **Pre-created bind groups** — all ping-pong variants built at init, zero per-frame GPU allocation

### File structure

```
src/
  main.rs          -- winit event loop, wgpu init, frame orchestration, TCP integration
  renderer.rs      -- VectorRenderer: all GPU state, pipelines, per-frame rendering
  beam.rs          -- BeamCommand, LineInstance (with time_offset), ContentProvider trait,
                      resolve_commands (computes beam timing), wire protocol parser
  demo.rs          -- LissajousDemo + TestPattern content providers
  server.rs        -- TCP server: accepts connections, parses wire protocol, sends to main via mpsc
  shaders/
    line.wgsl      -- Instanced quad expansion + gaussian beam + intra-frame phosphor decay
    decay.wgsl     -- Merge pass: phosphor decay + capacitor-model excitation absorption
    bloom.wgsl     -- Separable 9-tap Gaussian blur (fullscreen triangle)
    composite.wgsl -- Persistence + bloom → screen with Reinhard tone mapping + P31 green tint
```

### Keyboard controls

- `w`/`s` — beam width (wider/narrower gaussian sigma)
- `a`/`d` — phosphor time constant (shorter = crisper, longer = more persistence)
- `r`/`f` — beam speed (faster/slower — affects flicker on complex scenes)
- `q`/`e` — bloom strength
- `Esc` — quit

Parameters shown in window title bar.

## Render Parameters (runtime adjustable)

```rust
pub struct RenderParams {
    pub beam_width: f32,       // gaussian sigma in NDC (default 0.0013)
    pub beam_speed: f32,       // NDC/s beam deflection rate (default 3230.0)
    pub phosphor_tc: f32,      // phosphor time constant in seconds (default 0.0323)
    pub phosphor_max: f32,     // saturation level (default 3.0)
    pub bloom_strength: f32,   // additive bloom mix (default 0.10)
    pub phosphor_color: [f32; 3], // P31 green (default [0.2, 1.0, 0.3])
}
```

## Content Provider Interface

Implement `ContentProvider` in `beam.rs`:

```rust
pub trait ContentProvider {
    fn update(&mut self, time: f64, dt: f64) -> Vec<BeamCommand>;
}
```

`BeamCommand::MoveTo` blanks the beam, `BeamCommand::DrawTo` draws a line from current position. `resolve_commands()` converts these into `LineInstance` structs with beam timing and sends them to the GPU.

## WGSL/wgpu Gotchas

1. **WGSL `vec3<f32>` alignment** — 16-byte alignment in uniform buffers, NOT 12. Use 3 separate f32 fields if your Rust struct uses `[f32; 3]`.

2. **Blend state per-pass** — Lines need additive (`One + One`) on the excitation texture. All other passes use replace. Getting this wrong silently produces wrong output.

3. **Fullscreen triangle trick** — Generate 3 vertices from `vertex_index` (no vertex buffer): `uv = vec2(f32((idx << 1) & 2), f32(idx & 2))`, `pos = uv * 2.0 - 1.0`.
