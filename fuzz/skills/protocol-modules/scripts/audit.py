#!/usr/bin/env python3
"""Audit the campaign interpreter; no dependency installation or SUT startup."""

from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata
import io
import json
from pathlib import Path
import subprocess
import sys

SKILL = Path(__file__).resolve().parent.parent


def inventory():
    rows = json.loads((SKILL / "references/modules.json").read_text())["modules"]
    results = []
    for row in rows:
        result = {"distribution": row["distribution"], "expected": row["version"]}
        errors = []
        try:
            result["version"] = importlib.metadata.version(row["distribution"])
            if result["version"] != row["version"]:
                errors.append("installed version differs from the prepared inventory")
        except importlib.metadata.PackageNotFoundError as exc:
            errors.append(str(exc))
        for module in row["imports"]:
            try:
                importlib.import_module(module)
            except Exception as exc:
                errors.append(f"{module}: {type(exc).__name__}: {exc}")
        result["status"] = "failed" if errors else "passed"
        if errors:
            result["errors"] = errors
        results.append(result)
    return results


def daap():
    from pyatv.protocols.dmap import parser, tag_definitions, tags

    wire = tags.container_tag("mlog", tags.uint32_tag("mstt", 200)
                              + tags.uint32_tag("mlid", 1))
    decoded = parser.parse(wire, tag_definitions.lookup_tag)
    assert parser.first(decoded, "mlog", "mlid") == 1
    return "DMAP login-response tags encoded/decoded"


def dicom():
    from pydicom import Dataset
    from pydicom.filebase import DicomBytesIO
    from pydicom.filereader import read_dataset
    from pydicom.filewriter import write_dataset

    ds = Dataset()
    ds.PatientName = "Generator^Probe"
    buffer = DicomBytesIO()
    buffer.is_implicit_VR = False
    buffer.is_little_endian = True
    write_dataset(buffer, ds)
    buffer.seek(0)
    decoded = read_dataset(buffer, is_implicit_VR=False, is_little_endian=True)
    assert decoded.PatientName == ds.PatientName
    return "DICOM explicit-VR little-endian dataset roundtrip"


def dns():
    import dns.message
    from dnslib import A, DNSRecord, RR
    from scapy.layers.dns import DNS

    query = dns.message.make_query("example.test.", "A", use_edns=True)
    request = DNSRecord.parse(query.to_wire())
    reply = request.reply()
    reply.add_answer(RR("example.test.", rdata=A("127.0.0.7")))
    wire = bytes(reply.pack())
    parsed = dns.message.from_wire(wire)
    assert parsed.id == query.id and parsed.answer[0][0].address == "127.0.0.7"
    assert DNS(bytes(wire)).id == query.id
    return "dnspython/dnslib/Scapy DNS interoperability"


def dtls():
    from mbedtls import tls

    common = dict(validate_certificates=False,
                  lowest_supported_version=tls.DTLSVersion.DTLSv1_2,
                  highest_supported_version=tls.DTLSVersion.DTLSv1_2)
    cipher = "TLS-PSK-WITH-AES-128-CCM-8"
    assert cipher in tls.ciphers_available()
    common["ciphers"] = (cipher,)
    key = b"0123456789abcdef"
    client = tls.ClientContext(tls.DTLSConfiguration(
        **common, pre_shared_key=("probe", key))).wrap_buffers(None)
    server = tls.ServerContext(tls.DTLSConfiguration(
        **common, pre_shared_key_store={"probe": key})).wrap_buffers()
    server.setcookieparam(b"loopback-probe")
    done = set()

    def transfer(source, destination):
        wire = source.peek_outgoing(65536)
        if wire:
            destination.receive_from_network(wire)
            source.consume_outgoing(len(wire))

    # This version advances a handshake step per call and signals a completed
    # handshake with ValueError on the next call. Do not infer completion from
    # an early None return. The application-data assertion below checks it.
    for _ in range(100):
        for name, peer, other in [("client", client, server), ("server", server, client)]:
            if name not in done:
                try:
                    peer.do_handshake()
                except (tls.WantReadError, tls.WantWriteError):
                    pass
                except tls.HelloVerifyRequest:
                    peer.setcookieparam(b"loopback-probe")
                except ValueError as exc:
                    if str(exc) != "handshake already over":
                        raise
                    done.add(name)
            transfer(peer, other)
        if len(done) == 2:
            break
    assert len(done) == 2, "DTLS handshake exceeded bounded step count"
    assert client.negotiated_tls_version() == tls.DTLSVersion.DTLSv1_2
    client.write(b"encrypted probe")
    transfer(client, server)
    assert server.read(1024) == b"encrypted probe"
    return "DTLS 1.2 PSK/AES-128-CCM-8 cookie handshake and encrypted application data (memory transport)"


