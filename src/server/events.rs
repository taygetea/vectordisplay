//! Events the display sends to connected clients (WS back-channel).
//!
//! Serialized as JSON text frames. Hand-rolled to avoid pulling in serde —
//! the schema is small and stable.

#[derive(Clone, Debug)]
pub enum DisplayEvent {
    /// Sent immediately on client connect: viewport size and aspect.
    /// Lets clients correct for non-square windows before they start drawing.
    Hello { width: u32, height: u32 },
    /// Window was resized. Same payload as Hello, sent on every resize.
    Resize { width: u32, height: u32 },
    /// Cursor moved over the display. `x`, `y` are in NDC ([-1, 1] each axis,
    /// distorted with the window aspect — same coord system the beam uses).
    CursorMove { x: f32, y: f32 },
    /// Mouse button pressed / released. `button` is "left" / "right" / "middle".
    MouseButton {
        x: f32,
        y: f32,
        button: &'static str,
        pressed: bool,
    },
    /// Keyboard key pressed / released. `key` is the logical key name
    /// ("a", "Space", "Escape", ...).
    Key { key: String, pressed: bool },
}

impl DisplayEvent {
    /// Render as a single-line JSON object.
    pub fn to_json(&self) -> String {
        match self {
            Self::Hello { width, height } => {
                format!(r#"{{"type":"hello","width":{width},"height":{height}}}"#)
            }
            Self::Resize { width, height } => {
                format!(r#"{{"type":"resize","width":{width},"height":{height}}}"#)
            }
            Self::CursorMove { x, y } => {
                format!(r#"{{"type":"cursor_move","x":{x},"y":{y}}}"#)
            }
            Self::MouseButton {
                x,
                y,
                button,
                pressed,
            } => format!(
                r#"{{"type":"mouse_button","x":{x},"y":{y},"button":"{button}","pressed":{pressed}}}"#
            ),
            Self::Key { key, pressed } => {
                let escaped = escape_json_string(key);
                format!(r#"{{"type":"key","key":"{escaped}","pressed":{pressed}}}"#)
            }
        }
    }
}

fn escape_json_string(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}
