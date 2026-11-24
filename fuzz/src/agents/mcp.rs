//! Loopback HTTP transport shared by campaign MCP servers.

use rmcp::{
    transport::streamable_http_server::{
        session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
    },
    ServerHandler,
};
use tokio::sync::oneshot;

pub(crate) struct McpHttp {
    url: String,
    shutdown: Option<oneshot::Sender<()>>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl McpHttp {
    pub(crate) fn start<S: ServerHandler>(
        listener: std::net::TcpListener,
        thread_name: &str,
        service: impl Fn() -> S + Send + Sync + 'static,
    ) -> Result<Self, String> {
        listener
            .set_nonblocking(true)
            .map_err(|error| format!("setting MCP listener nonblocking: {error}"))?;
        let (bound_tx, bound_rx) = oneshot::channel();
        let (shutdown_tx, shutdown_rx) = oneshot::channel();
        let thread = std::thread::Builder::new()
            .name(thread_name.into())
            .spawn(move || {
                let runtime = match tokio::runtime::Builder::new_multi_thread()
                    .enable_all()
                    .build()
                {
                    Ok(runtime) => runtime,
                    Err(error) => {
                        let _ = bound_tx.send(Err(error.to_string()));
                        return;
                    }
                };
                runtime.block_on(async move {
                    let listener = match tokio::net::TcpListener::from_std(listener) {
                        Ok(listener) => listener,
                        Err(error) => {
                            let _ = bound_tx.send(Err(error.to_string()));
                            return;
                        }
                    };
                    let port = match listener.local_addr() {
                        Ok(addr) => addr.port(),
                        Err(error) => {
                            let _ = bound_tx.send(Err(error.to_string()));
                            return;
                        }
                    };
                    let service = StreamableHttpService::new(
                        move || Ok(service()),
                        LocalSessionManager::default().into(),
                        StreamableHttpServerConfig::default(),
                    );
                    let router = axum::Router::new().nest_service("/mcp", service);
                    let _ = bound_tx.send(Ok(port));
                    let _ = axum::serve(listener, router)
                        .with_graceful_shutdown(async move {
                            let _ = shutdown_rx.await;
                        })
                        .await;
                });
            })
            .map_err(|error| error.to_string())?;
        let port = bound_rx
            .blocking_recv()
            .map_err(|_| "MCP server dropped its bind result".to_string())??;
        Ok(Self {
            url: format!("http://127.0.0.1:{port}/mcp"),
            shutdown: Some(shutdown_tx),
            thread: Some(thread),
        })
    }

    pub(crate) fn url(&self) -> &str {
        &self.url
    }
}

impl Drop for McpHttp {
    fn drop(&mut self) {
        if let Some(shutdown) = self.shutdown.take() {
            let _ = shutdown.send(());
        }
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}