def ftp():
    import ftplib
    import aioftp
    from pyftpdlib.handlers import TLS_FTPHandler

    assert ftplib.parse227("227 Entering Passive Mode (127,0,0,1,8,1)") == ("127.0.0.1", 2049)
    assert callable(aioftp.Server.start) and callable(TLS_FTPHandler)
    return "FTP passive-address parsing and FTP/FTPS server APIs"


def rtsp():
    from aiortsp.rtsp.parser import RTSPParser
    from dpkt.rtp import RTP

    parser = RTSPParser()
    assert not list(parser.parse(b"RTSP/1.0 200 OK\r\nCSeq:"))
    messages = list(parser.parse(b" 9\r\n\r\n$\x00\x00\x03abc"))
    assert messages[0].cseq == 9 and messages[1].data == b"abc"
    rtp = RTP(seq=12, ts=345, ssrc=456, data=b"payload")
    assert RTP(bytes(rtp)).data == b"payload"
    return "fragmented RTSP response, interleaved frame and RTP serialization"


def sip():
    from twisted.protocols import sip

    message = sip.Request("OPTIONS", "sip:user@example.test")
    message.addHeader("via", "SIP/2.0/UDP 127.0.0.1:12345;branch=z9hG4bK-probe")
    message.addHeader("cseq", "1 OPTIONS")
    message.addHeader("content-length", "0")
    text = message.toString()
    messages = []
    parser = sip.MessagesParser(messages.append)
    parser.dataReceived(text)
    parser.dataDone()
    assert len(messages) == 1 and messages[0].method == "OPTIONS"
    assert text.encode("ascii").startswith(b"OPTIONS sip:")
    return "Twisted SIP request serialization and parser callback"


def smtp():
    from email import policy
    from email.message import EmailMessage
    from email.parser import BytesParser

    msg = EmailMessage(policy=policy.SMTP)
    msg["From"] = "probe@example.test"
    msg["To"] = "recipient@example.test"
    msg["Subject"] = "probe"
    msg.set_content("bounded payload")
    wire = msg.as_bytes()
    assert b"\r\n\r\n" in wire
    assert BytesParser(policy=policy.SMTP).parsebytes(wire)["Subject"] == "probe"
    return "SMTP MIME/CRLF message roundtrip"


def ssh():
    import asyncssh
    import paramiko

    key = asyncssh.generate_private_key("ssh-ed25519")
    decoded = asyncssh.import_private_key(key.export_private_key())
    assert decoded.export_public_key() == key.export_public_key()
    assert callable(paramiko.Transport.start_server)
    assert callable(paramiko.ServerInterface.check_auth_password)
    return "SSH Ed25519 key serialization and synchronous server APIs"


def tls():
    import ssl
    from dpkt.ssl import TLSRecord
    from tlslite.messages import ClientHello
    from scapy.layers.tls.all import TLS

    hello = ClientHello().create((3, 3), bytearray(range(32)), bytearray(), [0x002F])
    record = TLSRecord(type=22, version=0x0303, data=bytes(hello.write()))
    wire = bytes(record)
    assert TLSRecord(wire).data == bytes(hello.write())
    assert TLS(wire).msg[0].msgtype == 1
    assert ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    return "tlslite ClientHello, dpkt TLS record and Scapy handshake parsing"


