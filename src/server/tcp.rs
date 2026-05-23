//! TCP transport. Push-only — clients send length-prefixed binary frames of
//! beam commands; the display sends nothing back. Use WebSocket if you want
//! to receive input events.
//!
//! Wire format (little-endian):
//!   [u32 payload_byte_length][payload...]
//!   Payload = sequence of MoveTo (9B) / DrawTo (13B) commands.

use crate::beam::{parse_commands, BeamCommand};
use std::io::Read;
use std::net::{TcpListener, TcpStream};
use std::sync::mpsc::Sender;
use std::thread;

/// Maximum payload per frame: ~1 MB. Enough for tens of thousands of vectors.
const MAX_PAYLOAD: usize = 1_048_576;

pub fn spawn(port: u16, commands_tx: Sender<Vec<BeamCommand>>) {
    thread::Builder::new()
        .name("tcp-listener".into())
        .spawn(move || {
            let listener = match TcpListener::bind(format!("0.0.0.0:{port}")) {
                Ok(l) => l,
                Err(e) => {
                    log::error!("Failed to bind TCP port {port}: {e}");
                    return;
                }
            };
            log::info!("TCP listening on 0.0.0.0:{port}");

            for stream in listener.incoming() {
                match stream {
                    Ok(stream) => {
                        let peer = stream.peer_addr().ok();
                        log::info!("TCP client connected: {peer:?}");
                        handle_client(stream, &commands_tx);
                        log::info!("TCP client disconnected: {peer:?}");
                        // Empty sentinel → fall back to demo.
                        let _ = commands_tx.send(Vec::new());
                    }
                    Err(e) => log::warn!("TCP accept error: {e}"),
                }
            }
        })
        .expect("spawn tcp-listener");
}

fn handle_client(mut stream: TcpStream, tx: &Sender<Vec<BeamCommand>>) {
    let mut len_buf = [0u8; 4];
    loop {
        if stream.read_exact(&mut len_buf).is_err() {
            return;
        }
        let payload_len = u32::from_le_bytes(len_buf) as usize;
        if payload_len > MAX_PAYLOAD {
            log::warn!("TCP payload too large ({payload_len} bytes), dropping");
            return;
        }
        let mut payload = vec![0u8; payload_len];
        if stream.read_exact(&mut payload).is_err() {
            return;
        }
        match parse_commands(&payload) {
            Ok(cmds) => {
                if tx.send(cmds).is_err() {
                    return;
                }
            }
            Err(e) => {
                log::warn!("TCP protocol error: {e}");
                return;
            }
        }
    }
}
