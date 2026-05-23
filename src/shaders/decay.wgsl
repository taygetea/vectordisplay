// Phosphor decay + merge pass.
//
// Models real phosphor physics: the beam deposits energy into the phosphor,
// but absorption decreases as the phosphor approaches saturation (like charging
// a capacitor). This prevents runaway brightness accumulation while staying
// physically motivated.
//
// Reads: persistence[src] (previous frame) + excitation (this frame's beam energy)
// Writes: persistence[dst]

struct MergeUniforms {
    decay_factor: f32,    // exp(-dt / phosphor_tc), pre-computed on CPU
    phosphor_max: f32,    // saturation level of the phosphor
    _pad0: f32,
    _pad1: f32,
};

@group(0) @binding(0) var persistence_texture: texture_2d<f32>;
@group(0) @binding(1) var excitation_texture: texture_2d<f32>;
@group(0) @binding(2) var tex_sampler: sampler;
@group(0) @binding(3) var<uniform> params: MergeUniforms;

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

// Fullscreen triangle from vertex index (no vertex buffer needed)
@vertex
fn vs_main(@builtin(vertex_index) idx: u32) -> VertexOutput {
    let uv = vec2<f32>(
        f32((idx << 1u) & 2u),
        f32(idx & 2u)
    );
    var out: VertexOutput;
    out.position = vec4<f32>(uv * 2.0 - 1.0, 0.0, 1.0);
    out.uv = vec2<f32>(uv.x, 1.0 - uv.y);  // flip Y for texture coords
    return out;
}

@fragment
fn fs_main(in: VertexOutput) -> @location(0) vec4<f32> {
    let prev = textureSample(persistence_texture, tex_sampler, in.uv).rgb;
    let excitation = textureSample(excitation_texture, tex_sampler, in.uv).rgb;

    // Decay the previous frame's phosphor excitation
    let decayed = prev * params.decay_factor;

    // Phosphor absorption: decreases as excitation approaches saturation
    let absorption = max(vec3<f32>(1.0) - decayed / params.phosphor_max, vec3<f32>(0.0));

    // Merge: decayed persistence + new beam energy attenuated by absorption
    let merged = decayed + excitation * absorption;

    return vec4<f32>(merged, 1.0);
}
