mod beam;
mod demo;
mod font;
mod osd;
mod renderer;
mod server;

use beam::{BeamCommand, ContentProvider, resolve_commands};
use demo::LissajousDemo;
use renderer::VectorRenderer;
use server::{Broadcast, DisplayEvent, ServerConfig, ServerHandle};
use std::sync::Arc;
use std::sync::mpsc::Receiver;
use winit::application::ApplicationHandler;
use winit::event::{ElementState, MouseButton, WindowEvent};
use winit::event_loop::{ActiveEventLoop, EventLoop};
use winit::keyboard::{Key, NamedKey};
use winit::window::{Window, WindowAttributes, WindowId};

struct App {
    window: Option<Window>,
    gpu: Option<GpuState>,
    content: Box<dyn ContentProvider>,
    time: f64,
    last_instant: Option<std::time::Instant>,
    frame_count: u64,

    // Network plumbing
    rx: Receiver<Vec<BeamCommand>>,
    broadcast: Arc<Broadcast>,

    // External content state
    external_commands: Option<Vec<BeamCommand>>,
    external_active: bool,

    // Cursor tracking (NDC, updated on each CursorMoved)
    cursor_ndc: (f32, f32),

    // OSD state
    osd_visible: bool,
    osd_active_slider: Option<usize>, // Some(i) while mouse-dragging slider i
    mouse_left_down: bool,
}

struct GpuState {
    device: wgpu::Device,
    queue: wgpu::Queue,
    surface: wgpu::Surface<'static>,
    config: wgpu::SurfaceConfiguration,
    renderer: VectorRenderer,
}

impl App {
    fn new(handle: ServerHandle) -> Self {
        Self {
            window: None,
            gpu: None,
            content: Box::new(LissajousDemo::default()),
            time: 0.0,
            last_instant: None,
            frame_count: 0,
            rx: handle.commands_rx,
            broadcast: handle.broadcast,
            external_commands: None,
            external_active: false,
            cursor_ndc: (0.0, 0.0),
            osd_visible: false,
            osd_active_slider: None,
            mouse_left_down: false,
        }
    }

    /// Convert a window-relative pixel position into NDC. NDC matches the
    /// coordinate space beam commands use: x in [-1, 1] left→right,
    /// y in [-1, 1] bottom→top. Non-square windows are NOT corrected here —
    /// clients receive the raw NDC and can apply aspect correction using the
    /// width/height from Hello/Resize events.
    fn pixel_to_ndc(&self, px: f64, py: f64) -> (f32, f32) {
        let (w, h) = match self.gpu.as_ref() {
            Some(g) => (g.config.width.max(1) as f64, g.config.height.max(1) as f64),
            None => return (0.0, 0.0),
        };
        let x = (px / w) * 2.0 - 1.0;
        let y = 1.0 - (py / h) * 2.0; // winit y is top-down
        (x as f32, y as f32)
    }
}

