//! WebSocket transport. Bidirectional.
//!
//! - **Client → Display**: binary WS message = sequence of beam commands
//!   (same byte layout as TCP, but without the leading u32 — WS framing
//!   already carries length).
//! - **Display → Client**: text WS messages, one JSON event per frame.
//!   See `super::events::DisplayEvent`.
//!
//! Implementation: one OS thread per accepted client, with a short read
//! timeout so the same thread can flush outgoing events without blocking.

use super::events::DisplayEvent;
use super::Broadcast;
use crate::beam::{parse_commands, BeamCommand};
use std::io;
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc::Sender;
use std::sync::Arc;
use std::thread;
use std::time::Duration;
use tungstenite::{accept, Message};

/// How long a read can block before we check the outgoing event channel.
/// Short enough that input events feel snappy in the browser; long enough
/// not to burn CPU when idle.
const READ_TIMEOUT: Duration = Duration::from_millis(10);

pub fn spawn(port: u16, commands_tx: Sender<Vec<BeamCommand>>, broadcast: Arc<Broadcast>) {
    thread::Builder::new()
        .name("ws-listener".into())
        .spawn(move || {
            let listener = match TcpListener::bind(format!("0.0.0.0:{port}")) {
                Ok(l) => l,
                Err(e) => {
                    log::error!("Failed to bind WS port {port}: {e}");
                    return;
                }
            };
            log::info!("WS listening on 0.0.0.0:{port}");

            for stream in listener.incoming() {
                let stream = match stream {
                    Ok(s) => s,
                    Err(e) => {
                        log::warn!("WS accept error: {e}");
                        continue;
                    }
                };
                let peer = stream.peer_addr().ok();
                let tx = commands_tx.clone();
                let br = broadcast.clone();
                thread::Builder::new()
                    .name(format!("ws-client-{peer:?}"))
                    .spawn(move || {
                        log::info!("WS client connected: {peer:?}");
                        handle_client(stream, tx.clone(), br);
                        log::info!("WS client disconnected: {peer:?}");
                        let _ = tx.send(Vec::new());
                    })
                    .expect("spawn ws-client");
            }
        })
        .expect("spawn ws-listener");
}

fn handle_client(stream: TcpStream, commands_tx: Sender<Vec<BeamCommand>>, broadcast: Arc<Broadcast>) {
    // Do the WS handshake first with blocking reads — on Windows, setting a
    // read timeout BEFORE the handshake can surface ERROR_IO_PENDING (997)
    // through tungstenite's chunked read path.
    let mut ws = match accept(stream) {
        Ok(w) => w,
        Err(e) => {
            log::warn!("WS handshake failed: {e}");
            return;
        }
    };

    // After handshake, set a short read timeout so the same thread can poll
    // for incoming frames while flushing outgoing events.
    if let Err(e) = ws.get_ref().set_read_timeout(Some(READ_TIMEOUT)) {
        log::warn!("set_read_timeout failed: {e}");
        return;
    }

    let (events_rx, (width, height)) = broadcast.subscribe();

    // Hello goes directly to this client only.
    let hello = DisplayEvent::Hello { width, height };
    if ws.send(Message::Text(hello.to_json().into())).is_err() {
        return;
    }

    loop {
        // Flush pending outbound events first so they don't pile up.
        while let Ok(event) = events_rx.try_recv() {
            if ws.send(Message::Text(event.to_json().into())).is_err() {
                return;
            }
        }

        match ws.read() {
            Ok(Message::Binary(bytes)) => match parse_commands(&bytes) {
                Ok(cmds) => {
                    if commands_tx.send(cmds).is_err() {
                        return;
                    }
                }
                Err(e) => log::warn!("WS protocol error: {e}"),
            },
            Ok(Message::Text(text)) => {
                // Reserved for future client → server text protocol.
                log::debug!("WS text frame ignored: {}", text);
            }
            Ok(Message::Close(_)) => {
                let _ = ws.send(Message::Close(None));
                return;
            }
            Ok(Message::Ping(p)) => {
                if ws.send(Message::Pong(p)).is_err() {
                    return;
                }
            }
            Ok(_) => {}
            Err(tungstenite::Error::Io(e))
                if e.kind() == io::ErrorKind::WouldBlock
                    || e.kind() == io::ErrorKind::TimedOut
                    // Windows-specific: ERROR_IO_PENDING (997) and
                    // WSAEWOULDBLOCK (10035) / WSAETIMEDOUT (10060) sometimes
                    // surface here through overlapped I/O timeouts rather than
                    // mapped ErrorKinds.
                    || matches!(e.raw_os_error(), Some(997) | Some(10035) | Some(10060)) =>
            {
                continue;
            }
            Err(tungstenite::Error::ConnectionClosed | tungstenite::Error::AlreadyClosed) => {
                return;
            }
            Err(e) => {
                log::warn!("WS read error: {e}");
                return;
            }
        }
    }
}
