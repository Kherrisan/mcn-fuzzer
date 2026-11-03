//! Small helpers that are not part of the fuzzer loop.

use std::io::ErrorKind;

use libafl::Error;

/// Wrap an `io::Error` in LibAFL's OS error.
pub fn io_error(err: std::io::Error) -> Error {
    let msg = err.to_string();
    Error::os_error(err, msg)
}

/// An execution timeout reported by the forkserver.
pub fn create_timeout_error(msg: impl Into<String>) -> Error {
    let msg = msg.into();
    Error::os_error(std::io::Error::new(ErrorKind::TimedOut, msg.clone()), msg)
}