def codecs_crypto():
    import bitstring
    import cbor2
    from construct import Bytes, Int16ub, Struct, this
    from Crypto.Cipher import AES
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from nacl.signing import SigningKey
    from pyasn1.codec.der import decoder, encoder
    from pyasn1.type.univ import Integer

    codec = Struct("length" / Int16ub, "payload" / Bytes(this.length))
    assert codec.parse(codec.build(dict(length=3, payload=b"abc"))).payload == b"abc"
    assert bitstring.pack("uint:3, uint:5", 5, 17).unpack("uint:3, uint:5") == [5, 17]
    assert int(decoder.decode(encoder.encode(Integer(42)))[0]) == 42
    assert cbor2.loads(cbor2.dumps({"probe": [1, True, "ok"]}))["probe"][0] == 1
    key, nonce, aad = b"k" * 16, b"n" * 12, b"header"
    encrypted = AESGCM(key).encrypt(nonce, b"payload", aad)
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    cipher.update(aad)
    assert cipher.decrypt_and_verify(encrypted[:-16], encrypted[-16:]) == b"payload"
    signing_key = SigningKey.generate()
    assert signing_key.verify_key.verify(signing_key.sign(b"probe")) == b"probe"
    return "binary/bit/DER/CBOR codecs, cryptography-to-PyCryptodome AES-GCM and PyNaCl signatures"


def dns_network():
    import dns.query
    import dns.message
    from dnslib import A, RR
    from dnslib.server import BaseResolver, DNSLogger, DNSServer

    class Resolver(BaseResolver):
        def resolve(self, request, handler):
            reply = request.reply()
            reply.add_answer(RR(request.q.qname, rdata=A("127.0.0.7")))
            return reply

    server = DNSServer(Resolver(), address="127.0.0.1", port=0,
                       logger=DNSLogger("-request,-reply,-truncated,-error"))
    server.start_thread()
    try:
        query = dns.message.make_query("probe.test.", "A")
        reply = dns.query.udp(query, "127.0.0.1", port=server.server.server_address[1], timeout=2)
        assert reply.id == query.id and reply.answer[0][0].address == "127.0.0.7"
    finally:
        server.stop()
        server.server.server_close()
        server.thread.join(2)
    assert not server.thread.is_alive()
    return "loopback DNS UDP upstream query/response"


def ftp_network():
    import ftplib
    import tempfile
    import threading
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.ioloop import IOLoop
    from pyftpdlib.servers import FTPServer

    with tempfile.TemporaryDirectory(prefix="mcn-ftp-probe-") as root:
        authorizer = DummyAuthorizer()
        authorizer.add_user("probe", "secret", root, perm="elr")

        class Handler(FTPHandler):
            pass

        Handler.authorizer = authorizer
        loop = IOLoop()
        server = FTPServer(("127.0.0.1", 0), Handler, ioloop=loop)
        stop = threading.Event()

        def serve():
            try:
                while not stop.is_set():
                    server.serve_forever(timeout=0.02, blocking=False, handle_exit=False)
            finally:
                server.close_all()
                loop.close()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with ftplib.FTP(timeout=2) as client:
                client.connect(*server.socket.getsockname())
                client.login("probe", "secret")
                assert client.pwd() == "/"
                assert client.nlst() == []
        finally:
            stop.set()
            thread.join(2)
        assert not thread.is_alive()
    return "loopback FTP login, PWD and passive data transfer"


def smtp_network():
    import asyncio
    from aiosmtpd.smtp import SMTP
    import aiosmtplib

    async def run():
        received = []

        class Handler:
            async def handle_DATA(self, server, session, envelope):
                received.append(envelope.content)
                return "250 accepted"

        server = await asyncio.get_running_loop().create_server(
            lambda: SMTP(Handler()), "127.0.0.1", 0)
        try:
            port = server.sockets[0].getsockname()[1]
            async with aiosmtplib.SMTP(hostname="127.0.0.1", port=port, timeout=2,
                                      start_tls=False) as client:
                await client.sendmail("probe@example.test", ["rcpt@example.test"],
                                      "Subject: probe\r\n\r\npayload")
            assert len(received) == 1 and b"payload" in received[0]
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(run(), 5))
    return "loopback SMTP EHLO, MAIL/RCPT/DATA and QUIT"


