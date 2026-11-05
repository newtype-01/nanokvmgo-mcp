#!/usr/bin/env python3
"""NanoKVM MCP bridge with pinned TLS and bounded, fail-fast input batches."""

import copy
import hashlib
import http.client
import json
import pathlib
import re
import socket
import ssl
import sys
import time
import uuid

from .phone_control import PhoneController, TOOL_NAME as PHONE_TOOL_NAME, TOOL_SCHEMA as PHONE_SCHEMA, tool_definition as phone_tool
from .phone_vision import crop_phone
import base64
from .config import load_settings
from .read_chat import TOOL_NAME as READ_TOOL_NAME, TOOL_SCHEMA as READ_SCHEMA, tool_definition as read_tool, run_read_chat

BASE = pathlib.Path(__file__).resolve().parent
BATCH_TOOL_NAME = "batch_actions"
MAX_BATCH_SECONDS = 30
BATCH_INSTRUCTIONS = (
    "This bridge provides batch_actions: combine already-decided, user-authorized "
    "inputs and one final screenshot in a single call to reduce tool round trips. "
    "Use short batches; inspect the screen before any decision that depends on it. "
    "For this iPhone, relative mouse input and keyboard shortcuts are verified; "
    "absolute mouse input has not worked. Batch actions stop on failure and are never retried. "
    "Tool success confirms input dispatch only; verify the final screenshot. If input sticks "
    "after USB reconnection, use NanoKVM's Repair iPhone drag option. "
    "Prefer phone_control for the iPhone: one scroll call positions the visible cursor, "
    "performs a complete bounded drag with explicit release, and returns a fresh full-resolution "
    "phone-only screenshot. Up reveals older chat content; down reveals newer. Up to three "
    "already-authorized overlapping pages return one screenshot each. Ordinary screenshots "
    "and batch final screenshots are cropped locally when a portrait phone is confidently found. "
    "With phone_mode enabled, legacy scroll_mouse returns a screenshot and uses the bounded phone gesture; "
    "positive deltaY scrolls up, negative scrolls down. Never repeat a failed gesture blindly. Use read_chat for bounded continuous reading, local OCR, exact group checks and positional text deduplication in a single MCP call (macOS only)."
)


def object_schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def integer_schema(low, high, default=None):
    schema = {"type": "integer", "minimum": low, "maximum": high}
    if default is not None:
        schema["default"] = default
    return schema


KEY_PATTERN = (
    r"^(Key[A-Z]|Digit[0-9]|F([1-9]|1[0-9]|2[0-4])|"
    r"Enter|Escape|Backspace|Tab|Space|Minus|Equal|BracketLeft|BracketRight|"
    r"Backslash|Semicolon|Quote|Backquote|Comma|Period|Slash|CapsLock|"
    r"PrintScreen|ScrollLock|Pause|Insert|Home|PageUp|Delete|End|PageDown|"
    r"ArrowRight|ArrowLeft|ArrowDown|ArrowUp|NumLock|Numpad[0-9]|"
    r"NumpadDivide|NumpadMultiply|NumpadSubtract|NumpadAdd|NumpadEnter|NumpadDecimal|"
    r"ControlLeft|ControlRight|ShiftLeft|ShiftRight|AltLeft|AltRight|MetaLeft|MetaRight)$"
)
ACTION_ARGUMENT_SCHEMAS = {
    "press_keys": object_schema({
        "keys": {"type": "array", "minItems": 1, "maxItems": 6,
                 "items": {"type": "string", "pattern": KEY_PATTERN}},
        "holdMs": integer_schema(0, 500),
    }, ("keys",)),
    "type_text": object_schema({
        "text": {"type": "string", "minLength": 1, "maxLength": 256,
                 "pattern": r"^[\t\n\r\x20-\x7e]*$",
                 "description": "ASCII HID text only. Non-ASCII text is rejected before any batch input."},
        "delayMs": integer_schema(0, 50),
    }, ("text",)),
    "move_mouse": object_schema({
        "mode": {"type": "string", "enum": ["relative"], "default": "relative"},
        "deltaX": integer_schema(-127, 127),
        "deltaY": integer_schema(-127, 127),
    }, ("deltaX", "deltaY")),
    "click_mouse": object_schema({
        "mode": {"type": "string", "enum": ["relative"], "default": "relative"},
        "button": {"type": "string", "enum": ["left", "right", "middle", "back", "forward"]},
        "clicks": integer_schema(1, 3, 1),
    }, ("button",)),
    "scroll_mouse": object_schema({"deltaY": integer_schema(-127, 127)}, ("deltaY",)),
}
BATCH_SCHEMA = object_schema({
    "actions": {"type": "array", "minItems": 1, "maxItems": 12, "items": {"oneOf": [
        object_schema({
            "tool": {"type": "string", "enum": [name]},
            "arguments": schema,
            "delay_ms": integer_schema(0, 1000, 80),
        }, ("tool", "arguments")) for name, schema in ACTION_ARGUMENT_SCHEMAS.items()
    ]}},
    "capture": {"type": "boolean", "default": True},
    "quality": integer_schema(1, 100, 60),
    "settle_ms": integer_schema(0, 1000, 200),
}, ("actions",))


