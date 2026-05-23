/// Beam commands and content provider trait for the vector display simulator.

/// Commands that drive the beam across the CRT face.
#[derive(Clone)]
pub enum BeamCommand {
    /// Move beam to position without drawing (beam blanked).
    MoveTo { x: f32, y: f32 },
    /// Draw a line from current position to target. Intensity in [0, 1].
    DrawTo { x: f32, y: f32, intensity: f32 },
}

/// A single line segment instance sent to the GPU.
/// Vertex shader expands a unit quad around this segment.
#[repr(C)]
#[derive(Copy, Clone, bytemuck::Pod, bytemuck::Zeroable)]
pub struct LineInstance {
    pub start: [f32; 2],
    pub end: [f32; 2],
    pub intensity: f32,
    pub time_offset: f32, // seconds since this line was drawn (within frame)
}

/// Anything that produces beam commands each frame.
pub trait ContentProvider {
    fn update(&mut self, time: f64, dt: f64) -> Vec<BeamCommand>;
}

/// Parse beam commands from wire protocol bytes.
/// Format: sequence of [u8 tag][f32 x][f32 y][optional f32 intensity]
///   tag 0 = MoveTo (9 bytes), tag 1 = DrawTo (13 bytes)
pub fn parse_commands(data: &[u8]) -> Result<Vec<BeamCommand>, &'static str> {
    let mut commands = Vec::new();
    let mut pos = 0;

    while pos < data.len() {
        if pos + 1 > data.len() {
            return Err("unexpected end of data: missing command tag");
        }
        let tag = data[pos];
        pos += 1;

        match tag {
            0 => {
                if pos + 8 > data.len() {
                    return Err("unexpected end of data: incomplete MoveTo");
                }
                let x = f32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
                let y = f32::from_le_bytes(data[pos + 4..pos + 8].try_into().unwrap());
                pos += 8;
                commands.push(BeamCommand::MoveTo { x, y });
            }
            1 => {
                if pos + 12 > data.len() {
                    return Err("unexpected end of data: incomplete DrawTo");
                }
                let x = f32::from_le_bytes(data[pos..pos + 4].try_into().unwrap());
                let y = f32::from_le_bytes(data[pos + 4..pos + 8].try_into().unwrap());
                let intensity = f32::from_le_bytes(data[pos + 8..pos + 12].try_into().unwrap());
                pos += 12;
                commands.push(BeamCommand::DrawTo { x, y, intensity });
            }
            _ => return Err("unknown command tag"),
        }
    }

    Ok(commands)
}

/// Resolve a sequence of BeamCommands into GPU-ready LineInstances.
/// beam_speed: NDC units per second. The beam takes time to traverse each segment,
/// and each line's time_offset reflects how long ago it was drawn within this frame.
pub fn resolve_commands(commands: &[BeamCommand], beam_speed: f32) -> Vec<LineInstance> {
    // First pass: collect instances and compute cumulative beam time
    let mut instances = Vec::new();
    let mut pos = [0.0f32; 2];
    let mut cumulative_time = 0.0f32;
    let mut draw_times = Vec::new();

    for cmd in commands {
        match cmd {
            BeamCommand::MoveTo { x, y } => {
                // Beam takes time to reposition even when blanked
                let dx = *x - pos[0];
                let dy = *y - pos[1];
                let dist = (dx * dx + dy * dy).sqrt();
                cumulative_time += dist / beam_speed;
                pos = [*x, *y];
            }
            BeamCommand::DrawTo { x, y, intensity } => {
                let end = [*x, *y];
                let dx = end[0] - pos[0];
                let dy = end[1] - pos[1];
                let dist = (dx * dx + dy * dy).sqrt();
                let draw_time = dist / beam_speed;

                // Record the time at the midpoint of this segment's draw
                draw_times.push(cumulative_time + draw_time * 0.5);
                cumulative_time += draw_time;

                instances.push(LineInstance {
                    start: pos,
                    end,
                    intensity: *intensity,
                    time_offset: 0.0, // filled in below
                });
                pos = end;
            }
        }
    }

    // Second pass: time_offset = total_trace_time - draw_time
    // Last line drawn has offset ≈ 0, first line has the largest offset
    let total_time = cumulative_time;
    for (inst, t) in instances.iter_mut().zip(draw_times.iter()) {
        inst.time_offset = total_time - t;
    }

    instances
}
