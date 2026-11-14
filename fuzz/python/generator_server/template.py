"""Reference scaffold for synthesized network generators.

This file is intentionally a *reference*, not a runnable production generator.
Copy the parts relevant to the proposal into the request's ``generator.py`` and
replace the example step names, fields, and protocol logic.

The runtime imports ``generator.py`` once, discovers synchronous functions
decorated with ``@step_handler("<model step name>")``, and dispatches the
concrete trace in order.  Do not write a trace loop or define ``generate`` when
using decorated handlers: ``generator_server.handlers.build_dispatch`` already
provides it.

ProfuzzGym target examples below (one handler each, 13 targets):
DAAP/forked-daapd, DICOM/Dcmtk, DNS/Dnsmasq, DTLS/TinyDTLS,
FTP/BFTPD, FTP/LightFTP, FTP/ProFTPD, FTP/PureFTPD, RTSP/Live555,
SIP/Kamailio, SMTP/Exim, SSH/OpenSSH, TLS/OpenSSL.

All examples are under ``if False:`` so importing this scaffold does not
register thirteen unrelated handlers.  Copy a block out of the guard, replace
``MODEL_STEP_NAME``, and implement the body.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import ssl
import struct
import tempfile
import time
from typing import Any

from generator_server import Channel, Ctx, InteractionError, global_init, step_handler

# A unique sentinel distinguishes "not supplied" from a legitimate None.
_MISSING = object()


def _instance(ctx: Ctx) -> dict[str, Any]:
    """Return the current read-only step instance or fail with a useful error."""
    if ctx.step_instance is None:
        raise InteractionError("generator", "no current step instance")
    return ctx.step_instance


def _field(ctx: Ctx, params: dict[str, Any], name: str, default: Any = _MISSING) -> Any:
    """Resolve one field in the order required by the runtime contract.

    Precedence:
      1. this instance's entry in the per-instance ``params`` map;
      2. explicit omission in ``omitted_fields`` (never silently fall back);
      3. an ``inputs`` dependency binding, via ``ctx.load_depending_state``;
      4. the caller's default, if one was supplied.

    ``params`` is keyed by concrete step instance name (``ctx.step_name``).
    """
    instance = _instance(ctx)
    scope = params.get(instance.get("name"), {})
    if name in scope:
        return scope[name]

    if name in (instance.get("omitted_fields") or ()):
        if default is not _MISSING:
            return default
        raise InteractionError("generator", f"field {name!r} was deliberately omitted")

    inputs = instance.get("inputs") or {}
    if name in inputs:
        return ctx.load_depending_state(name)

    if default is not _MISSING:
        return default
    raise InteractionError("generator", f"field {name!r} has no value")


def _text_field(ctx: Ctx, params: dict[str, Any], name: str, default: Any = _MISSING) -> str:
    value = _field(ctx, params, name, default)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _int_field(ctx: Ctx, params: dict[str, Any], name: str, default: Any = _MISSING) -> int:
    value = _field(ctx, params, name, default)
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InteractionError("generator", f"field {name!r} is not an integer") from exc


def _bytes_field(ctx: Ctx, params: dict[str, Any], name: str, default: Any = _MISSING) -> bytes:
    value = _field(ctx, params, name, default)
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")
    raise InteractionError("generator", f"field {name!r} is not text or bytes")


def _recv_until(
    ctx: Ctx,
    delimiter: bytes,
    *,
    channel: Channel | None = None,
    limit: int = 65536,
) -> bytes:
    """Read until a delimiter, respecting a total byte bound.

    ``ctx.recv`` may return a partial chunk when ``max_bytes`` is reached, so
    keep reading until the delimiter is present or the local bound is hit.
    """
    if not delimiter:
        raise InteractionError("generator", "delimiter must not be empty")
    data = bytearray()
    while len(data) < limit:
        chunk = ctx.recv(
            channel=channel,
            until=delimiter,
            max_bytes=min(4096, limit - len(data)),
        )
        data.extend(chunk)
        if delimiter in data:
            return bytes(data)
    raise InteractionError("generator", "response exceeded local read bound")


def _readline(ctx: Ctx, *, channel: Channel | None = None, limit: int = 4096) -> bytes:
    return _recv_until(ctx, b"\r\n", channel=channel, limit=limit)


def _close_quietly(resource: Any) -> None:
    """Best-effort cleanup for SDK/network objects in a handler."""
    if resource is None:
        return
    close = getattr(resource, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Shared initializer pattern
# ---------------------------------------------------------------------------
# ``@global_init`` is optional and runs once per testcase before the first
# trace step.  Keep imports with possible thread/service side effects here or
# inside handlers, never at module import time.
#
# if False:
#     @global_init
#     def initialize(ctx: Ctx, params: dict[str, Any]) -> None:
#         ctx.case_notes = []          # case-local attribute, discarded with ctx
#         ctx.case_notes.append("initialized")
#
# Use ctx.save_state() only inside a step handler: it needs ctx.step_name.
# ctx.load_state() reads only the current instance's own state; use a sentinel
# default to tell "missing" from a saved None.  ctx.get_global(name, default)
# reads testcase-scoped shared parameters when that API is appropriate.


# ===========================================================================
# Listener pattern (generator-owned server or accepted data connection)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def accept_one_connection(ctx: Ctx, params: dict[str, Any]) -> None:
        # bind immediately; the actual port is known even when port=0 was used
        listener = ctx.accept(host="127.0.0.1", port=0, timeout=ctx.timeout_s)
        try:
            ctx.save_state("listen_port", listener.port)
            accepted = ctx.load_state("accepted_count", 0)
            ctx.save_state("accepted_count", int(accepted) + 1)
            channel = listener.take(timeout=ctx.timeout_s)
            try:
                greeting = _readline(ctx, channel=channel)
                ctx.save_state("peer_greeting", greeting)
                ctx.send(b"OK\r\n", channel=channel)
            finally:
                channel.close()
        finally:
            listener.close()  # also stops its accept thread and closes queued channels
        # ctx.close_all()/ctx.close_extra() are normally called by dispatch's
        # finally block; close explicit case-local resources yourself as above.
        # A previous handler may have saved: prior = ctx.load_state("prior_key")
        # Shared generator globals: ctx.get_global("name", default)


# ===========================================================================
# DAAP / forked-daapd (HTTP + DMAP)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def daap_http_request(ctx: Ctx, params: dict[str, Any]) -> None:
        # Imports belong inside the handler so module import is side-effect free.
        import httpx
        from pyatv.protocols.dmap import parser, tag_definitions, tags

        path = _text_field(ctx, params, "path", "/server-info")
        method = _text_field(ctx, params, "method", "GET").upper()
        base = f"http://{ctx.host}:{ctx.port}"
        headers = {"Accept": "application/x-dmap-tagged"}
        try:
            with httpx.Client(base_url=base, timeout=ctx.timeout_s) as client:
                response = client.request(method, path, headers=headers)
            # Preserve the real status/body for the next handler or dependency.
            ctx.save_state("daap_status", response.status_code)
            ctx.save_state("daap_body", response.content)
            if response.headers.get("content-type", "").startswith("application/x-dmap-tagged"):
                parsed = parser.parse(response.content, tag_definitions.lookup_tag)
                ctx.save_state("daap_first_tag", getattr(parsed, "name", None))
        except httpx.HTTPError as exc:
            raise InteractionError("conn", f"DAAP request failed at {ctx.step_name}: {exc}") from exc
        # Example DMAP encoder for a controlled response or upstream fixture:
        # body = tags.container_tag("mlog", tags.uint32_tag("mstt", 200))


# ===========================================================================
# DICOM / Dcmtk (DIMSE association)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def dicom_c_echo(ctx: Ctx, params: dict[str, Any]) -> None:
        from pynetdicom import AE
        from pynetdicom.sop_class import Verification

        ae_title = _text_field(ctx, params, "ae_title", "MCNSCU")
        called_ae = _text_field(ctx, params, "called_ae", "ANY-SCP")
        ae = AE(ae_title=ae_title)
        ae.add_requested_context(Verification)
        ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = ctx.timeout_s
        association = ae.associate(ctx.host, ctx.port, ae_title=called_ae)
        if not association.is_established:
            raise InteractionError("conn", f"DICOM association to {ctx.host}:{ctx.port} failed")
        try:
            status = association.send_c_echo()
            if status is None:
                raise InteractionError("eof", "DICOM peer closed during C-ECHO")
            ctx.save_state("dicom_status", int(status.Status))
        finally:
            association.release()
            if association.is_established:
                association.abort()


# ===========================================================================
# DNS / Dnsmasq (UDP query and response parsing)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def dns_query(ctx: Ctx, params: dict[str, Any]) -> None:
        import dns.message

        name = _text_field(ctx, params, "qname", "example.test.")
        rdtype = _text_field(ctx, params, "qtype", "A").upper()
        query = dns.message.make_query(name, rdtype, use_edns=True)
        channel = ctx.connect(ctx.host, ctx.port, proto="udp", timeout=ctx.timeout_s)
        try:
            ctx.send(query.to_wire(), channel=channel)
            # For exact UDP datagram boundaries use a native socket.recvfrom();
            # Ctx.recv is a generic byte buffer and may coalesce datagrams.
            wire = ctx.recv(channel=channel, max_bytes=65535)
            reply = dns.message.from_wire(wire)
            ctx.save_state("dns_id", reply.id)
            ctx.save_state("dns_rcode", reply.rcode())
            ctx.save_state("dns_answer_count", len(reply.answer))
        finally:
            channel.close()


# ===========================================================================
# DTLS / TinyDTLS (PSK handshake over UDP)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def dtls_handshake(ctx: Ctx, params: dict[str, Any]) -> None:
        from mbedtls import tls

        identity = _text_field(ctx, params, "psk_identity", "mcn-client")
        psk = _bytes_field(ctx, params, "psk", b"0123456789abcdef")
        config = tls.DTLSConfiguration(
            validate_certificates=False,  # Only for a deliberately unverified test peer.
            pre_shared_key=(identity, psk),
            lowest_supported_version=tls.DTLSVersion.DTLSv1_2,
            highest_supported_version=tls.DTLSVersion.DTLSv1_2,
            handshake_timeout_min=0.1,
            handshake_timeout_max=ctx.timeout_s,
        )
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(ctx.timeout_s)
        connection = None
        try:
            sock.connect((ctx.host, ctx.port))
            connection = tls.ClientContext(config).wrap_socket(
                sock, server_hostname=None
            )
            if hasattr(connection, "do_handshake"):
                connection.do_handshake()
            payload = _bytes_field(ctx, params, "payload", b"mcn-dtls")
            connection.send(payload)
            ctx.save_state("dtls_peer", connection.getpeercert(binary_form=True))
        except (OSError, ValueError) as exc:
            raise InteractionError("conn", f"DTLS handshake failed: {exc}") from exc
        finally:
            _close_quietly(connection)
            _close_quietly(sock)


# ===========================================================================
# FTP / BFTPD (high-level FTP client)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def ftp_bftpd_login(ctx: Ctx, params: dict[str, Any]) -> None:
        from ftplib import FTP

        user = _text_field(ctx, params, "username", "anonymous")
        password = _text_field(ctx, params, "password", "anonymous@")
        ftp = FTP(timeout=ctx.timeout_s)
        try:
            welcome = ftp.connect(ctx.host, ctx.port)
            ctx.save_state("ftp_welcome", welcome)
            ftp.login(user=user, passwd=password)
            ctx.save_state("ftp_user", user)
            ctx.save_state("ftp_feat", ftp.sendcmd("FEAT"))
        finally:
            _close_quietly(ftp)


# ===========================================================================
# FTP / LightFTP (raw control channel; shows ctx.send/recv)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def ftp_lightftp_login(ctx: Ctx, params: dict[str, Any]) -> None:
        user = _text_field(ctx, params, "username", "anonymous")
        password = _text_field(ctx, params, "password", "anonymous@")
        # Omitted channel uses the lazy default SUT TCP connection.
        welcome = _readline(ctx)
        ctx.save_state("ftp_welcome", welcome)
        ctx.send(f"USER {user}\r\n".encode("utf-8"))
        ctx.save_state("ftp_user_reply", _readline(ctx))
        ctx.send(f"PASS {password}\r\n".encode("utf-8"))
        ctx.save_state("ftp_pass_reply", _readline(ctx))
        ctx.send(b"TYPE I\r\n")
        ctx.save_state("ftp_type_reply", _readline(ctx))
        ctx.save_state("ftp_user", user)


# ===========================================================================
# FTP / ProFTPD (explicit FTPS)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def ftp_proftpd_login(ctx: Ctx, params: dict[str, Any]) -> None:
        from ftplib import FTP_TLS

        user = _text_field(ctx, params, "username", "anonymous")
        password = _text_field(ctx, params, "password", "anonymous@")
        ftp = FTP_TLS(timeout=ctx.timeout_s)
        try:
            ftp.connect(ctx.host, ctx.port)
            ftp.auth()
            ftp.login(user=user, passwd=password)
            ftp.prot_p()  # Protect the data channel only when the proposal requires it.
            ctx.save_state("ftps_user", user)
            ctx.save_state("ftps_prot", ftp.sendcmd("PBSZ 0"))
        finally:
            _close_quietly(ftp)


# ===========================================================================
# FTP / PureFTPD (feature discovery + passive transfer)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def ftp_pureftpd_transfer(ctx: Ctx, params: dict[str, Any]) -> None:
        from ftplib import FTP

        user = _text_field(ctx, params, "username", "anonymous")
        password = _text_field(ctx, params, "password", "anonymous@")
        path = _text_field(ctx, params, "path", "/")
        ftp = FTP(timeout=ctx.timeout_s)
        try:
            ftp.connect(ctx.host, ctx.port)
            ftp.login(user=user, passwd=password)
            ctx.save_state("ftp_features", ftp.sendcmd("FEAT"))
            ftp.set_pasv(True)
            ctx.save_state("ftp_names", ftp.nlst(path))
        finally:
            _close_quietly(ftp)


# ===========================================================================
# RTSP / Live555 (raw request, CSeq and response headers)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def rtsp_options(ctx: Ctx, params: dict[str, Any]) -> None:
        url = _text_field(ctx, params, "url", f"rtsp://{ctx.host}:{ctx.port}/mcn")
        cseq = _int_field(ctx, params, "cseq", 1)
        request = (
            f"OPTIONS {url} RTSP/1.0\r\n"
            f"CSeq: {cseq}\r\n"
            "User-Agent: mcn-generator\r\n"
            "\r\n"
        ).encode("ascii")
        ctx.send(request)
        response = _recv_until(ctx, b"\r\n\r\n", limit=16384)
        ctx.save_state("rtsp_cseq", cseq)
        ctx.save_state("rtsp_response_headers", response)
        # Preserve Session/Transport values from actual replies for later steps:
        # for line in response.split(b"\r\n"):
        #     if line.lower().startswith(b"session:"):
        #         ctx.save_state("rtsp_session", line.split(b":", 1)[1].strip())


# ===========================================================================
# SIP / Kamailio (UDP request with transaction identifiers)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def sip_options(ctx: Ctx, params: dict[str, Any]) -> None:
        from twisted.protocols import sip

        user = _text_field(ctx, params, "user", "mcn")
        target = _text_field(ctx, params, "target", f"sip:{user}@{ctx.host}")
        call_id = _text_field(ctx, params, "call_id", f"mcn-{int(time.time() * 1000)}")
        request = sip.Request("OPTIONS", target)
        request.addHeader("via", f"SIP/2.0/UDP {ctx.host};branch=z9hG4bK-mcn")
        request.addHeader("from", f"<sip:{user}@{ctx.host}>;tag=mcn")
        request.addHeader("to", f"<{target}>")
        request.addHeader("call-id", call_id)
        request.addHeader("cseq", "1 OPTIONS")
        request.addHeader("content-length", "0")
        wire = request.toString().encode("ascii")
        channel = ctx.connect(ctx.host, ctx.port, proto="udp", timeout=ctx.timeout_s)
        try:
            ctx.send(wire, channel=channel)
            response = ctx.recv(channel=channel, max_bytes=65535)
            ctx.save_state("sip_call_id", call_id)
            ctx.save_state("sip_response", response)
        finally:
            channel.close()


# ===========================================================================
# SMTP / Exim (EHLO and message submission)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def smtp_ehlo(ctx: Ctx, params: dict[str, Any]) -> None:
        import smtplib
        from email.message import EmailMessage
        from email.policy import SMTP

        sender = _text_field(ctx, params, "sender", "mcn@example.test")
        recipient = _text_field(ctx, params, "recipient", "root@example.test")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = "mcn generator probe"
        message.set_content("smtp scaffold probe\n", policy=SMTP)
        with smtplib.SMTP(ctx.host, ctx.port, timeout=ctx.timeout_s) as smtp:
            code, banner = smtp.ehlo()
            ctx.save_state("smtp_ehlo_code", code)
            ctx.save_state("smtp_banner", banner)
            smtp.send_message(message, from_addr=sender, to_addrs=[recipient])
            ctx.save_state("smtp_last_code", smtp.noop()[0])


# ===========================================================================
# SSH / OpenSSH (banner + password authentication)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def ssh_banner(ctx: Ctx, params: dict[str, Any]) -> None:
        import paramiko

        user = _text_field(ctx, params, "username", "mcn")
        password = _text_field(ctx, params, "password", "mcn")
        sock = socket.create_connection((ctx.host, ctx.port), timeout=ctx.timeout_s)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=ctx.timeout_s)
            ctx.save_state("ssh_banner", transport.remote_version)
            transport.auth_password(username=user, password=password)
            ctx.save_state("ssh_authenticated", transport.is_authenticated())
        finally:
            _close_quietly(transport)
            _close_quietly(sock)


# ===========================================================================
# TLS / OpenSSL (valid client session; use SDKs for record mutation)
# ===========================================================================
if False:
    @step_handler("MODEL_STEP_NAME")
    def tls_handshake(ctx: Ctx, params: dict[str, Any]) -> None:
        server_name = _text_field(ctx, params, "server_name", ctx.host)
        payload = _bytes_field(ctx, params, "payload", b"mcn-tls")
        context = ssl.create_default_context()
        # For a deliberate test peer without a trusted CA:
        # context.check_hostname = False
        # context.verify_mode = ssl.CERT_NONE
        raw = socket.create_connection((ctx.host, ctx.port), timeout=ctx.timeout_s)
        connection = None
        try:
            connection = context.wrap_socket(raw, server_hostname=server_name)
            connection.settimeout(ctx.timeout_s)
            connection.sendall(payload)
            data = connection.recv(4096)
            ctx.save_state("tls_version", connection.version())
            ctx.save_state("tls_cipher", connection.cipher())
            ctx.save_state("tls_response", data)
        finally:
            _close_quietly(connection)
            _close_quietly(raw)


# ===========================================================================
# Protocol module map (imports used by the examples above)
# ===========================================================================
# Standard library:
#   socket, ssl, struct, base64, hashlib, hmac, json, time, tempfile
#   ftplib, smtplib, email.message, email.policy
# Available third-party modules (see the protocol-modules skill inventory):
#   DAAP: httpx, aiohttp, pyatv.protocols.dmap, zeroconf
#   DICOM: pydicom, pynetdicom, pynetdicom.sop_class
#   DNS: dns.message, dns.query, dns.edns, dnslib, dnslib.server
#   DTLS: mbedtls.tls, mbedtls.pk, mbedtls.x509
#   FTP: ftplib, pyftpdlib.handlers, pyftpdlib.authorizers,
#        pyftpdlib.servers, aioftp
#   RTSP: aiortsp.rtsp.parser, aiortsp.rtsp.connection,
#         aiortsp.rtcp.parser, dpkt.rtp, scapy.layers.rtp
#   SIP: twisted.protocols.sip
#   SMTP: smtplib, aiosmtplib, aiosmtpd.smtp, aiosmtpd.controller
#   SSH: paramiko, asyncssh
#   TLS: ssl, OpenSSL.SSL, tlslite.api, tlslite.messages,
#        scapy.layers.tls.all, cryptography, pyasn1, asn1crypto
#
# Keep protocol-SDK imports inside handlers (or inside @global_init when the
# import has service/thread side effects).  Module scope is imported once and
# shared by every forked case worker.