def batch_tool():
    return {
        "name": BATCH_TOOL_NAME,
        "description": (
            "Execute 1-12 already-decided NanoKVM input actions sequentially, then capture "
            "one screenshot (default). Supports keyboard shortcuts, ASCII text, relative "
            "mouse movement/clicks, and scrolling. All parameters are validated before any "
            "input; stops on the first error, never retries, and has a 30-second execution budget. "
            "Only batch user-authorized actions; pause for a new screenshot whenever the "
            "next decision depends on the screen. Success confirms dispatch, so verify the "
            "final image before claiming a UI outcome. Input values and typed text are not echoed."
        ),
        "inputSchema": copy.deepcopy(BATCH_SCHEMA),
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": False, "openWorldHint": True},
    }


def validate(value, schema, path="arguments"):
    """Validate the schema subset above; never include input values in errors."""
    if "oneOf" in schema:
        matches = 0
        for variant in schema["oneOf"]:
            try:
                validate(value, variant, path)
                matches += 1
            except ValueError:
                pass
        if matches != 1:
            raise ValueError(path + ": invalid action or arguments")
        return
    expected = schema["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    if type(value) is not types[expected]:
        raise ValueError(path + ": expected " + expected)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(path + ": unsupported option")
    if expected == "object":
        properties = schema["properties"]
        if set(value) - set(properties):
            raise ValueError(path + ": unknown fields")
        if set(schema.get("required", [])) - set(value):
            raise ValueError(path + ": missing required fields")
        for key, item in value.items():
            validate(item, properties[key], path + "." + key)
    elif expected == "array":
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", sys.maxsize):
            raise ValueError(path + ": invalid item count")
        for index, item in enumerate(value):
            validate(item, schema["items"], path + "[" + str(index) + "]")
    elif expected == "string":
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", sys.maxsize):
            raise ValueError(path + ": invalid text length")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise ValueError(path + ": unsupported characters or key code")
    elif expected == "integer":
        if not schema.get("minimum", -sys.maxsize) <= value <= schema.get("maximum", sys.maxsize):
            raise ValueError(path + ": out of range")


def emit(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class Bridge:
    def __init__(self, base=None, config=None):
        self.config = config or load_settings(base)
        self.base = self.config.config_dir
        self.phone = None
        self.context = self.config.ssl_context()
        self.api_key = self.config.api_key()
        self.session_id = None
        self.protocol_version = None

    def close(self):
        phone, self.phone = getattr(self, "phone", None), None
        if phone is not None:
            phone.close()

    def get_phone(self):
        if getattr(self, "phone", None) is None:
            self.phone = PhoneController(self, self.base, self.config.host, self.config.tls_server_name,
                                         self.config.cert_sha256, port=self.config.port,
                                         cookie_path=self.config.cookie_file, origin=self.config.origin)
        return self.phone

    @staticmethod
    def crop_result(result):
        """Crop only verified JPEG data; preserve upstream errors and unknown formats."""
        for item in result.get("content", []):
            if item.get("type") != "image" or item.get("mimeType") != "image/jpeg":
                continue
            try:
                data, info = crop_phone(base64.b64decode(item["data"], validate=True))
                if not info.get("cropped") and info.get("detection") != "already_portrait":
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            item["data"] = base64.b64encode(data).decode("ascii")
            result["structuredContent"] = {**result.get("structuredContent", {}), **info,
                                           "size": len(data), "coordinate_space": "cropped_phone_image"}
            for text_item in result.get("content", []):
                if text_item.get("type") != "text":
                    continue
                try:
                    metadata = json.loads(text_item["text"])
                except (ValueError, TypeError, KeyError):
                    continue
                if isinstance(metadata, dict) and "width" in metadata and "height" in metadata:
                    text_item["text"] = json.dumps(result["structuredContent"], ensure_ascii=False)
        return result

    def run_phone(self, arguments):
        started = time.monotonic()
        try:
            validate(arguments, PHONE_SCHEMA)
            action = arguments["action"]
            if action == "tap" and ("x" not in arguments or "y" not in arguments):
                raise ValueError("Tap requires both x and y")
            if action == "scroll" and "direction" not in arguments:
                raise ValueError("Scroll requires direction up or down")
        except ValueError as error:
            return self.batch_result({"ok": False, "outcome": "validation_failed",
                                      "error": str(error), "ui_verified": False}, [], started)
        summary, images = self.get_phone().run(arguments, started + MAX_BATCH_SECONDS)
        return self.batch_result(summary, images, started)

    def exchange(self, message, timeout=45):
        """One upstream request. Never retry an input that might already have taken effect."""
        deadline = time.monotonic() + timeout
        raw_socket = socket.create_connection((self.config.host, self.config.port), timeout=timeout)
        try:
            raw_socket.settimeout(max(0.001, deadline - time.monotonic()))
            tls_socket = self.context.wrap_socket(raw_socket, server_hostname=self.config.tls_server_name)
        except Exception:
            raw_socket.close()
            raise
        connection = http.client.HTTPConnection(self.config.host, self.config.port, timeout=timeout)
        connection.sock = tls_socket
        try:
            fingerprint = hashlib.sha256(tls_socket.getpeercert(binary_form=True)).hexdigest()
            if self.config.cert_sha256 and fingerprint != self.config.cert_sha256:
                raise ssl.SSLError("NanoKVM certificate fingerprint changed; refusing connection")
            headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                       "Authorization": "Bearer " + self.api_key}
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
            if self.protocol_version:
                headers["MCP-Protocol-Version"] = self.protocol_version
            body = json.dumps(message, ensure_ascii=False).encode("utf-8")
            tls_socket.settimeout(max(0.001, deadline - time.monotonic()))
            connection.request("POST", self.config.api_path, body=body, headers=headers)
            tls_socket.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise RuntimeError("NanoKVM HTTP status " + str(response.status))
            if response.getheader("Mcp-Session-Id"):
                self.session_id = response.getheader("Mcp-Session-Id")
            if "id" not in message:
                return None
            if response.status == 204:
                raise RuntimeError("NanoKVM returned no response for an MCP request")
            if "text/event-stream" in response.getheader("Content-Type", ""):
                event_lines = []
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("NanoKVM response deadline exceeded")
                    tls_socket.settimeout(remaining)
                    line = response.readline()
                    if not line:
                        if event_lines:
                            payload = json.loads("\n".join(event_lines))
                            if self.matches(payload, message):
                                return payload
                            emit(payload)
                        break
                    text = line.decode("utf-8").rstrip("\r\n")
                    if text.startswith("data:"):
                        event_lines.append(text[5:].lstrip(" "))
                    elif text == "" and event_lines:
                        payload = json.loads("\n".join(event_lines))
                        event_lines = []
                        if self.matches(payload, message):
                            return payload
                        emit(payload)
                raise RuntimeError("NanoKVM event stream ended before the requested response")
            payload = json.loads(response.read())
            if not self.matches(payload, message):
                raise RuntimeError("NanoKVM returned an unexpected MCP response")
            return payload
        finally:
            connection.close()

    @staticmethod
    def matches(payload, request):
        return isinstance(payload, dict) and payload.get("id") == request["id"]

    def handle(self, message):
        method = message.get("method")
        params = message.get("params") or {}
        if method == "tools/call" and params.get("name") == READ_TOOL_NAME:
            if "id" not in message:
                return None
            arguments = params.get("arguments", {})
            try:
                validate(arguments, READ_SCHEMA)
                result = run_read_chat(self, arguments)
            except ValueError:
                result = {"isError": True, "structuredContent": {"ok": False, "outcome": "validation_failed"},
                          "content": [{"type": "text", "text": "Invalid read_chat arguments; no input was sent"}]}
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if method == "tools/call" and params.get("name") == PHONE_TOOL_NAME:
            if "id" not in message:
                return None
            return {"jsonrpc": "2.0", "id": message["id"],
                    "result": self.run_phone(params.get("arguments", {}))}
        if method == "tools/call" and params.get("name") == "scroll_mouse" and self.config.phone_mode:
            if "id" not in message:
                return None
            arguments = params.get("arguments", {})
            started = time.monotonic()
            try:
                validate(arguments, ACTION_ARGUMENT_SCHEMAS["scroll_mouse"])
                delta = arguments["deltaY"]
                mapped = {"action": "scroll", "direction": "up" if delta > 0 else "down",
                          "distance": min(600, max(150, abs(delta) * 12)), "pages": 1}
                result = self.run_phone(mapped if delta else {"action": "observe"})
            except ValueError as error:
                result = self.batch_result({"ok": False, "outcome": "validation_failed",
                                            "error": str(error), "ui_verified": False}, [], started)
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}
        if method == "tools/call" and params.get("name") == BATCH_TOOL_NAME:
            if "id" not in message:
                return None  # Never execute tool calls delivered as notifications.
            result = self.run_batch(params.get("arguments", {}))
            return {"jsonrpc": "2.0", "id": message["id"], "result": result}
        response = self.exchange(message)
        if response is None or "result" not in response:
            return response
        result = response["result"]
        if method == "initialize":
            self.protocol_version = result.get("protocolVersion")
            existing = result.get("instructions", "")
            result["instructions"] = BATCH_INSTRUCTIONS + ("\n\n" + existing if existing else "")
        elif method == "tools/list" and not params.get("cursor"):
            tools = result.setdefault("tools", [])
            if not any(tool.get("name") == BATCH_TOOL_NAME for tool in tools):
                tools.append(batch_tool())
            if not any(tool.get("name") == PHONE_TOOL_NAME for tool in tools):
                tools.append(phone_tool())
            if not any(tool.get("name") == READ_TOOL_NAME for tool in tools):
                tools.append(read_tool())
            for tool in tools:
                name = tool.get("name")
                if self.config.phone_mode and name in ("screenshot", "scroll_mouse"):
                    if isinstance(tool.get("outputSchema"), dict):
                        tool["outputSchema"]["additionalProperties"] = True
                    if name == "scroll_mouse":
                        tool["description"] = (
                            "Scroll the connected iPhone using one complete bounded relative drag "
                            "and return a fresh phone screenshot. Positive deltaY reveals older/up "
                            "content, negative reveals newer/down. Prefer phone_control for explicit "
                            "partial-page distance or multiple overlapping screenshots. Dispatch "
                            "success is not visual verification; inspect frame_stable and the image."
                        )
                    else:
                        tool["description"] = (
                            tool.get("description", "") + " Portrait-phone black margins are cropped "
                            "locally without downsampling unless an explicit device crop is requested. "
                            "Returned width/height describe the returned image; source dimensions "
                            "and crop_box preserve mapping to the original frame."
                        )
        elif method == "tools/call" and params.get("name") == "screenshot":
            if self.config.phone_mode and not any(key in (params.get("arguments") or {}) for key in ("x", "y", "w", "h")):
                self.crop_result(result)
        return response

    def call_tool(self, name, arguments, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Batch deadline exceeded")
        response = self.exchange({"jsonrpc": "2.0", "id": "batch-" + uuid.uuid4().hex,
                                  "method": "tools/call", "params": {"name": name, "arguments": arguments}},
                                 timeout=remaining)
        if response is None or "result" not in response:
            raise RuntimeError("NanoKVM did not return a tool result")
        return response["result"]

    @staticmethod
    def tool_succeeded(result):
        if result.get("isError"):
            return False
        info = result.get("structuredContent", {})
        return not (info.get("ok") is False or info.get("skippedCount", 0) or info.get("retCode", 0) != 0)

    def run_batch(self, arguments):
        started = time.monotonic()
        summary = {"ok": False, "outcome": "validation_failed", "completed": 0, "total": 0,
                   "ui_verified": False, "steps": []}
        images = []
        try:
            validate(arguments, BATCH_SCHEMA)
            if sum(len(step["arguments"].get("text", "")) for step in arguments["actions"]) > 512:
                raise ValueError("arguments.actions: total text length exceeds 512 characters")
        except ValueError as error:
            summary["error"] = str(error)
            return self.batch_result(summary, images, started)
        actions = arguments["actions"]
        summary["total"] = len(actions)
        summary["outcome"] = "completed"
        deadline = started + MAX_BATCH_SECONDS
        for index, action in enumerate(actions, 1):
            step_started = time.monotonic()
            step = {"index": index, "tool": action["tool"], "ok": False}
            tool_args = copy.deepcopy(action["arguments"])
            if action["tool"] in ("move_mouse", "click_mouse"):
                tool_args["mode"] = "relative"
            try:
                if action["tool"] == "scroll_mouse" and self.config.phone_mode:
                    delta = tool_args["deltaY"]
                    phone_arguments = ({"action": "scroll", "direction": "up" if delta > 0 else "down",
                                  "distance": min(600, max(150, abs(delta) * 12)), "pages": 1}
                                 if delta else {"action": "observe"})
                    info, unused_images = self.get_phone().run(phone_arguments, deadline)
                    if info.get("buttons_may_be_held"):
                        summary["buttons_may_be_held"] = True
                        info["ok"] = False
                    result = {"structuredContent": info, "isError": not info["ok"]}
                else:
                    result = self.call_tool(action["tool"], tool_args, deadline)
                step["ok"] = self.tool_succeeded(result)
                if step["ok"]:
                    summary["completed"] += 1
                else:
                    summary["outcome"] = "action_failed"
                    summary["error"] = "An action failed or typed only part of its text; subsequent actions were not sent"
            except Exception:
                summary["outcome"] = "action_uncertain"
                summary["error"] = "An action did not return a valid result; it may have taken effect. Do not retry without inspecting the screen"
            step["elapsed_ms"] = round((time.monotonic() - step_started) * 1000)
            summary["steps"].append(step)
            if not step["ok"]:
                summary["stopped_at"] = index
                break
            delay = action.get("delay_ms", 80) / 1000
            if delay:
                time.sleep(min(delay, max(0, deadline - time.monotonic())))
        if arguments.get("capture", True):
            try:
                settle = arguments.get("settle_ms", 200) / 1000
                if settle:
                    time.sleep(min(settle, max(0, deadline - time.monotonic())))
                screenshot = self.call_tool("screenshot", {"quality": arguments.get("quality", 60),
                                                         "timeoutMs": 10000}, deadline)
                screenshot = self.crop_result(screenshot) if self.config.phone_mode else screenshot
                images = [item for item in screenshot.get("content", []) if item.get("type") == "image"]
                summary["screenshot"] = screenshot.get("structuredContent", {})
                if not self.tool_succeeded(screenshot) or not images:
                    raise RuntimeError("Screenshot failed")
            except Exception:
                images = []
                summary["screenshot_error"] = "Final screenshot could not be captured; do not infer that inputs were undone"
                if summary["outcome"] == "completed":
                    summary["outcome"] = "screenshot_failed"
        summary["ok"] = summary["outcome"] == "completed"
        return self.batch_result(summary, images, started)

    @staticmethod
    def batch_result(summary, images, started):
        summary["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return {"isError": not summary["ok"], "structuredContent": summary,
                "content": [{"type": "text", "text": json.dumps(summary, ensure_ascii=False)}, *images]}


if __name__ == "__main__":
    from .cli import main
    raise SystemExit(main())
