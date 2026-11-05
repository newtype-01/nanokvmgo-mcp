#!/usr/bin/env python3
"""Stable stdio entrypoint; refresh compatible implementation at request boundaries.

The public MCP tools/schema still require the MCP client's normal refresh. This
loader only avoids process restarts for compatible implementation changes.
"""

import hashlib
import importlib.util
import json
import pathlib
import signal
import sys


MODULE_FILES = (
    ("nanokvmgo_mcp.config", "config.py"),
    ("nanokvmgo_mcp.hid_transport", "hid_transport.py"),
    ("nanokvmgo_mcp.phone_vision", "phone_vision.py"),
    ("nanokvmgo_mcp.phone_control", "phone_control.py"),
    ("nanokvmgo_mcp.local_reader", "local_reader.py"),
    ("nanokvmgo_mcp.read_chat", "read_chat.py"),
    ("nanokvmgo_mcp.bridge_runtime", "bridge_runtime.py"),
)
MISSING = object()
RELOAD_ERROR = "NanoKVM implementation update failed; retaining the working version.\n"
START_ERROR = "NanoKVM implementation could not be loaded.\n"
CLEANUP_ERROR = "NanoKVM connection cleanup failed.\n"


class HotReloadBridge:
    """One serial Bridge instance with transactional, source-hash-based refresh.

Bridge versions must support ordinary __dict__ state and compatible class
replacement. Bridge.close() must be idempotent and clear cached phone connection
objects so the same instance remains usable after a failed update. Authentication
and MCP session state stay on the original instance; __init__ is not run again.
"""

    def __init__(self, base, stderr=None):
        self.base = pathlib.Path(base).resolve()
        self.stderr = stderr if stderr is not None else sys.stderr
        self.bridge = None
        self.runtime = None
        self.working_signature = None
        self.attempted_signature = None
        self.io_failed = False

    def _warn(self, text):
        self.stderr.write(text)
        self.stderr.flush()

    def _snapshot(self):
        sources = {}
        signature = []
        for name, filename in MODULE_FILES:
            try:
                source = (self.base / filename).read_bytes()
            except FileNotFoundError:
                source = None
            sources[name] = source
            signature.append((name, hashlib.sha256(source).digest() if source is not None else None))
        return sources, tuple(signature)

    def _load(self, sources):
        """Execute one source snapshot without accepting stale timestamp pyc files."""
        previous_modules = {name: sys.modules.get(name, MISSING) for name, _ in MODULE_FILES}
        # Relative imports also inspect attributes on the containing package.
        # Update/restore both tables so an import cannot retain a stale binding
        # after success, or expose a half-loaded implementation after failure.
        packages = {name.rpartition(".")[0]: sys.modules[name.rpartition(".")[0]]
                    for name, _ in MODULE_FILES}
        previous_attributes = {
            name: getattr(packages[name.rpartition(".")[0]], name.rpartition(".")[2], MISSING)
            for name, _ in MODULE_FILES
        }
        old_class = type(self.bridge) if self.bridge is not None else None
        try:
            for name, _ in MODULE_FILES:
                sys.modules.pop(name, None)
                package_name, _, attribute = name.rpartition(".")
                if hasattr(packages[package_name], attribute):
                    delattr(packages[package_name], attribute)
            for name, filename in MODULE_FILES:
                source = sources[name]
                if source is None:
                    raise ImportError("Implementation source is unavailable")
                path = self.base / filename
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                package_name, _, attribute = name.rpartition(".")
                setattr(packages[package_name], attribute, module)
                exec(compile(source, str(path), "exec"), module.__dict__)
            runtime = sys.modules.get(MODULE_FILES[-1][0])
            if runtime is None:
                raise ImportError("Runtime is unavailable")
            bridge_class = runtime.Bridge
            if not isinstance(bridge_class, type) or not callable(getattr(bridge_class, "handle", None)):
                raise TypeError("Runtime does not provide a compatible Bridge")
            if self.bridge is None:
                # The runtime loads its user configuration. base names source
                # files only and must never become a credential/config path.
                instance = bridge_class()
            else:
                # Compatible class replacement preserves context, credentials,
                # session_id, protocol_version, base and every other state field.
                self.bridge.__class__ = bridge_class
                instance = self.bridge
            self.bridge = instance
            self.runtime = runtime
        except BaseException:
            if old_class is not None and type(self.bridge) is not old_class:
                self.bridge.__class__ = old_class
            for name, previous in previous_modules.items():
                if previous is MISSING:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = previous
                package_name, _, attribute = name.rpartition(".")
                old_attribute = previous_attributes[name]
                if old_attribute is MISSING:
                    if hasattr(packages[package_name], attribute):
                        delattr(packages[package_name], attribute)
                else:
                    setattr(packages[package_name], attribute, old_attribute)
            raise

    def refresh(self):
        """Refresh before dispatch only; a failed update keeps the previous version."""
        # The local installer holds this marker while replacing a multi-file
        # implementation, so requests keep using the complete working version.
        if self.bridge is not None and (self.base / ".updating").exists():
            return False
        try:
            sources, signature = self._snapshot()
            self.io_failed = False
        except Exception:
            if self.bridge is None:
                raise
            if not self.io_failed:
                self._warn(RELOAD_ERROR)
            self.io_failed = True
            return False
        if signature == self.working_signature or signature == self.attempted_signature:
            return False
        self.attempted_signature = signature
        if self.bridge is None:
            self._load(sources)
        else:
            try:
                close = getattr(self.bridge, "close", None)
                if callable(close):
                    close()
                self._load(sources)
            except Exception:
                self._warn(RELOAD_ERROR)
                return False
        self.working_signature = signature
        return True

    def close(self):
        close = getattr(self.bridge, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                self._warn(CLEANUP_ERROR)


def _response_error(code, message, identifier=None):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def serve(base=None, stdin=None, stdout=None, stderr=None, install_signals=False):
    """Process stdin serially; no input action is automatically retried."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    base = pathlib.Path(base) if base is not None else pathlib.Path(__file__).resolve().parent
    loader = HotReloadBridge(base, stderr)
    previous_term = None

    def terminate(signum, frame):
        raise KeyboardInterrupt

    def emit(response):
        stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        stdout.flush()

    try:
        if install_signals:
            previous_term = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, terminate)
        try:
            loader.refresh()
        except Exception:
            stderr.write(START_ERROR)
            stderr.flush()
            return 1
        for line in stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                emit(_response_error(-32700, "Invalid JSON"))
                continue
            if not isinstance(message, dict):
                emit(_response_error(-32600, "Invalid JSON-RPC request"))
                continue
            loader.refresh()
            try:
                response = loader.bridge.handle(message)
            except Exception:
                response = (_response_error(-32603, "NanoKVM request failed", message["id"])
                            if "id" in message else None)
            if response is not None:
                emit(response)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        loader.close()
        if install_signals and previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    raise SystemExit(serve(install_signals=True))
