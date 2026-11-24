//! Static checks for the draft of the current synthesis request.

use std::{
    net::{Ipv4Addr, TcpListener},
    path::Path,
    sync::{Arc, Mutex},
};

use rmcp::{
    handler::server::tool::ToolRouter, schemars::JsonSchema, tool, tool_handler, tool_router, Json,
    ServerHandler,
};
use serde::{Deserialize, Serialize};

use super::request::GeneratorRequest;
use crate::agents::{mcp::McpHttp, CommandLine};

static HOST_LISTENER: Mutex<Option<TcpListener>> = Mutex::new(None);

/// Reserve a listener in the agent's network namespace before the fuzzer unshares.
pub(crate) fn bind_host_listener() -> Result<(), libafl::Error> {
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))
        .map_err(|error| libafl::Error::unknown(format!("binding synthesis MCP: {error}")))?;
    *HOST_LISTENER
        .lock()
        .unwrap_or_else(|error| error.into_inner()) = Some(listener);
    Ok(())
}

#[derive(Deserialize, Serialize, JsonSchema)]
struct DraftValidation {
    request_id: Option<String>,
    status: String,
    error: Option<String>,
}

type ActiveRequest = Arc<Mutex<Option<GeneratorRequest>>>;

#[derive(Clone)]
struct SynthesisServer {
    active: ActiveRequest,
    tool_router: ToolRouter<Self>,
}

#[tool_router]
impl SynthesisServer {
    #[tool(
        description = "Statically validate the current request's result.draft.json and generator artifacts. Fix any error and call again. On success, rename result.draft.json to result.json. This tool reads files and never imports, executes, or publishes the generator."
    )]
    fn validate_synthesis_draft(&self) -> Json<DraftValidation> {
        let active = self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .clone();
        let (request_id, error) = match active {
            Some(request) => {
                let error = request.validate_draft().err();
                (Some(request.id), error)
            }
            None => (None, Some("no active synthesis request".into())),
        };
        Json(DraftValidation {
            request_id,
            status: if error.is_some() { "error" } else { "ok" }.into(),
            error,
        })
    }
}

#[tool_handler]
impl ServerHandler for SynthesisServer {}

pub(crate) struct SynthesisMcp {
    active: ActiveRequest,
    _http: McpHttp,
}

impl SynthesisMcp {
    fn start(listener: TcpListener, launch: &mut CommandLine) -> Result<Self, String> {
        let active = ActiveRequest::default();
        let for_server = Arc::clone(&active);
        let http = McpHttp::start(listener, "synthesis-mcp", move || SynthesisServer {
            active: Arc::clone(&for_server),
            tool_router: SynthesisServer::tool_router(),
        })?;
        launch.argv.extend([
            "-c".into(),
            format!(
                "mcp_servers.synthesis.url={}",
                serde_json::json!(http.url())
            ),
        ]);
        Ok(Self {
            active,
            _http: http,
        })
    }

    pub(super) fn campaign(launch: &mut CommandLine) -> Result<Self, String> {
        let listener = HOST_LISTENER
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .take()
            .ok_or("synthesis MCP listener was not bound before network isolation")?;
        Self::start(listener, launch)
    }

    pub(crate) fn replay(
        launch: &mut CommandLine,
        output_dir: &Path,
        request_id: &str,
        session: &str,
    ) -> Result<Self, String> {
        let listener =
            TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).map_err(|error| error.to_string())?;
        let server = Self::start(listener, launch)?;
        server.set_request(Some(GeneratorRequest {
            output_dir: output_dir.to_path_buf(),
            id: request_id.into(),
            session: session.into(),
        }));
        Ok(server)
    }

    pub(super) fn set_request(&self, request: Option<GeneratorRequest>) {
        *self
            .active
            .lock()
            .unwrap_or_else(|error| error.into_inner()) = request;
    }
}
