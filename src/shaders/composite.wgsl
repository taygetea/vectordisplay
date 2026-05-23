// Final compositing pass.
// Combines the persistence buffer with bloom, applies P31 phosphor tinting
// and simple tone mapping, outputs to the swapchain.

struct CompositeUniforms {
    phosphor_color: vec3<f32>,  // P31 green: (0.2, 1.0, 0.3) approx
    bloom_strength: f32,
};

@group(0) @binding(0) var persistence_tex: texture_2d<f32>;
@group(0) @binding(1) var bloom_tex: texture_2d<f32>;
@group(0) @binding(2) var tex_sampler: sampler;
@group(0) @binding(3) var<uniform> params: CompositeUniforms;

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) uv: vec2<f32>,
};

@vertex
fn vs_main(@builtin(vertex_index) idx: u32) -> VertexOutput {
    let uv = vec2<f32>(
        f32((idx << 1u) & 2u),
        f32(idx & 2u)
    );
    var out: VertexOutput;
    out.position = vec4<f32>(uv * 2.0 - 1.0, 0.0, 1.0);
    out.uv = vec2<f32>(uv.x, 1.0 - uv.y);
    return out;
}

@fragment
fn fs_main(in: VertexOutput) -> @location(0) vec4<f32> {
    let persistence = textureSample(persistence_tex, tex_sampler, in.uv).rgb;
    let bloom = textureSample(bloom_tex, tex_sampler, in.uv).rgb;

    let combined = persistence + bloom * params.bloom_strength;

    // Apply phosphor color tint
    let tinted = combined * params.phosphor_color;

    // Simple Reinhard tone mapping to bring HDR into displayable range
    let mapped = tinted / (1.0 + tinted);

    return vec4<f32>(mapped, 1.0);
}
