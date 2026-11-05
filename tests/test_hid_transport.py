"""Offline protocol checks: no real sockets, session files, or device inputs."""

import base64
import hashlib
import struct
import unittest
from unittest.mock import Mock, patch

from nanokvmgo_mcp import hid_transport as transport


CERTIFICATE = b"unit-test-certificate"
FINGERPRINT = hashlib.sha256(CERTIFICATE).hexdigest()
COOKIE = "test-session=offline-only"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeSocket:
    def __init__(self, chunks=(), upgrade=None):
        self.chunks = list(chunks)
        self.upgrade = upgrade
        self.sent = []
        self.attempts = 0
        self.closed = False
        self.shutdowns = []
        self.timeout = None
        self.send_error = None
        self.options = []

    def getpeercert(self, binary_form=False):
        if not binary_form:
            raise AssertionError("The certificate pin must inspect DER bytes")
        return CERTIFICATE

    def sendall(self, data):
        self.attempts += 1
        if self.send_error:
            raise self.send_error
        self.sent.append(bytes(data))
        if self.upgrade is not None and data.startswith(b"GET "):
            key = next(line.split(b":", 1)[1].strip() for line in
                       data.split(b"\r\n") if line.startswith(b"Sec-WebSocket-Key:"))
            self.chunks.extend(self.upgrade(key.decode("ascii")))

    def recv(self, count):
        if not self.chunks:
            return b""
        part = self.chunks.pop(0)
        if len(part) > count:
            self.chunks.insert(0, part[count:])
            return part[:count]
        return part

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))

    def pending(self):
        return bool(self.chunks)

    def shutdown(self, how):
        self.shutdowns.append(how)

    def close(self):
        self.closed = True


def upgrade_response(key, *, status="101 Switching Protocols", accept=None,
                     upgrade="websocket", connection="Upgrade", pending=b""):
    if accept is None:
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    head = (f"HTTP/1.1 {status}\r\nUpgrade: {upgrade}\r\nConnection: {connection}\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii")
    return head + pending


def decode_client_frame(frame):
    """Independent RFC6455 decoder, asserting the browser/client masking rules."""
    if len(frame) < 6 or frame[0] & 0x70 or not frame[0] & 0x80:
        raise AssertionError("Malformed client WebSocket frame")
    if not frame[1] & 0x80:
        raise AssertionError("Client frames must be masked")
    size, offset = frame[1] & 127, 2
    if size == 126:
        size, offset = struct.unpack("!H", frame[2:4])[0], 4
    elif size == 127:
        raise AssertionError("The bounded client must not use 64-bit lengths")
    mask = frame[offset:offset + 4]
    body = frame[offset + 4:]
    if len(body) != size:
        raise AssertionError("WebSocket frame length mismatch")
    return frame[0] & 15, bytes(value ^ mask[i % 4] for i, value in enumerate(body))


def make_client(sock=None):
    client = transport.PinnedInputSocket(Mock(), "192.0.2.1", "localhost", FINGERPRINT, COOKIE)
    client.sock = sock
    return client


class RelativeProtocolTests(unittest.TestCase):
    def test_frame_matches_official_header_and_signed_little_endian_payload(self):
        sock = FakeSocket()
        client = make_client(sock)
        with patch.object(transport.time, "monotonic", return_value=1234.0):
            client.relative(buttons=19, dx=-32768, dy=32767, wheel=-256)
        opcode, packet = decode_client_frame(sock.sent[0])
        self.assertEqual(opcode, 2)
        self.assertEqual(len(packet), 20)
        self.assertEqual(packet[:5], bytes((3, 1, 2, 2, 0)))
        self.assertEqual(struct.unpack("<II", packet[5:13]), (1, 1234000))
        self.assertEqual(packet[13], 19)
        self.assertEqual(packet[14:], b"\x00\x80\xff\x7f\x00\xff")

    def test_drag_retains_button_state_and_release_is_explicit_on_same_source(self):
        sock = FakeSocket()
        client = make_client(sock)
        client.relative(buttons=1)
        client.relative(buttons=1, dy=5)
        client.release()
        packets = [decode_client_frame(frame)[1] for frame in sock.sent]
        self.assertEqual([packet[2] for packet in packets], [2, 2, 2])
        self.assertEqual([packet[13] for packet in packets], [1, 1, 0])
        self.assertEqual([struct.unpack("<I", packet[5:9])[0] for packet in packets], [1, 2, 3])
        self.assertEqual(packets[-1][13:], bytes(7))

    def test_sequence_and_timestamp_wrap_as_uint32(self):
        sock = FakeSocket()
        client = make_client(sock)
        client.sequence = 0xFFFFFFFF
        with patch.object(transport.time, "monotonic", return_value=(2**32 + 1024) / 1000):
            client.relative()
        packet = decode_client_frame(sock.sent[0])[1]
        self.assertEqual(struct.unpack("<II", packet[5:13]), (0, 1024))

    def test_invalid_movement_never_sends_input(self):
        invalid = [dict(buttons=-1), dict(buttons=32), dict(buttons=True),
                   dict(dx=-32769), dict(dy=32768), dict(wheel=1.5),
                   dict(dx=True), dict(dy="2")]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                sock = FakeSocket()
                client = make_client(sock)
                with self.assertRaises(ValueError):
                    client.relative(**arguments)
                self.assertEqual(sock.sent, [])
                self.assertEqual(client.sequence, 0)