impl ApplicationHandler for App {
    fn resumed(&mut self, event_loop: &ActiveEventLoop) {
        if self.window.is_some() {
            return;
        }

        let attrs = WindowAttributes::default()
            .with_title("Vector Display Simulator")
            .with_inner_size(winit::dpi::LogicalSize::new(1024.0, 768.0));

        let window = event_loop.create_window(attrs).unwrap();
        let size = window.inner_size();

        let instance = wgpu::Instance::new(&wgpu::InstanceDescriptor {
            backends: wgpu::Backends::all(),
            flags: if cfg!(target_os = "windows") {
                wgpu::InstanceFlags::default()
            } else {
                wgpu::InstanceFlags::default() & !wgpu::InstanceFlags::VALIDATION_INDIRECT_CALL
            },
            ..Default::default()
        });

        // SAFETY: window lives as long as surface (both owned by App)
        let surface = unsafe {
            instance.create_surface_unsafe(
                wgpu::SurfaceTargetUnsafe::from_window(&window).unwrap(),
            )
        }
        .unwrap();

        let (device, queue, config) = pollster::block_on(async {
            let adapter = instance
                .request_adapter(&wgpu::RequestAdapterOptions {
                    power_preference: wgpu::PowerPreference::HighPerformance,
                    compatible_surface: Some(&surface),
                    force_fallback_adapter: false,
                })
                .await
                .expect("No suitable GPU adapter found");

            let info = adapter.get_info();
            log::info!("{} ({:?})", info.name, info.backend);

            let (device, queue) = adapter
                .request_device(&wgpu::DeviceDescriptor {
                    label: Some("vectordisplay_device"),
                    required_features: wgpu::Features::empty(),
                    required_limits: wgpu::Limits::default(),
                    memory_hints: wgpu::MemoryHints::Performance,
                    trace: wgpu::Trace::Off,
                })
                .await
                .expect("Failed to create device");

            let caps = surface.get_capabilities(&adapter);
            let format = caps
                .formats
                .iter()
                .find(|f| f.is_srgb())
                .copied()
                .unwrap_or(caps.formats[0]);

            let config = wgpu::SurfaceConfiguration {
                usage: wgpu::TextureUsages::RENDER_ATTACHMENT,
                format,
                width: size.width.max(1),
                height: size.height.max(1),
                present_mode: wgpu::PresentMode::AutoVsync,
                alpha_mode: caps.alpha_modes[0],
                view_formats: vec![],
                desired_maximum_frame_latency: 2,
            };
            surface.configure(&device, &config);

            (device, queue, config)
        });

        let renderer = VectorRenderer::new(&device, &queue, config.format, config.width, config.height);

        // Publish initial viewport so any client that connects sees a valid Hello.
        self.broadcast.set_viewport(config.width, config.height);

        self.gpu = Some(GpuState {
            device,
            queue,
            surface,
            config,
            renderer,
        });
        self.window = Some(window);
        self.last_instant = Some(std::time::Instant::now());
    }