def dicom_network():
    from pynetdicom import AE
    from pynetdicom.sop_class import Verification

    server_ae = AE(ae_title="PROBE_SCP")
    server_ae.add_supported_context(Verification)
    server = server_ae.start_server(("127.0.0.1", 0), block=False)
    assoc = None
    try:
        client = AE(ae_title="PROBE_SCU")
        client.add_requested_context(Verification)
        client.acse_timeout = client.dimse_timeout = client.network_timeout = 2
        assoc = client.associate(*server.server_address)
        assert assoc.is_established
        assert assoc.send_c_echo().Status == 0
        assoc.release()
    finally:
        if assoc is not None and assoc.is_established:
            assoc.abort()
        server.shutdown()
        server.server_close()
    return "loopback DICOM association, C-ECHO and release"


def ssh_network():
    import asyncio
    import asyncssh

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return False  # This temporary test server deliberately requires no auth.

    async def run():
        server = await asyncssh.create_server(Server, "127.0.0.1", 0,
                         server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")])
        try:
            async with asyncssh.connect("127.0.0.1", port=server.get_port(), username="probe",
                       known_hosts=None, client_keys=[], agent_path=None,
                       connect_timeout=2, login_timeout=2) as client:
                assert client.get_server_host_key().get_algorithm() == "ssh-ed25519"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(asyncio.wait_for(run(), 5))
    return "loopback SSH key exchange and authentication"


def http_network():
    import asyncio
    from aiohttp import web
    import httpx
    from pyatv.protocols.dmap import parser, tag_definitions, tags

    async def run():
        async def login(request):
            body = tags.container_tag("mlog", tags.uint32_tag("mlid", 7))
            return web.Response(body=body, content_type="application/x-dmap-tagged")

        app = web.Application()
        app.router.add_get("/login", login)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", 0).start()
            port = runner.addresses[0][1]
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                response = await client.get(f"http://127.0.0.1:{port}/login")
                response.raise_for_status()
                decoded = parser.parse(response.content, tag_definitions.lookup_tag)
                assert parser.first(decoded, "mlog", "mlid") == 7
        finally:
            await runner.cleanup()

    asyncio.run(asyncio.wait_for(run(), 5))
    return "loopback HTTP transport of DMAP tagged response (not a full DAAP server)"


OFFLINE = ("daap", "dicom", "dns", "dtls", "ftp", "rtsp", "sip", "smtp", "ssh", "tls", "codecs_crypto")
NETWORK = ("dns_network", "ftp_network", "smtp_network", "dicom_network", "ssh_network", "http_network")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="exercise temporary loopback peers")
    parser.add_argument("--report", type=Path, help="also save the JSON report")
    parser.add_argument("--probe", choices=("inventory",) + OFFLINE + NETWORK, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        # SDKs may print service logs. Keep stdout machine-readable.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                detail = globals()[args.probe]()
            failed = args.probe == "inventory" and any(row["status"] != "passed" for row in detail)
            result = {"name": args.probe, "status": "failed" if failed else "passed", "detail": detail}
        except Exception as exc:
            result = {"name": args.probe, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps(result))
        return 0 if result["status"] == "passed" else 1

    checks = []
    for name in ("inventory",) + OFFLINE + (NETWORK if args.network else ()):
        try:
            run = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--probe", name],
                                 capture_output=True, text=True, timeout=20)
            result = json.loads(run.stdout)
            if run.returncode and result["status"] == "passed":
                result = {"name": name, "status": "failed", "error": f"exit {run.returncode}"}
            checks.append(result)
        except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            checks.append({"name": name, "status": "failed", "error": str(exc)})
    report = {"python": sys.executable, "python_version": sys.version.split()[0],
              "status": "passed" if all(c["status"] == "passed" for c in checks) else "failed",
              "checks": checks}
    text = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(text)
    print(text, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
