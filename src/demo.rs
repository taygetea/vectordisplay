/// Demo content providers: Lissajous figures and test patterns.
use crate::beam::{BeamCommand, ContentProvider};

pub struct LissajousDemo {
    pub freq_x: f64,
    pub freq_y: f64,
    pub phase_offset: f64,
    pub num_points: usize,
}

impl Default for LissajousDemo {
    fn default() -> Self {
        Self {
            freq_x: 3.0,
            freq_y: 2.0,
            phase_offset: std::f64::consts::FRAC_PI_2,
            num_points: 512,
        }
    }
}

impl ContentProvider for LissajousDemo {
    fn update(&mut self, time: f64, _dt: f64) -> Vec<BeamCommand> {
        let mut cmds = Vec::with_capacity(self.num_points + 1);
        let scale = 0.8;

        for i in 0..=self.num_points {
            let t = (i as f64 / self.num_points as f64) * std::f64::consts::TAU;
            let x = (self.freq_x * t + time * 0.5).sin() * scale;
            let y = (self.freq_y * t + self.phase_offset + time * 0.3).sin() * scale;

            if i == 0 {
                cmds.push(BeamCommand::MoveTo {
                    x: x as f32,
                    y: y as f32,
                });
            } else {
                cmds.push(BeamCommand::DrawTo {
                    x: x as f32,
                    y: y as f32,
                    intensity: 1.0,
                });
            }
        }

        cmds
    }
}

/// Simple rotating line pattern for testing.
pub struct TestPattern;

impl ContentProvider for TestPattern {
    fn update(&mut self, time: f64, _dt: f64) -> Vec<BeamCommand> {
        let mut cmds = Vec::new();
        let n = 8;

        for i in 0..n {
            let angle = (i as f64 / n as f64) * std::f64::consts::TAU + time;
            let x = angle.cos() as f32 * 0.7;
            let y = angle.sin() as f32 * 0.7;

            cmds.push(BeamCommand::MoveTo { x: 0.0, y: 0.0 });
            cmds.push(BeamCommand::DrawTo {
                x,
                y,
                intensity: 1.0,
            });
        }

        cmds
    }
}
