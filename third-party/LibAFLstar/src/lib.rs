//! LibAFLstar, an extension of LibAFL to fuzz stateful targets, primarily via sockets.

pub mod event_manager;
pub mod executor;
pub mod fuzzer;
pub mod generator_client;
pub mod generator_mutator;
pub mod input;
pub mod monitor;
pub mod mutator;
pub mod http_mutator;
pub mod rtsp_mutator;
pub mod replay;
pub mod state;
pub mod state_scheduler;

// The perf wrappers were written against LibAFL 0.11 and are not used by the
// fuzzer binaries. They are left out of the 0.16 build.
// pub mod perf;

mod libaflstar_bolts;
