"""The NanoKVM Go frontend's InputRouter protocol, over narrowly pinned TLS.

Only an existing web login cookie is used. No passwords, automatic logins, input
retries, or changes to device settings are performed by this module.
"""

import base64
import hashlib
import os
import select
import socket
import struct
import threading
import time


class InputTransportError(RuntimeError):
    pass


class PinnedInputSocket:
    def __init__(self, context, host, certificate_name, fingerprint, cookie, port=443, origin=None):
        if not cookie or "\r" in cookie or "\n" in cookie:
            raise InputTransportError("A valid local NanoKVM web session is required")
        self.context = context
        self.host, self.port = host, port
        self.certificate_name, self.fingerprint = certificate_name, fingerprint
        self.cookie = cookie
        authority = "[" + host + "]" if ":" in host else host
        self.authority = authority + (":" + str(port) if port != 443 else "")
        self.origin = origin or ("https://" + self.authority)
        self.sock = None
        self.sequence = 0
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.closed_code = None
        self.error = None
        self.reader = None

    def connect(self):
        raw = socket.create_connection((self.host, self.port), timeout=3)
        try:
            # Small held-motion and release reports must leave immediately;
            # waiting to coalesce them changes the timing of a real gesture.
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock = self.context.wrap_socket(raw, server_hostname=self.certificate_name)
            if self.fingerprint and hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest() != self.fingerprint:
                raise InputTransportError("NanoKVM certificate fingerprint changed")
            # Credentials are sent only after normal TLS and the exact pin pass.
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET /api/ws HTTP/1.1\r\nHost: {self.authority}\r\n"
                f"Origin: {self.origin}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\nCookie: {self.cookie}\r\n\r\n"
            )
            self.sock.sendall(request.encode("ascii"))
            data = bytearray()
            while b"\r\n\r\n" not in data:
                chunk = self.sock.recv(4096)
                if not chunk or len(data) + len(chunk) > 16384:
                    raise InputTransportError("Invalid NanoKVM WebSocket upgrade")
                data.extend(chunk)
            header, pending = bytes(data).split(b"\r\n\r\n", 1)
            lines = header.decode("iso-8859-1").split("\r\n")
            if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
                raise InputTransportError("NanoKVM web session rejected; sign in again")
            headers = {name.strip().lower(): value.strip() for name, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()).decode("ascii")
            if (headers.get("sec-websocket-accept", "") != expected
                    or headers.get("upgrade", "").lower() != "websocket"
                    or "upgrade" not in {token.strip().lower() for token in
                                         headers.get("connection", "").split(",")}):
                raise InputTransportError("Invalid NanoKVM WebSocket upgrade")
            self.sock.settimeout(1)
            self.reader = threading.Thread(target=self._read, args=(pending,), daemon=True)
            self.reader.start()
            return self
        except BaseException:
            if self.sock is not None:
                self.sock.close()
            else:
                raw.close()
            self.sock = None
            raise

    def _send(self, opcode, payload):
        if self.stopped.is_set() or self.sock is None:
            raise InputTransportError(self.error or "NanoKVM input connection is closed")
        mask = os.urandom(4)
        length = len(payload)
        if length > 65535:
            raise ValueError("Input frame is too large")
        header = bytes((0x80 | opcode, 0x80 | length)) if length < 126 else bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        masked = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
        try:
            with self.lock:
                self.sock.sendall(header + mask + masked)
        except Exception:
            self.error = "NanoKVM input send failed; input must not be retried"
            self.stopped.set()
            raise InputTransportError(self.error) from None

    def _read(self, pending):
        buffer = bytearray(pending)
        heartbeat = time.monotonic()
        try:
            while not self.stopped.is_set():
                if time.monotonic() - heartbeat >= 10:
                    self._send(2, b"\x00")
                    heartbeat = time.monotonic()
                while len(buffer) >= 2:
                    first, second = buffer[:2]
                    if second & 0x80 or first & 0x70:
                        raise InputTransportError("Invalid NanoKVM WebSocket frame")
                    size, offset = second & 127, 2
                    if size == 126:
                        if len(buffer) < 4:
                            break
                        size, offset = struct.unpack("!H", buffer[2:4])[0], 4
                    elif size == 127:
                        if len(buffer) < 10:
                            break
                        size, offset = struct.unpack("!Q", buffer[2:10])[0], 10
                    if size > 1048576:
                        raise InputTransportError("NanoKVM WebSocket frame exceeds limit")
                    if len(buffer) < offset + size:
                        break
                    payload = bytes(buffer[offset:offset + size])
                    del buffer[:offset + size]
                    opcode = first & 15
                    if opcode == 8:
                        self.closed_code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1000
                        self.error = "NanoKVM closed the input connection (code %s)" % self.closed_code
                        self.stopped.set()
                        return
                    if opcode == 9:
                        self._send(10, payload)
                    # Other messages are intentionally not logged or exposed.
                if self.sock.pending() or select.select([self.sock], [], [], 0.25)[0]:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise InputTransportError("NanoKVM input connection ended")
                    buffer.extend(chunk)
        except Exception:
            if not self.stopped.is_set():
                self.error = "NanoKVM input connection ended; do not replay the last gesture"
                self.stopped.set()

    def relative(self, buttons=0, dx=0, dy=0, wheel=0):
        if type(buttons) is not int or not 0 <= buttons <= 31:
            raise ValueError("Invalid mouse button state")
        if any(type(value) is not int or not -32768 <= value <= 32767 for value in (dx, dy, wheel)):
            raise ValueError("Invalid relative pointer movement")
        self.sequence = (self.sequence + 1) & 0xFFFFFFFF
        packet = struct.pack("<BBBBBIIBhhh", 3, 1, 2, 2, 0, self.sequence,
                             round(time.monotonic() * 1000) & 0xFFFFFFFF,
                             buttons, dx, dy, wheel)
        self._send(2, packet)

    def release(self):
        self.relative(0, 0, 0, 0)

    def close(self):
        if self.sock is not None:
            if not self.stopped.is_set():
                try:
                    self._send(8, struct.pack("!H", 1000))
                except Exception:
                    pass
            self.stopped.set()
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            self.sock.close()
            self.sock = None
        if self.reader is not None and self.reader is not threading.current_thread():
            self.reader.join(timeout=1)

    def __enter__(self):
        return self.connect()

    def __exit__(self, *unused):
        self.close()
