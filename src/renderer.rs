/// VectorRenderer: manages all GPU state, pipelines, textures, and per-frame rendering.
use crate::beam::LineInstance;

const MAX_INSTANCES: u64 = 8192;
const HDR_FORMAT: wgpu::TextureFormat = wgpu::TextureFormat::Rgba16Float;

// Uniform structs matching WGSL layouts
#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct LineUniforms {
    viewport_size: [f32; 2],
    beam_width: f32,
    phosphor_tc: f32,
}

#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct MergeUniforms {
    decay_factor: f32,
    phosphor_max: f32,
    _pad: [f32; 2],
}

#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct BloomUniforms {
    direction: [f32; 2],
    _pad: [f32; 2],
}

#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
struct CompositeUniforms {
    phosphor_color: [f32; 3],
    bloom_strength: f32,
}

/// Rendering parameters adjustable at runtime.
pub struct RenderParams {
    pub beam_width: f32,
    pub beam_speed: f32,       // NDC units per second — beam deflection rate
    pub phosphor_tc: f32,      // phosphor time constant in seconds (P31 ≈ 0.013s)
    pub phosphor_max: f32,     // saturation level — phosphor can't be excited past this
    pub bloom_strength: f32,
    pub phosphor_color: [f32; 3],
}

impl Default for RenderParams {
    fn default() -> Self {
        Self {
            beam_width: 0.0013,
            beam_speed: 3230.0,
            phosphor_tc: 0.0323,
            phosphor_max: 3.0,
            bloom_strength: 0.10,
            phosphor_color: [0.2, 1.0, 0.3],  // P31 green
        }
    }
}

pub struct VectorRenderer {
    // Core GPU state (kept for potential future use in pipeline recreation)
    #[allow(dead_code)]
    surface_format: wgpu::TextureFormat,
    width: u32,
    height: u32,

    // Ping-pong persistence textures
    persistence_textures: [wgpu::Texture; 2],
    persistence_views: [wgpu::TextureView; 2],
    ping_pong_index: usize, // which texture is "current" (dst)

    // Excitation texture: beam energy for this frame (before merge with persistence)
    excitation_texture: wgpu::Texture,
    excitation_view: wgpu::TextureView,

    // Bloom textures (half resolution)
    bloom_texture_a: wgpu::Texture,
    bloom_view_a: wgpu::TextureView,
    bloom_texture_b: wgpu::Texture,
    bloom_view_b: wgpu::TextureView,

    // Samplers
    linear_sampler: wgpu::Sampler,

    // Line rendering pipeline
    line_pipeline: wgpu::RenderPipeline,
    instance_buffer: wgpu::Buffer,
    line_uniform_buffer: wgpu::Buffer,
    line_bind_group: wgpu::BindGroup,

    // Merge pipeline (decay + excitation absorption)
    merge_pipeline: wgpu::RenderPipeline,
    merge_uniform_buffer: wgpu::Buffer,
    merge_bind_group_layout: wgpu::BindGroupLayout,
    merge_bind_groups: [wgpu::BindGroup; 2], // one per source persistence texture

    // Bloom pipeline
    bloom_pipeline: wgpu::RenderPipeline,
    bloom_uniform_buffer_h: wgpu::Buffer,
    bloom_uniform_buffer_v: wgpu::Buffer,
    bloom_bind_group_layout: wgpu::BindGroupLayout,
    // Pre-created for both ping-pong states to avoid per-frame allocation
    bloom_bind_groups_h: [wgpu::BindGroup; 2], // [i] reads persistence[i]
    bloom_bind_group_v: wgpu::BindGroup, // reads bloom_a -> writes bloom_b

    // Composite pipeline
    composite_pipeline: wgpu::RenderPipeline,
    composite_uniform_buffer: wgpu::Buffer,
    composite_bind_group_layout: wgpu::BindGroupLayout,
    // Rebuilt per-frame since persistence target changes
    composite_bind_groups: [wgpu::BindGroup; 2],

    pub params: RenderParams,
}