class ClientFrameTests(unittest.TestCase):
    def test_every_client_frame_is_masked_at_short_and_extended_length_boundaries(self):
        for size in [0, 1, 125, 126, 255, 65535]:
            with self.subTest(size=size):
                sock = FakeSocket()
                client = make_client(sock)
                payload = bytes(index % 251 for index in range(size))
                with patch.object(transport.os, "urandom", return_value=b"\x12\x34\x56\x78"):
                    client._send(2, payload)
                opcode, decoded = decode_client_frame(sock.sent[0])
                self.assertEqual((opcode, decoded), (2, payload))
                self.assertEqual(sock.sent[0][1] & 127, size if size < 126 else 126)

    def test_oversized_client_frame_is_rejected_without_write(self):
        sock = FakeSocket()
        client = make_client(sock)
        with self.assertRaises(ValueError):
            client._send(2, bytes(65536))
        self.assertEqual(sock.sent, [])

    def test_send_failure_stops_connection_and_never_retries_or_echoes_exception(self):
        sock = FakeSocket()
        sock.send_error = OSError("private transport detail")
        client = make_client(sock)
        with self.assertRaises(transport.InputTransportError) as raised:
            client.relative(dy=5)
        self.assertTrue(client.stopped.is_set())
        self.assertNotIn("private transport detail", str(raised.exception))
        with self.assertRaises(transport.InputTransportError):
            client.relative(dy=5)
        self.assertEqual(sock.attempts, 1)

    def test_closed_connection_does_not_write(self):
        for sock in [None, FakeSocket()]:
            with self.subTest(sock=sock):
                client = make_client(sock)
                client.stopped.set()
                with self.assertRaises(transport.InputTransportError):
                    client.relative()
                if sock:
                    self.assertEqual(sock.sent, [])


