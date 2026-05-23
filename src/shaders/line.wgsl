// Line segment rendering with gaussian beam profile.
// Each instance is a line segment; the vertex shader expands a unit quad
// to cover the segment plus gaussian padding.

struct Uniforms {
    viewport_size: vec2<f32>,  // window size in pixels
    beam_width: f32,           // sigma of gaussian beam in NDC
    phosphor_tc: f32,          // phosphor time constant in seconds
};

@group(0) @binding(0) var<uniform> uniforms: Uniforms;

struct LineInstance {
    @location(0) start: vec2<f32>,
    @location(1) end: vec2<f32>,
    @location(2) intensity: f32,
    @location(3) time_offset: f32,
};

struct VertexOutput {
    @builtin(position) position: vec4<f32>,
    @location(0) local_pos: vec2<f32>,   // position in segment-local coords
    @location(1) half_length: f32,       // half the segment length
    @location(2) intensity: f32,
    @location(3) time_offset: f32,
};

// Unit quad: 2 triangles covering [-1,1] x [-1,1]
// Vertices generated from vertex_index
fn quad_pos(idx: u32) -> vec2<f32> {
    // 0: (-1,-1), 1: (1,-1), 2: (-1,1), 3: (1,-1), 4: (1,1), 5: (-1,1)
    let x = select(-1.0, 1.0, idx == 1u || idx == 3u || idx == 4u);
    let y = select(-1.0, 1.0, idx == 2u || idx == 4u || idx == 5u);
    return vec2<f32>(x, y);
}

@vertex
fn vs_main(
    @builtin(vertex_index) vertex_index: u32,
    instance: LineInstance,
) -> VertexOutput {
    let quad = quad_pos(vertex_index);

    let dir = instance.end - instance.start;
    let seg_length = length(dir);
    let half_len = seg_length * 0.5;

    // Padding: 4 sigma ensures gaussian tail is negligible
    let padding = uniforms.beam_width * 4.0;

    // Build local coordinate frame
    let tangent = select(vec2<f32>(1.0, 0.0), dir / seg_length, seg_length > 1e-6);
    let normal = vec2<f32>(-tangent.y, tangent.x);

    let center = (instance.start + instance.end) * 0.5;

    // Expand quad: along tangent by (half_length + padding), along normal by padding
    let local_x = quad.x * (half_len + padding);
    let local_y = quad.y * padding;

    let world_pos = center + tangent * local_x + normal * local_y;

    var out: VertexOutput;
    out.position = vec4<f32>(world_pos, 0.0, 1.0);
    out.local_pos = vec2<f32>(local_x, local_y);
    out.half_length = half_len;
    out.intensity = instance.intensity;
    out.time_offset = instance.time_offset;
    return out;
}

// Abramowitz & Stegun erf approximation (max error ~1.5e-7)
fn erf_approx(x: f32) -> f32 {
    let a = abs(x);
    let t = 1.0 / (1.0 + 0.3275911 * a);
    let t2 = t * t;
    let t3 = t2 * t;
    let t4 = t3 * t;
    let t5 = t4 * t;
    let poly = 0.254829592 * t
             - 0.284496736 * t2
             + 1.421413741 * t3
             - 1.453152027 * t4
             + 1.061405429 * t5;
    let result = 1.0 - poly * exp(-a * a);
    return select(-result, result, x >= 0.0);
}

@fragment
fn fs_main(in: VertexOutput) -> @location(0) vec4<f32> {
    let sigma = uniforms.beam_width;
    let inv_sigma = 1.0 / sigma;

    // Perpendicular distance: gaussian falloff
    let perp_gauss = exp(-0.5 * in.local_pos.y * in.local_pos.y * inv_sigma * inv_sigma);

    // Along-segment: integrated gaussian (erf) gives smooth endpoints
    let x = in.local_pos.x;
    let half_l = in.half_length;

    // Integral of gaussian from -half_l to +half_l, evaluated at our x position
    let arg_plus = (half_l - x) * inv_sigma * 0.7071068;  // 1/sqrt(2)
    let arg_minus = (-half_l - x) * inv_sigma * 0.7071068;
    let along = 0.5 * (erf_approx(arg_plus) - erf_approx(arg_minus));

    // Brightness: intensity / (2 * segment_length) gives physical beam behavior
    // Shorter segments = brighter (beam moves slower)
    let seg_length = 2.0 * half_l;
    let brightness_factor = select(1.0 / (2.0 * seg_length), 10.0, seg_length < 0.001);

    let brightness = in.intensity * brightness_factor * perp_gauss * along;

    // Intra-frame phosphor decay: lines drawn earlier in the refresh have
    // been decaying longer. This is the real temporal behavior of the beam.
    let phosphor_decay = exp(-in.time_offset / uniforms.phosphor_tc);

    let final_brightness = brightness * phosphor_decay;

    // P31 green phosphor color will be applied in composite pass;
    // here we output monochrome intensity.
    return vec4<f32>(final_brightness, final_brightness, final_brightness, 1.0);
}
