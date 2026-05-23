//! On-screen settings UI. Five labeled sliders for the render parameters,
//! a panel border, and a footer line listing the keyboard shortcuts.
//!
//! Coordinates everywhere are NDC. The renderer treats the OSD as overlay
//! geometry (no beam-time decay) — see `main.rs` for how its LineInstances
//! are appended.

use crate::beam::BeamCommand;
use crate::font;
use crate::renderer::RenderParams;

// Layout — bottom-left corner, deliberately compact.
const PANEL_LEFT: f32 = -0.97;
const PANEL_TOP: f32 = -0.30;
const PANEL_WIDTH: f32 = 1.05;
const PANEL_HEIGHT: f32 = 0.62;

const LABEL_SIZE: f32 = 0.026;
const VALUE_SIZE: f32 = 0.026;
const TITLE_SIZE: f32 = 0.030;
const FOOTER_SIZE: f32 = 0.022;

const ROW_HEIGHT: f32 = 0.08;
const FIRST_ROW_OFFSET: f32 = 0.10; // distance from panel top down to first row baseline
const LABEL_INDENT: f32 = 0.025;
const SLIDER_LEFT: f32 = -0.65;
const SLIDER_WIDTH: f32 = 0.45;
const VALUE_LEFT: f32 = -0.16;
const SLIDER_HANDLE_HALF_H: f32 = 0.020;
const SLIDER_HIT_HALF_H: f32 = 0.030; // generous hit-test on Y

const PARAM_COUNT: usize = 5;

pub struct ParamSpec {
    pub label: &'static str,
    pub min: f32,
    pub max: f32,
    pub log_scale: bool,
}

const SPECS: [ParamSpec; PARAM_COUNT] = [
    ParamSpec { label: "WIDTH",    min: 0.0003, max: 0.005,   log_scale: true  },
    ParamSpec { label: "SPEED",    min: 500.0,  max: 20000.0, log_scale: true  },
    ParamSpec { label: "PHOSPHOR", min: 0.005,  max: 0.500,   log_scale: true  },
    ParamSpec { label: "MAX",      min: 0.5,    max: 10.0,    log_scale: true  },
    ParamSpec { label: "BLOOM",    min: 0.0,    max: 2.0,     log_scale: false },
];

fn read(params: &RenderParams, i: usize) -> f32 {
    match i {
        0 => params.beam_width,
        1 => params.beam_speed,
        2 => params.phosphor_tc,
        3 => params.phosphor_max,
        4 => params.bloom_strength,
        _ => 0.0,
    }
}

fn write(params: &mut RenderParams, i: usize, v: f32) {
    match i {
        0 => params.beam_width = v.max(1e-5),
        1 => params.beam_speed = v.max(10.0),
        2 => params.phosphor_tc = v.max(1e-4),
        3 => params.phosphor_max = v.max(0.05),
        4 => params.bloom_strength = v.max(0.0),
        _ => {}
    }
}

fn value_to_norm(v: f32, p: &ParamSpec) -> f32 {
    let n = if p.log_scale {
        let lv = v.max(p.min * 0.5).ln();
        (lv - p.min.ln()) / (p.max.ln() - p.min.ln())
    } else {
        (v - p.min) / (p.max - p.min)
    };
    n.clamp(0.0, 1.0)
}

fn norm_to_value(n: f32, p: &ParamSpec) -> f32 {
    let n = n.clamp(0.0, 1.0);
    if p.log_scale {
        (p.min.ln() + n * (p.max.ln() - p.min.ln())).exp()
    } else {
        p.min + n * (p.max - p.min)
    }
}

/// Slider track bounds in NDC: (x_left, y_baseline, width, full_track_height).
fn slider_geometry(row: usize) -> (f32, f32, f32, f32) {
    let y = PANEL_TOP - FIRST_ROW_OFFSET - row as f32 * ROW_HEIGHT;
    (SLIDER_LEFT, y, SLIDER_WIDTH, SLIDER_HANDLE_HALF_H * 2.0)
}

/// Test whether a cursor point falls within a slider's track. Returns
/// the slider index, if any.
pub fn hit_test(cursor: (f32, f32)) -> Option<usize> {
    for i in 0..PARAM_COUNT {
        let (sx, sy, sw, _) = slider_geometry(i);
        if cursor.0 >= sx - 0.01
            && cursor.0 <= sx + sw + 0.01
            && (cursor.1 - sy).abs() <= SLIDER_HIT_HALF_H
        {
            return Some(i);
        }
    }
    None
}