class UpgradeAndTlsTests(unittest.TestCase):
    def connect_with_fake(self, *, response=None, fingerprint=FINGERPRINT, wrap_error=None):
        raw = FakeSocket()
        tls = FakeSocket(upgrade=lambda key: response(key) if response else [upgrade_response(key)])
        context = Mock()
        if wrap_error:
            context.wrap_socket.side_effect = wrap_error
        else:
            context.wrap_socket.return_value = tls
        client = transport.PinnedInputSocket(context, "192.0.2.1", "localhost", fingerprint, COOKIE)
        socket_patch = patch.object(transport.socket, "create_connection", return_value=raw)
        thread_patch = patch.object(transport.threading, "Thread")
        socket_factory, thread_factory = socket_patch.start(), thread_patch.start()
        self.addCleanup(socket_patch.stop)
        self.addCleanup(thread_patch.stop)
        return client, raw, tls, context, socket_factory, thread_factory

    def test_valid_upgrade_preserves_pending_frames_and_tls_hostname(self):
        close = b"\x88\x02" + struct.pack("!H", 4001)
        client, raw, tls, context, socket_factory, thread_factory = self.connect_with_fake(
            response=lambda key: [upgrade_response(key, connection="keep-alive, Upgrade", pending=close)])
        self.assertIs(client.connect(), client)
        socket_factory.assert_called_once_with(("192.0.2.1", 443), timeout=3)
        self.assertEqual(raw.options, [(transport.socket.IPPROTO_TCP,
                                        transport.socket.TCP_NODELAY, 1)])
        context.wrap_socket.assert_called_once_with(raw, server_hostname="localhost")
        request = tls.sent[0]
        self.assertIn(b"GET /api/ws HTTP/1.1\r\n", request)
        self.assertIn(("Cookie: " + COOKIE + "\r\n").encode(), request)
        self.assertIn(b"Origin: https://192.0.2.1\r\n", request)
        self.assertEqual(thread_factory.call_args.kwargs["args"], (close,))
        thread_factory.return_value.start.assert_called_once()

    def test_fragmented_upgrade_header_is_assembled_before_starting_reader(self):
        def response(key):
            value = upgrade_response(key)
            return [value[:1], value[1:23], value[23:-2], value[-2:]]
        client, _, tls, _, _, factory = self.connect_with_fake(response=response)
        client.connect()
        self.assertFalse(tls.closed)
        self.assertEqual(factory.call_args.kwargs["args"], (b"",))

    def test_wrong_pin_closes_tls_before_cookie_or_any_http_byte_is_sent(self):
        client, _, tls, _, _, factory = self.connect_with_fake(fingerprint="0" * 64)
        with self.assertRaises(transport.InputTransportError):
            client.connect()
        self.assertTrue(tls.closed)
        self.assertEqual(tls.sent, [])
        self.assertIsNone(client.sock)
        factory.assert_not_called()

    def test_normal_tls_failure_closes_raw_socket_without_credentials(self):
        client, raw, tls, _, _, factory = self.connect_with_fake(wrap_error=OSError("TLS failed"))
        with self.assertRaises(OSError):
            client.connect()
        self.assertTrue(raw.closed)
        self.assertEqual(tls.sent, [])
        factory.assert_not_called()

    def test_bad_upgrade_accept_status_and_headers_are_rejected(self):
        cases = [dict(accept="invalid"), dict(status="401 Unauthorized"),
                 dict(upgrade="http"), dict(connection="keep-alive")]
        for changes in cases:
            with self.subTest(changes=changes):
                client, _, tls, _, _, factory = self.connect_with_fake(
                    response=lambda key, changes=changes: [upgrade_response(key, **changes)])
                with self.assertRaises(transport.InputTransportError):
                    client.connect()
                self.assertTrue(tls.closed)
                self.assertIsNone(client.sock)
                factory.assert_not_called()

    def test_oversized_or_truncated_upgrade_never_starts_input_reader(self):
        for chunks in [[b"x" * 16385], [b"HTTP/1.1 101", b""]]:
            with self.subTest(size=sum(map(len, chunks))):
                client, _, tls, _, _, factory = self.connect_with_fake(response=lambda key: chunks)
                with self.assertRaises(transport.InputTransportError):
                    client.connect()
                self.assertTrue(tls.closed)
                factory.assert_not_called()

    def test_cookie_header_injection_is_rejected_before_network(self):
        with patch.object(transport.socket, "create_connection") as create:
            for cookie in ["", "session=x\r\nInjected: true", "session=x\nOther: value"]:
                with self.subTest(cookie=cookie):
                    with self.assertRaises(transport.InputTransportError):
                        transport.PinnedInputSocket(Mock(), "192.0.2.1", "localhost", FINGERPRINT, cookie)
            create.assert_not_called()


class ServerLifecycleTests(unittest.TestCase):
    def test_auth_close_codes_stop_input_without_reconnect_or_reason_disclosure(self):
        for code in [4001, 4002, 4003]:
            with self.subTest(code=code):
                sock = FakeSocket()
                client = make_client(sock)
                reason = b"private close reason"
                body = struct.pack("!H", code) + reason
                with patch.object(transport.socket, "create_connection") as reconnect:
                    client._read(bytes((0x88, len(body))) + body)
                    with self.assertRaises(transport.InputTransportError):
                        client.relative(dy=1)
                    reconnect.assert_not_called()
                self.assertTrue(client.stopped.is_set())
                self.assertEqual(client.closed_code, code)
                self.assertNotIn(reason.decode(), client.error)
                self.assertEqual(sock.sent, [])

    def test_server_ping_gets_masked_pong_then_close_stops_loop(self):
        sock = FakeSocket()
        client = make_client(sock)
        client._read(b"\x89\x03hey\x88\x02" + struct.pack("!H", 1000))
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(decode_client_frame(sock.sent[0]), (10, b"hey"))
        self.assertEqual(client.closed_code, 1000)

    def test_masked_or_reserved_server_frames_stop_connection(self):
        for frame in [b"\x82\x80", b"\xc2\x00"]:
            with self.subTest(frame=frame):
                client = make_client(FakeSocket())
                client._read(frame)
                self.assertTrue(client.stopped.is_set())
                self.assertEqual(client.sock.sent, [])

    def test_server_64bit_length_limit_stops_before_payload_read(self):
        client = make_client(FakeSocket())
        client._read(b"\x82\x7f" + struct.pack("!Q", 1048577))
        self.assertTrue(client.stopped.is_set())
        self.assertEqual(client.sock.sent, [])

    def test_close_is_idempotent_and_sends_masked_normal_close_once(self):
        sock = FakeSocket()
        client = make_client(sock)
        client.close()
        client.close()
        self.assertTrue(sock.closed)
        self.assertTrue(client.stopped.is_set())
        self.assertIsNone(client.sock)
        self.assertEqual(len(sock.shutdowns), 1)
        self.assertEqual(len(sock.sent), 1)
        self.assertEqual(decode_client_frame(sock.sent[0]), (8, struct.pack("!H", 1000)))


if __name__ == "__main__":
    unittest.main()