    fn window_event(&mut self, event_loop: &ActiveEventLoop, _id: WindowId, event: WindowEvent) {
        match event {
            WindowEvent::CloseRequested => {
                event_loop.exit();
            }
            WindowEvent::Resized(new_size) => {
                if let Some(gpu) = &mut self.gpu {
                    if new_size.width > 0 && new_size.height > 0 {
                        gpu.config.width = new_size.width;
                        gpu.config.height = new_size.height;
                        gpu.surface.configure(&gpu.device, &gpu.config);
                        gpu.renderer
                            .resize(&gpu.device, new_size.width, new_size.height);
                        self.broadcast.set_viewport(new_size.width, new_size.height);
                    }
                }
            }
            WindowEvent::CursorMoved { position, .. } => {
                let (x, y) = self.pixel_to_ndc(position.x, position.y);
                self.cursor_ndc = (x, y);
                self.broadcast.send(DisplayEvent::CursorMove { x, y });

                // If the user is dragging an OSD slider, update its value.
                if let (Some(gpu), Some(slider)) = (self.gpu.as_mut(), self.osd_active_slider) {
                    osd::apply_drag(&mut gpu.renderer.params, slider, (x, y));
                }
            }
            WindowEvent::MouseInput { state, button, .. } => {
                let pressed = state == ElementState::Pressed;
                let name = match button {
                    MouseButton::Left => "left",
                    MouseButton::Right => "right",
                    MouseButton::Middle => "middle",
                    _ => return,
                };
                let (x, y) = self.cursor_ndc;
                self.broadcast.send(DisplayEvent::MouseButton {
                    x,
                    y,
                    button: name,
                    pressed,
                });

                // Mouse-drag handling for the OSD: a left-press over a slider
                // claims that slider and starts a drag; release clears it.
                if matches!(button, MouseButton::Left) {
                    self.mouse_left_down = pressed;
                    if pressed && self.osd_visible {
                        if let Some(slider) = osd::hit_test(self.cursor_ndc) {
                            self.osd_active_slider = Some(slider);
                            if let Some(gpu) = self.gpu.as_mut() {
                                osd::apply_drag(&mut gpu.renderer.params, slider, self.cursor_ndc);
                            }
                        }
                    }
                    if !pressed {
                        self.osd_active_slider = None;
                    }
                }
            }
            WindowEvent::KeyboardInput { event, .. } => {
                let pressed = event.state == ElementState::Pressed;

                // Always forward to clients (use the named-key string for special keys,
                // the character itself for printable ones).
                let key_name = match &event.logical_key {
                    Key::Named(named) => named_key_str(named).to_string(),
                    Key::Character(s) => s.to_string(),
                    _ => String::new(),
                };
                if !key_name.is_empty() {
                    self.broadcast.send(DisplayEvent::Key {
                        key: key_name,
                        pressed,
                    });
                }

                // Local key handling — only on press, only when GPU is up.
                if !pressed {
                    return;
                }
                let Some(gpu) = self.gpu.as_mut() else { return };
                match event.logical_key {
                    Key::Named(NamedKey::Escape) => event_loop.exit(),
                    Key::Character(ref ch) => match ch.as_str() {
                        "w" => gpu.renderer.params.beam_width *= 1.2,
                        "s" => gpu.renderer.params.beam_width /= 1.2,
                        "d" => gpu.renderer.params.phosphor_tc *= 1.2,
                        "a" => gpu.renderer.params.phosphor_tc /= 1.2,
                        "e" => {
                            gpu.renderer.params.bloom_strength =
                                (gpu.renderer.params.bloom_strength + 0.1).min(2.0)
                        }
                        "q" => {
                            gpu.renderer.params.bloom_strength =
                                (gpu.renderer.params.bloom_strength - 0.1).max(0.0)
                        }
                        "r" => gpu.renderer.params.beam_speed *= 1.2,
                        "f" => gpu.renderer.params.beam_speed /= 1.2,
                        // OSD: toggle visibility and reset-to-defaults.
                        "o" => self.osd_visible = !self.osd_visible,
                        "0" => osd::reset_defaults(&mut gpu.renderer.params),
                        _ => {}
                    },
                    _ => {}
                }
            }
            WindowEvent::RedrawRequested => {
                let now = std::time::Instant::now();
                let dt = self
                    .last_instant
                    .map(|prev| now.duration_since(prev).as_secs_f64())
                    .unwrap_or(1.0 / 60.0);
                self.last_instant = Some(now);
                self.time += dt;

                // Drain channel: take the most recent message.
                let mut latest = None;
                while let Ok(cmds) = self.rx.try_recv() {
                    latest = Some(cmds);
                }
                if let Some(cmds) = latest {
                    if cmds.is_empty() {
                        self.external_active = false;
                        self.external_commands = None;
                        log::info!("External source disconnected, falling back to demo");
                    } else {
                        self.external_active = true;
                        self.external_commands = Some(cmds);
                    }
                }

                let commands = if self.external_active {
                    self.external_commands.clone().unwrap_or_default()
                } else {
                    self.content.update(self.time, dt)
                };

                if let Some(gpu) = &mut self.gpu {
                    let mut instances =
                        resolve_commands(&commands, gpu.renderer.params.beam_speed);

                    // Overlay: OSD strokes are drawn last with no intra-frame
                    // decay so they stay crisp regardless of beam_speed.
                    if self.osd_visible {
                        let mut osd_commands = Vec::new();
                        osd::render(
                            &mut osd_commands,
                            &gpu.renderer.params,
                            self.osd_active_slider,
                        );
                        let mut osd_instances =
                            resolve_commands(&osd_commands, gpu.renderer.params.beam_speed);
                        for inst in osd_instances.iter_mut() {
                            inst.time_offset = 0.0;
                        }
                        instances.append(&mut osd_instances);
                    }

                    match gpu.renderer.render(
                        &gpu.device,
                        &gpu.queue,
                        &gpu.surface,
                        &instances,
                        dt,
                    ) {
                        Ok(()) => {
                            self.frame_count += 1;
                            if let Some(window) = &self.window {
                                window.pre_present_notify();
                                if self.frame_count % 15 == 0 {
                                    let p = &gpu.renderer.params;
                                    window.set_title(&format!(
                                        "Vector Display | beam_width={:.4} phosphor_tc={:.4} beam_speed={:.0} phosphor_max={:.1} bloom={:.2}",
                                        p.beam_width, p.phosphor_tc, p.beam_speed, p.phosphor_max, p.bloom_strength,
                                    ));
                                }
                            }
                        }
                        Err(wgpu::SurfaceError::Lost | wgpu::SurfaceError::Outdated) => {
                            log::warn!("Surface lost/outdated at frame {}, reconfiguring", self.frame_count);
                            gpu.surface.configure(&gpu.device, &gpu.config);
                        }
                        Err(e) => {
                            log::error!("Surface error at frame {}: {e}", self.frame_count);
                            event_loop.exit();
                        }
                    }
                }
            }
            _ => {}
        }
    }