/// Given the cursor position and an active slider index, compute the new
/// value the user is dragging to.
pub fn drag_to_value(cursor: (f32, f32), row: usize) -> f32 {
    let (sx, _, sw, _) = slider_geometry(row);
    let n = ((cursor.0 - sx) / sw).clamp(0.0, 1.0);
    norm_to_value(n, &SPECS[row])
}

/// Apply a dragged-to value to params.
pub fn apply_drag(params: &mut RenderParams, row: usize, cursor: (f32, f32)) {
    let v = drag_to_value(cursor, row);
    write(params, row, v);
}

/// Reset all params to the library's defaults (the values the project is
/// shipped tuned to).
pub fn reset_defaults(params: &mut RenderParams) {
    *params = RenderParams::default();
}

fn format_value(spec: &ParamSpec, v: f32) -> String {
    if spec.max <= 0.01 {
        format!("{v:.5}")
    } else if spec.max <= 1.0 {
        format!("{v:.3}")
    } else if spec.max <= 100.0 {
        format!("{v:.2}")
    } else if spec.max <= 10000.0 {
        format!("{v:.0}")
    } else {
        format!("{v:.0}")
    }
}

/// Emit beam commands for the OSD. `hovered` is the slider currently being
/// dragged (or None); used to brighten its handle.
pub fn render(out: &mut Vec<BeamCommand>, params: &RenderParams, hovered: Option<usize>) {
    // Panel border.
    let l = PANEL_LEFT;
    let t = PANEL_TOP;
    let r = l + PANEL_WIDTH;
    let b = t - PANEL_HEIGHT;
    out.push(BeamCommand::MoveTo { x: l, y: t });
    out.push(BeamCommand::DrawTo { x: r, y: t, intensity: 0.5 });
    out.push(BeamCommand::DrawTo { x: r, y: b, intensity: 0.5 });
    out.push(BeamCommand::DrawTo { x: l, y: b, intensity: 0.5 });
    out.push(BeamCommand::DrawTo { x: l, y: t, intensity: 0.5 });

    // Title.
    font::draw_text(out, "BEAM SETTINGS", l + 0.025, t - 0.045, TITLE_SIZE, 0.9);
    // Title underline.
    out.push(BeamCommand::MoveTo { x: l + 0.025, y: t - 0.060 });
    out.push(BeamCommand::DrawTo { x: l + 0.30, y: t - 0.060, intensity: 0.5 });

    // Rows.
    for (i, spec) in SPECS.iter().enumerate() {
        let v = read(params, i);
        let n = value_to_norm(v, spec);
        let (sx, sy, sw, _) = slider_geometry(i);
        let row_baseline = sy + LABEL_SIZE * 0.5;

        // Label.
        font::draw_text(out, spec.label, l + LABEL_INDENT, row_baseline, LABEL_SIZE, 0.85);

        // Slider track.
        let track_intensity = if hovered == Some(i) { 0.85 } else { 0.5 };
        out.push(BeamCommand::MoveTo { x: sx, y: sy });
        out.push(BeamCommand::DrawTo { x: sx + sw, y: sy, intensity: track_intensity });

        // Tick marks at 0/25/50/75/100% (small ticks).
        for k in 0..=4 {
            let tx = sx + sw * (k as f32 / 4.0);
            out.push(BeamCommand::MoveTo { x: tx, y: sy - 0.008 });
            out.push(BeamCommand::DrawTo { x: tx, y: sy + 0.008, intensity: 0.4 });
        }

        // Handle: vertical bar plus a small dot for emphasis.
        let hx = sx + sw * n;
        let handle_intensity = if hovered == Some(i) { 1.4 } else { 1.0 };
        out.push(BeamCommand::MoveTo { x: hx, y: sy - SLIDER_HANDLE_HALF_H });
        out.push(BeamCommand::DrawTo {
            x: hx,
            y: sy + SLIDER_HANDLE_HALF_H,
            intensity: handle_intensity,
        });
        // Bright dot
        out.push(BeamCommand::MoveTo { x: hx, y: sy });
        out.push(BeamCommand::DrawTo { x: hx, y: sy, intensity: 1.6 });

        // Value text.
        let txt = format_value(spec, v);
        font::draw_text(out, &txt, VALUE_LEFT, row_baseline, VALUE_SIZE, 0.85);
    }

    // Footer hints.
    let footer_y = b + 0.04;
    font::draw_text(
        out,
        "0  RESET     O  CLOSE     W/S A/D R/F Q/E  TUNE",
        l + 0.025,
        footer_y,
        FOOTER_SIZE,
        0.55,
    );
}
