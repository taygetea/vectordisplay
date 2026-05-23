//! Networking: receive beam commands from clients and broadcast input events
//! back to clients. Supports two transports in parallel:
//!
//! - **TCP** (port 5001 by default): length-prefixed binary frames, push-only
//!   (no back-channel). Lowest overhead, native clients only.
//! - **WebSocket** (port 5002 by default): bidirectional. Clients send binary
//!   frames of beam commands; the display sends JSON text frames back with
//!   mouse / keyboard / viewport events.
//!
//! Both transports share the same beam-command wire format and feed the same
//! mpsc channel to the render loop. Either or both can be disabled.

use crate::beam::BeamCommand;
use std::sync::mpsc::{self, Receiver};
use std::sync::{Arc, Mutex};

mod events;
mod tcp;
mod ws;

pub use events::DisplayEvent;

pub struct ServerConfig {
    pub tcp_port: Option<u16>,
    pub ws_port: Option<u16>,
}

pub struct ServerHandle {
    pub commands_rx: Receiver<Vec<BeamCommand>>,
    pub broadcast: Arc<Broadcast>,
}

/// Shared event fan-out + cached viewport state.
///
/// Each WS client subscribes on connect and gets its own `mpsc::Receiver`.
/// Dead senders are pruned lazily on the next `send`. The viewport size is
/// kept here too so a freshly-connected client can synthesize a `Hello`
/// event for itself without a round-trip to the main thread.
pub struct Broadcast {
    senders: Mutex<Vec<mpsc::Sender<DisplayEvent>>>,
    viewport: Mutex<(u32, u32)>,
}

impl Broadcast {
    fn new() -> Arc<Self> {
        Arc::new(Self {
            senders: Mutex::new(Vec::new()),
            viewport: Mutex::new((0, 0)),
        })
    }

    /// Broadcast an event to every connected client. Closed clients are
    /// pruned in passing.
    pub fn send(&self, event: DisplayEvent) {
        let mut senders = self.senders.lock().unwrap();
        senders.retain(|s| s.send(event.clone()).is_ok());
    }

    /// Update the cached viewport size. Called by the main loop on init and
    /// on every `WindowEvent::Resized`. Also broadcasts a `Resize` event so
    /// already-connected clients can react.
    pub fn set_viewport(&self, width: u32, height: u32) {
        {
            let mut v = self.viewport.lock().unwrap();
            if *v == (width, height) {
                return;
            }
            *v = (width, height);
        }
        self.send(DisplayEvent::Resize { width, height });
    }

    fn subscribe(&self) -> (Receiver<DisplayEvent>, (u32, u32)) {
        let viewport = *self.viewport.lock().unwrap();
        let (tx, rx) = mpsc::channel();
        self.senders.lock().unwrap().push(tx);
        (rx, viewport)
    }
}

pub fn start(cfg: ServerConfig) -> ServerHandle {
    let (commands_tx, commands_rx) = mpsc::channel();
    let broadcast = Broadcast::new();

    if let Some(port) = cfg.tcp_port {
        tcp::spawn(port, commands_tx.clone());
    }
    if let Some(port) = cfg.ws_port {
        ws::spawn(port, commands_tx, broadcast.clone());
    }

    ServerHandle {
        commands_rx,
        broadcast,
    }
}