    fn about_to_wait(&mut self, _event_loop: &ActiveEventLoop) {
        if let Some(window) = &self.window {
            window.request_redraw();
        }
    }

    fn suspended(&mut self, _event_loop: &ActiveEventLoop) {
        self.gpu = None;
    }
}

fn named_key_str(key: &NamedKey) -> &'static str {
    // Stable subset of named keys clients are likely to care about. Anything
    // not listed falls through to "Unidentified" so the event still fires.
    match key {
        NamedKey::Space => "Space",
        NamedKey::Enter => "Enter",
        NamedKey::Escape => "Escape",
        NamedKey::Tab => "Tab",
        NamedKey::Backspace => "Backspace",
        NamedKey::Delete => "Delete",
        NamedKey::ArrowUp => "ArrowUp",
        NamedKey::ArrowDown => "ArrowDown",
        NamedKey::ArrowLeft => "ArrowLeft",
        NamedKey::ArrowRight => "ArrowRight",
        NamedKey::Shift => "Shift",
        NamedKey::Control => "Control",
        NamedKey::Alt => "Alt",
        NamedKey::Home => "Home",
        NamedKey::End => "End",
        NamedKey::PageUp => "PageUp",
        NamedKey::PageDown => "PageDown",
        _ => "Unidentified",
    }
}

fn parse_port_arg(args: &[String], flag: &str, default: u16) -> Option<u16> {
    // Returns None if the user passed --no-<flag>, otherwise Some(port).
    let disable = format!("--no-{}", flag.trim_start_matches("--"));
    if args.iter().any(|a| a == &disable) {
        return None;
    }
    let parsed = args
        .windows(2)
        .find(|w| w[0] == flag)
        .and_then(|w| w[1].parse::<u16>().ok());
    Some(parsed.unwrap_or(default))
}

fn main() {
    env_logger::init();

    let args: Vec<String> = std::env::args().collect();
    let tcp_port = parse_port_arg(&args, "--tcp-port", 5001);
    let ws_port = parse_port_arg(&args, "--ws-port", 5002);

    log::info!(
        "Starting servers: tcp={:?}, ws={:?}",
        tcp_port,
        ws_port,
    );

    let handle = server::start(ServerConfig { tcp_port, ws_port });
    let event_loop = EventLoop::new().unwrap();
    let mut app = App::new(handle);
    match event_loop.run_app(&mut app) {
        Ok(()) => {}
        Err(e) => {
            eprintln!("Exited after {} frames: {e}", app.frame_count);
        }
    }
    // Drop GPU state before window to avoid use-after-free on compositor disconnect.
    drop(app.gpu.take());
    drop(app.window.take());
}