impl VectorRenderer {
    pub fn new(
        device: &wgpu::Device,
        queue: &wgpu::Queue,
        surface_format: wgpu::TextureFormat,
        width: u32,
        height: u32,
    ) -> Self {
        let _ = queue; // used for initial uploads if needed

        let linear_sampler = device.create_sampler(&wgpu::SamplerDescriptor {
            label: Some("linear_sampler"),
            mag_filter: wgpu::FilterMode::Linear,
            min_filter: wgpu::FilterMode::Linear,
            ..Default::default()
        });

        // Create persistence textures (ping-pong)
        let persistence_textures = std::array::from_fn(|i| {
            create_hdr_texture(device, width, height, &format!("persistence_{i}"))
        });
        let persistence_views = std::array::from_fn(|i| {
            persistence_textures[i].create_view(&wgpu::TextureViewDescriptor::default())
        });

        // Excitation texture: beam energy for this frame
        let excitation_texture = create_hdr_texture(device, width, height, "excitation");
        let excitation_view = excitation_texture.create_view(&wgpu::TextureViewDescriptor::default());

        // Create bloom textures (half resolution)
        let bw = (width / 2).max(1);
        let bh = (height / 2).max(1);
        let bloom_texture_a = create_hdr_texture(device, bw, bh, "bloom_a");
        let bloom_view_a = bloom_texture_a.create_view(&wgpu::TextureViewDescriptor::default());
        let bloom_texture_b = create_hdr_texture(device, bw, bh, "bloom_b");
        let bloom_view_b = bloom_texture_b.create_view(&wgpu::TextureViewDescriptor::default());

        // Instance buffer for line segments
        let instance_buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("instance_buffer"),
            size: MAX_INSTANCES * std::mem::size_of::<LineInstance>() as u64,
            usage: wgpu::BufferUsages::VERTEX | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });

        let params = RenderParams::default();

        // === LINE PIPELINE ===
        let line_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("line_shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("shaders/line.wgsl").into()),
        });

        let line_uniform_buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("line_uniforms"),
            size: std::mem::size_of::<LineUniforms>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });

        let line_bind_group_layout =
            device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
                label: Some("line_bgl"),
                entries: &[wgpu::BindGroupLayoutEntry {
                    binding: 0,
                    visibility: wgpu::ShaderStages::VERTEX | wgpu::ShaderStages::FRAGMENT,
                    ty: wgpu::BindingType::Buffer {
                        ty: wgpu::BufferBindingType::Uniform,
                        has_dynamic_offset: false,
                        min_binding_size: None,
                    },
                    count: None,
                }],
            });

        let line_bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("line_bg"),
            layout: &line_bind_group_layout,
            entries: &[wgpu::BindGroupEntry {
                binding: 0,
                resource: line_uniform_buffer.as_entire_binding(),
            }],
        });

        let line_pipeline_layout =
            device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                label: Some("line_pipeline_layout"),
                bind_group_layouts: &[&line_bind_group_layout],
                push_constant_ranges: &[],
            });

        let line_pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("line_pipeline"),
            layout: Some(&line_pipeline_layout),
            vertex: wgpu::VertexState {
                module: &line_shader,
                entry_point: Some("vs_main"),
                buffers: &[wgpu::VertexBufferLayout {
                    array_stride: std::mem::size_of::<LineInstance>() as u64,
                    step_mode: wgpu::VertexStepMode::Instance,
                    attributes: &[
                        // start: vec2<f32>
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32x2,
                            offset: 0,
                            shader_location: 0,
                        },
                        // end: vec2<f32>
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32x2,
                            offset: 8,
                            shader_location: 1,
                        },
                        // intensity: f32
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32,
                            offset: 16,
                            shader_location: 2,
                        },
                        // time_offset: f32
                        wgpu::VertexAttribute {
                            format: wgpu::VertexFormat::Float32,
                            offset: 20,
                            shader_location: 3,
                        },
                    ],
                }],
                compilation_options: Default::default(),
            },
            fragment: Some(wgpu::FragmentState {
                module: &line_shader,
                entry_point: Some("fs_main"),
                targets: &[Some(wgpu::ColorTargetState {
                    format: HDR_FORMAT,
                    blend: Some(wgpu::BlendState {
                        color: wgpu::BlendComponent {
                            src_factor: wgpu::BlendFactor::One,
                            dst_factor: wgpu::BlendFactor::One,
                            operation: wgpu::BlendOperation::Add,
                        },
                        alpha: wgpu::BlendComponent::REPLACE,
                    }),
                    write_mask: wgpu::ColorWrites::ALL,
                })],
                compilation_options: Default::default(),
            }),
            primitive: wgpu::PrimitiveState {
                topology: wgpu::PrimitiveTopology::TriangleList,
                cull_mode: None, // quads can face either way
                ..Default::default()
            },
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview: None,
            cache: None,
        });

        // === MERGE PIPELINE (decay + excitation absorption) ===
        let merge_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("merge_shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("shaders/decay.wgsl").into()),
        });

        let merge_uniform_buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("merge_uniforms"),
            size: std::mem::size_of::<MergeUniforms>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });

        let merge_bind_group_layout =
            device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
                label: Some("merge_bgl"),
                entries: &[
                    // persistence source texture
                    wgpu::BindGroupLayoutEntry {
                        binding: 0,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Texture {
                            sample_type: wgpu::TextureSampleType::Float { filterable: true },
                            view_dimension: wgpu::TextureViewDimension::D2,
                            multisampled: false,
                        },
                        count: None,
                    },
                    // excitation texture (this frame's beam energy)
                    wgpu::BindGroupLayoutEntry {
                        binding: 1,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Texture {
                            sample_type: wgpu::TextureSampleType::Float { filterable: true },
                            view_dimension: wgpu::TextureViewDimension::D2,
                            multisampled: false,
                        },
                        count: None,
                    },
                    // sampler
                    wgpu::BindGroupLayoutEntry {
                        binding: 2,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                        count: None,
                    },
                    // merge params
                    wgpu::BindGroupLayoutEntry {
                        binding: 3,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Buffer {
                            ty: wgpu::BufferBindingType::Uniform,
                            has_dynamic_offset: false,
                            min_binding_size: None,
                        },
                        count: None,
                    },
                ],
            });

        // Two bind groups: one per source persistence texture, both share excitation
        let merge_bind_groups = std::array::from_fn(|i| {
            device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(&format!("merge_bg_{i}")),
                layout: &merge_bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&persistence_views[i]),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: wgpu::BindingResource::TextureView(&excitation_view),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: wgpu::BindingResource::Sampler(&linear_sampler),
                    },
                    wgpu::BindGroupEntry {
                        binding: 3,
                        resource: merge_uniform_buffer.as_entire_binding(),
                    },
                ],
            })
        });

        let merge_pipeline_layout =
            device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                label: Some("merge_pipeline_layout"),
                bind_group_layouts: &[&merge_bind_group_layout],
                push_constant_ranges: &[],
            });

        let merge_pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("merge_pipeline"),
            layout: Some(&merge_pipeline_layout),
            vertex: wgpu::VertexState {
                module: &merge_shader,
                entry_point: Some("vs_main"),
                buffers: &[],
                compilation_options: Default::default(),
            },
            fragment: Some(wgpu::FragmentState {
                module: &merge_shader,
                entry_point: Some("fs_main"),
                targets: &[Some(wgpu::ColorTargetState {
                    format: HDR_FORMAT,
                    blend: Some(wgpu::BlendState::REPLACE),
                    write_mask: wgpu::ColorWrites::ALL,
                })],
                compilation_options: Default::default(),
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview: None,
            cache: None,
        });

        // === BLOOM PIPELINE ===
        let bloom_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("bloom_shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("shaders/bloom.wgsl").into()),
        });

        let bloom_bind_group_layout =
            device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
                label: Some("bloom_bgl"),
                entries: &[
                    wgpu::BindGroupLayoutEntry {
                        binding: 0,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Texture {
                            sample_type: wgpu::TextureSampleType::Float { filterable: true },
                            view_dimension: wgpu::TextureViewDimension::D2,
                            multisampled: false,
                        },
                        count: None,
                    },
                    wgpu::BindGroupLayoutEntry {
                        binding: 1,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                        count: None,
                    },
                    wgpu::BindGroupLayoutEntry {
                        binding: 2,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Buffer {
                            ty: wgpu::BufferBindingType::Uniform,
                            has_dynamic_offset: false,
                            min_binding_size: None,
                        },
                        count: None,
                    },
                ],
            });

        let bloom_uniform_buffer_h = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("bloom_uniforms_h"),
            size: std::mem::size_of::<BloomUniforms>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let bloom_uniform_buffer_v = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("bloom_uniforms_v"),
            size: std::mem::size_of::<BloomUniforms>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });

        // Bloom horizontal: one bind group per persistence texture
        let bloom_bind_groups_h = std::array::from_fn(|i| {
            device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(&format!("bloom_bg_h_{i}")),
                layout: &bloom_bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&persistence_views[i]),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: wgpu::BindingResource::Sampler(&linear_sampler),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: bloom_uniform_buffer_h.as_entire_binding(),
                    },
                ],
            })
        });

        // Bloom vertical: reads bloom_a -> writes bloom_b
        let bloom_bind_group_v = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("bloom_bg_v"),
            layout: &bloom_bind_group_layout,
            entries: &[
                wgpu::BindGroupEntry {
                    binding: 0,
                    resource: wgpu::BindingResource::TextureView(&bloom_view_a),
                },
                wgpu::BindGroupEntry {
                    binding: 1,
                    resource: wgpu::BindingResource::Sampler(&linear_sampler),
                },
                wgpu::BindGroupEntry {
                    binding: 2,
                    resource: bloom_uniform_buffer_v.as_entire_binding(),
                },
            ],
        });

        let bloom_pipeline_layout =
            device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                label: Some("bloom_pipeline_layout"),
                bind_group_layouts: &[&bloom_bind_group_layout],
                push_constant_ranges: &[],
            });

        let bloom_pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("bloom_pipeline"),
            layout: Some(&bloom_pipeline_layout),
            vertex: wgpu::VertexState {
                module: &bloom_shader,
                entry_point: Some("vs_main"),
                buffers: &[],
                compilation_options: Default::default(),
            },
            fragment: Some(wgpu::FragmentState {
                module: &bloom_shader,
                entry_point: Some("fs_main"),
                targets: &[Some(wgpu::ColorTargetState {
                    format: HDR_FORMAT,
                    blend: Some(wgpu::BlendState::REPLACE),
                    write_mask: wgpu::ColorWrites::ALL,
                })],
                compilation_options: Default::default(),
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview: None,
            cache: None,
        });

        // === COMPOSITE PIPELINE ===
        let composite_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("composite_shader"),
            source: wgpu::ShaderSource::Wgsl(include_str!("shaders/composite.wgsl").into()),
        });

        let composite_uniform_buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("composite_uniforms"),
            size: std::mem::size_of::<CompositeUniforms>() as u64,
            usage: wgpu::BufferUsages::UNIFORM | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });

        let composite_bind_group_layout =
            device.create_bind_group_layout(&wgpu::BindGroupLayoutDescriptor {
                label: Some("composite_bgl"),
                entries: &[
                    // persistence texture
                    wgpu::BindGroupLayoutEntry {
                        binding: 0,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Texture {
                            sample_type: wgpu::TextureSampleType::Float { filterable: true },
                            view_dimension: wgpu::TextureViewDimension::D2,
                            multisampled: false,
                        },
                        count: None,
                    },
                    // bloom texture
                    wgpu::BindGroupLayoutEntry {
                        binding: 1,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Texture {
                            sample_type: wgpu::TextureSampleType::Float { filterable: true },
                            view_dimension: wgpu::TextureViewDimension::D2,
                            multisampled: false,
                        },
                        count: None,
                    },
                    // sampler
                    wgpu::BindGroupLayoutEntry {
                        binding: 2,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Sampler(wgpu::SamplerBindingType::Filtering),
                        count: None,
                    },
                    // composite params
                    wgpu::BindGroupLayoutEntry {
                        binding: 3,
                        visibility: wgpu::ShaderStages::FRAGMENT,
                        ty: wgpu::BindingType::Buffer {
                            ty: wgpu::BufferBindingType::Uniform,
                            has_dynamic_offset: false,
                            min_binding_size: None,
                        },
                        count: None,
                    },
                ],
            });

        // Two composite bind groups, one for each persistence texture as source
        let composite_bind_groups = std::array::from_fn(|i| {
            device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(&format!("composite_bg_{i}")),
                layout: &composite_bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&persistence_views[i]),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: wgpu::BindingResource::TextureView(&bloom_view_b),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: wgpu::BindingResource::Sampler(&linear_sampler),
                    },
                    wgpu::BindGroupEntry {
                        binding: 3,
                        resource: composite_uniform_buffer.as_entire_binding(),
                    },
                ],
            })
        });

        let composite_pipeline_layout =
            device.create_pipeline_layout(&wgpu::PipelineLayoutDescriptor {
                label: Some("composite_pipeline_layout"),
                bind_group_layouts: &[&composite_bind_group_layout],
                push_constant_ranges: &[],
            });

        let composite_pipeline = device.create_render_pipeline(&wgpu::RenderPipelineDescriptor {
            label: Some("composite_pipeline"),
            layout: Some(&composite_pipeline_layout),
            vertex: wgpu::VertexState {
                module: &composite_shader,
                entry_point: Some("vs_main"),
                buffers: &[],
                compilation_options: Default::default(),
            },
            fragment: Some(wgpu::FragmentState {
                module: &composite_shader,
                entry_point: Some("fs_main"),
                targets: &[Some(wgpu::ColorTargetState {
                    format: surface_format,
                    blend: Some(wgpu::BlendState::REPLACE),
                    write_mask: wgpu::ColorWrites::ALL,
                })],
                compilation_options: Default::default(),
            }),
            primitive: wgpu::PrimitiveState::default(),
            depth_stencil: None,
            multisample: wgpu::MultisampleState::default(),
            multiview: None,
            cache: None,
        });

        Self {
            surface_format,
            width,
            height,
            persistence_textures,
            persistence_views,
            ping_pong_index: 0,
            excitation_texture,
            excitation_view,
            bloom_texture_a,
            bloom_view_a,
            bloom_texture_b,
            bloom_view_b,
            linear_sampler,
            line_pipeline,
            instance_buffer,
            line_uniform_buffer,
            line_bind_group,
            merge_pipeline,
            merge_uniform_buffer,
            merge_bind_group_layout,
            merge_bind_groups,
            bloom_pipeline,
            bloom_uniform_buffer_h,
            bloom_uniform_buffer_v,
            bloom_bind_group_layout,
            bloom_bind_groups_h,
            bloom_bind_group_v,
            composite_pipeline,
            composite_uniform_buffer,
            composite_bind_group_layout,
            composite_bind_groups,
            params,
        }
    }

    pub fn resize(&mut self, device: &wgpu::Device, width: u32, height: u32) {
        if width == 0 || height == 0 {
            return;
        }
        self.width = width;
        self.height = height;

        // Recreate persistence textures
        self.persistence_textures = std::array::from_fn(|i| {
            create_hdr_texture(device, width, height, &format!("persistence_{i}"))
        });
        self.persistence_views = std::array::from_fn(|i| {
            self.persistence_textures[i].create_view(&wgpu::TextureViewDescriptor::default())
        });

        // Recreate excitation texture
        self.excitation_texture = create_hdr_texture(device, width, height, "excitation");
        self.excitation_view = self.excitation_texture.create_view(&wgpu::TextureViewDescriptor::default());

        // Recreate bloom textures
        let bw = (width / 2).max(1);
        let bh = (height / 2).max(1);
        self.bloom_texture_a = create_hdr_texture(device, bw, bh, "bloom_a");
        self.bloom_view_a = self
            .bloom_texture_a
            .create_view(&wgpu::TextureViewDescriptor::default());
        self.bloom_texture_b = create_hdr_texture(device, bw, bh, "bloom_b");
        self.bloom_view_b = self
            .bloom_texture_b
            .create_view(&wgpu::TextureViewDescriptor::default());

        // Rebuild all bind groups that reference textures
        self.rebuild_bind_groups(device);
    }

    fn rebuild_bind_groups(&mut self, device: &wgpu::Device) {
        // Merge bind groups (persistence[i] + excitation → persistence[1-i])
        self.merge_bind_groups = std::array::from_fn(|i| {
            device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(&format!("merge_bg_{i}")),
                layout: &self.merge_bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&self.persistence_views[i]),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: wgpu::BindingResource::TextureView(&self.excitation_view),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: wgpu::BindingResource::Sampler(&self.linear_sampler),
                    },
                    wgpu::BindGroupEntry {
                        binding: 3,
                        resource: self.merge_uniform_buffer.as_entire_binding(),
                    },
                ],
            })
        });

        // Bloom H bind groups: one per persistence texture
        self.bloom_bind_groups_h = std::array::from_fn(|i| {
            device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(&format!("bloom_bg_h_{i}")),
                layout: &self.bloom_bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&self.persistence_views[i]),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: wgpu::BindingResource::Sampler(&self.linear_sampler),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: self.bloom_uniform_buffer_h.as_entire_binding(),
                    },
                ],
            })
        });

        self.bloom_bind_group_v = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("bloom_bg_v"),
            layout: &self.bloom_bind_group_layout,
            entries: &[
                wgpu::BindGroupEntry {
                    binding: 0,
                    resource: wgpu::BindingResource::TextureView(&self.bloom_view_a),
                },
                wgpu::BindGroupEntry {
                    binding: 1,
                    resource: wgpu::BindingResource::Sampler(&self.linear_sampler),
                },
                wgpu::BindGroupEntry {
                    binding: 2,
                    resource: self.bloom_uniform_buffer_v.as_entire_binding(),
                },
            ],
        });

        // Composite bind groups
        self.composite_bind_groups = std::array::from_fn(|i| {
            device.create_bind_group(&wgpu::BindGroupDescriptor {
                label: Some(&format!("composite_bg_{i}")),
                layout: &self.composite_bind_group_layout,
                entries: &[
                    wgpu::BindGroupEntry {
                        binding: 0,
                        resource: wgpu::BindingResource::TextureView(&self.persistence_views[i]),
                    },
                    wgpu::BindGroupEntry {
                        binding: 1,
                        resource: wgpu::BindingResource::TextureView(&self.bloom_view_b),
                    },
                    wgpu::BindGroupEntry {
                        binding: 2,
                        resource: wgpu::BindingResource::Sampler(&self.linear_sampler),
                    },
                    wgpu::BindGroupEntry {
                        binding: 3,
                        resource: self.composite_uniform_buffer.as_entire_binding(),
                    },
                ],
            })
        });
    }

    /// Render a frame.
    pub fn render(
        &mut self,
        device: &wgpu::Device,
        queue: &wgpu::Queue,
        surface: &wgpu::Surface,
        instances: &[LineInstance],
        dt: f64,
    ) -> Result<(), wgpu::SurfaceError> {
        // Upload instance data
        let instance_count = instances.len().min(MAX_INSTANCES as usize);
        if instance_count > 0 {
            queue.write_buffer(
                &self.instance_buffer,
                0,
                bytemuck::cast_slice(&instances[..instance_count]),
            );
        }

        // Update uniforms
        let decay_factor = (-dt as f32 / self.params.phosphor_tc).exp();
        queue.write_buffer(
            &self.line_uniform_buffer,
            0,
            bytemuck::bytes_of(&LineUniforms {
                viewport_size: [self.width as f32, self.height as f32],
                beam_width: self.params.beam_width,
                phosphor_tc: self.params.phosphor_tc,
            }),
        );
        queue.write_buffer(
            &self.merge_uniform_buffer,
            0,
            bytemuck::bytes_of(&MergeUniforms {
                decay_factor,
                phosphor_max: self.params.phosphor_max,
                _pad: [0.0; 2],
            }),
        );

        let bw = (self.width / 2).max(1) as f32;
        let bh = (self.height / 2).max(1) as f32;
        queue.write_buffer(
            &self.bloom_uniform_buffer_h,
            0,
            bytemuck::bytes_of(&BloomUniforms {
                direction: [1.0 / bw, 0.0],
                _pad: [0.0; 2],
            }),
        );
        queue.write_buffer(
            &self.bloom_uniform_buffer_v,
            0,
            bytemuck::bytes_of(&BloomUniforms {
                direction: [0.0, 1.0 / bh],
                _pad: [0.0; 2],
            }),
        );
        queue.write_buffer(
            &self.composite_uniform_buffer,
            0,
            bytemuck::bytes_of(&CompositeUniforms {
                phosphor_color: self.params.phosphor_color,
                bloom_strength: self.params.bloom_strength,
            }),
        );

        // Acquire surface texture
        let output = surface.get_current_texture()?;
        let output_view = output
            .texture
            .create_view(&wgpu::TextureViewDescriptor::default());

        let src = 1 - self.ping_pong_index; // previous frame's result
        let dst = self.ping_pong_index; // this frame's target

        let mut encoder = device.create_command_encoder(&wgpu::CommandEncoderDescriptor {
            label: Some("frame_encoder"),
        });

        // PASS 1: Line rendering — beam energy to excitation texture
        // Additive blend: overlapping lines within a frame stack (beam hits twice)
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("line_pass"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &self.excitation_view,
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
            });
            if instance_count > 0 {
                pass.set_pipeline(&self.line_pipeline);
                pass.set_bind_group(0, &self.line_bind_group, &[]);
                pass.set_vertex_buffer(0, self.instance_buffer.slice(..));
                pass.draw(0..6, 0..instance_count as u32);
            }
        }

        // PASS 2: Merge — decay persistence[src] + absorb excitation → persistence[dst]
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("merge_pass"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &self.persistence_views[dst],
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
            });
            pass.set_pipeline(&self.merge_pipeline);
            pass.set_bind_group(0, &self.merge_bind_groups[src], &[]);
            pass.draw(0..3, 0..1); // fullscreen triangle
        }

        // PASS 3: Bloom horizontal — read persistence[dst], write bloom_a
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("bloom_h_pass"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &self.bloom_view_a,
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
            });
            pass.set_pipeline(&self.bloom_pipeline);
            pass.set_bind_group(0, &self.bloom_bind_groups_h[dst], &[]);
            pass.draw(0..3, 0..1);
        }

        // PASS 4: Bloom vertical — read bloom_a, write bloom_b
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("bloom_v_pass"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &self.bloom_view_b,
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
            });
            pass.set_pipeline(&self.bloom_pipeline);
            pass.set_bind_group(0, &self.bloom_bind_group_v, &[]);
            pass.draw(0..3, 0..1);
        }

        // PASS 5: Composite — persistence[dst] + bloom_b -> swapchain
        {
            let mut pass = encoder.begin_render_pass(&wgpu::RenderPassDescriptor {
                label: Some("composite_pass"),
                color_attachments: &[Some(wgpu::RenderPassColorAttachment {
                    view: &output_view,
                    resolve_target: None,
                    ops: wgpu::Operations {
                        load: wgpu::LoadOp::Clear(wgpu::Color::BLACK),
                        store: wgpu::StoreOp::Store,
                    },
                })],
                depth_stencil_attachment: None,
                timestamp_writes: None,
                occlusion_query_set: None,
            });
            pass.set_pipeline(&self.composite_pipeline);
            pass.set_bind_group(0, &self.composite_bind_groups[dst], &[]);
            pass.draw(0..3, 0..1);
        }

        queue.submit(std::iter::once(encoder.finish()));
        output.present();

        // Swap ping-pong
        self.ping_pong_index = 1 - self.ping_pong_index;

        Ok(())
    }
}

fn create_hdr_texture(device: &wgpu::Device, width: u32, height: u32, label: &str) -> wgpu::Texture {
    device.create_texture(&wgpu::TextureDescriptor {
        label: Some(label),
        size: wgpu::Extent3d {
            width,
            height,
            depth_or_array_layers: 1,
        },
        mip_level_count: 1,
        sample_count: 1,
        dimension: wgpu::TextureDimension::D2,
        format: HDR_FORMAT,
        usage: wgpu::TextureUsages::RENDER_ATTACHMENT | wgpu::TextureUsages::TEXTURE_BINDING,
        view_formats: &[],
    })
}
