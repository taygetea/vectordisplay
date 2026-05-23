// Separable Gaussian blur for bloom effect.
// Two passes: horizontal then vertical.
// Uses 9-tap kernel for good quality/performance balance.

struct BloomUniforms {
    direction: vec2<f32>,  // (1/w, 0) for horizontal, (0, 1/h) for vertical
    _pad: vec2<f32>,
};

@group(0) @binding(0) var src_texture: texture_2d<f32>;
@group(0) @binding(1) var src_sampler: sampler;
@group(0) @binding(2) var<uniform> params: BloomUniforms;

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
    // 9-tap Gaussian kernel (sigma ~= 2.5)
    // Weights: precomputed and normalized
    let w0 = 0.227027;
    let w1 = 0.194596;
    let w2 = 0.121597;
    let w3 = 0.054054;
    let w4 = 0.016216;

    var result = textureSample(src_texture, src_sampler, in.uv) * w0;

    let step = params.direction;

    result += textureSample(src_texture, src_sampler, in.uv + step * 1.0) * w1;
    result += textureSample(src_texture, src_sampler, in.uv - step * 1.0) * w1;
    result += textureSample(src_texture, src_sampler, in.uv + step * 2.0) * w2;
    result += textureSample(src_texture, src_sampler, in.uv - step * 2.0) * w2;
    result += textureSample(src_texture, src_sampler, in.uv + step * 3.0) * w3;
    result += textureSample(src_texture, src_sampler, in.uv - step * 3.0) * w3;
    result += textureSample(src_texture, src_sampler, in.uv + step * 4.0) * w4;
    result += textureSample(src_texture, src_sampler, in.uv - step * 4.0) * w4;

    return result;
}
