//! Incremental event boundaries for LF, CRLF and bare CR, including split CRLF.
//! Dispatch immediately on a blank CR line: elicitation must not wait for another
//! upstream byte. The optional following LF belongs to that already ended event.
use bytes::{Bytes, BytesMut};

#[derive(Default)]
pub(super) struct Events {
    held: BytesMut,
    line_nonempty: bool,
    skip_lf: bool,
    ended_cr_len: Option<usize>,
}
impl Events {
    pub fn push(&mut self, byte: u8) -> Result<Option<Bytes>, ()> {
        if self.skip_lf {
            self.skip_lf = false;
            if byte == b'\n' {
                if let Some(len) = self.ended_cr_len.take() {
                    if len >= super::transport::EVENT_LIMIT {
                        return Err(());
                    }
                } else {
                    self.append(byte)?;
                }
                return Ok(None);
            }
            self.ended_cr_len = None;
        }
        self.append(byte)?;
        match byte {
            b'\r' => {
                self.skip_lf = true;
                if !self.line_nonempty {
                    self.ended_cr_len = Some(self.held.len());
                    return Ok(Some(self.held.split().freeze()));
                }
                self.line_nonempty = false;
            }
            b'\n' => {
                if !self.line_nonempty {
                    return Ok(Some(self.held.split().freeze()));
                }
                self.line_nonempty = false;
            }
            _ => self.line_nonempty = true,
        }
        Ok(None)
    }
    fn append(&mut self, byte: u8) -> Result<(), ()> {
        if self.held.len() >= super::transport::EVENT_LIMIT {
            return Err(());
        }
        self.held.extend_from_slice(&[byte]);
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn all_line_endings_dispatch_without_waiting_for_another_event() {
        for ending in ["\n\n", "\r\n\r\n", "\r\r", "\n\r\n", "\r\n\n"] {
            let mut parser = Events::default();
            let mut events = vec![];
            for id in [1, 2] {
                for byte in format!("data: {{\"id\":{id}}}{ending}").bytes() {
                    if let Some(event) = parser.push(byte).unwrap() {
                        events.push(event);
                    }
                }
                // No EOF or next message is needed to deliver the event.
                assert_eq!(events.len(), id);
            }
            for (index, event) in events.iter().enumerate() {
                assert!(std::str::from_utf8(event)
                    .unwrap()
                    .starts_with(&format!("data: {{\"id\":{}}}", index + 1)));
            }
        }
    }
    #[test]
    fn unterminated_and_oversized_events() {
        let mut parser = Events::default();
        for byte in b"data: partial\r" {
            assert!(parser.push(*byte).unwrap().is_none());
        }
        let mut parser = Events::default();
        for _ in 0..super::super::transport::EVENT_LIMIT {
            parser.push(b'x').unwrap();
        }
        assert!(parser.push(b'x').is_err());
    }
}
