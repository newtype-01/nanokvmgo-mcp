import copy
import base64
import hashlib
import io
import json
import ssl
import unittest
from unittest.mock import Mock, patch

from nanokvmgo_mcp import bridge_runtime as bridge
from nanokvmgo_mcp.config import load_settings
from PIL import Image, ImageDraw


IMAGE = {"type": "image", "mimeType": "image/jpeg", "data": "aW1hZ2U="}


def action(tool="move_mouse", arguments=None):
    if arguments is None:
        arguments = {"deltaX": 0, "deltaY": 0}
    return {"tool": tool, "arguments": arguments, "delay_ms": 0}


def batch(actions=None, **kwargs):
    return {"actions": actions or [action()], "settle_ms": 0, **kwargs}


def make_bridge():
    instance = object.__new__(bridge.Bridge)
    instance.protocol_version = None
    instance.session_id = None
    instance.api_key = "TEST_SECRET_DO_NOT_ECHO"
    instance.context = Mock()
    instance.config = load_settings('/offline-example', {'url': 'https://device.example/api/mcp',
                                                        'cert_sha256': '0' * 64, 'phone_mode': True})
    instance.base = instance.config.config_dir
    return instance


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.bridge = make_bridge()
        self.calls = []

        def call(name, arguments, deadline):
            self.calls.append((name, copy.deepcopy(arguments)))
            if name == "screenshot":
                return {"content": [IMAGE], "structuredContent": {"ok": True, "retCode": 0}}
            return {"structuredContent": {"ok": True}}

        self.bridge.call_tool = Mock(side_effect=call)

    def test_validated_sequence_preserves_order_defaults_and_final_image(self):
        steps = [action(), action("click_mouse", {"button": "left"}),
                 action("press_keys", {"keys": ["MetaLeft", "KeyH"]})]
        result = self.bridge.run_batch(batch(steps))
        self.assertFalse(result["isError"])
        self.assertFalse(result["structuredContent"]["ui_verified"])
        self.assertEqual(result["structuredContent"]["completed"], 3)
        self.assertEqual([name for name, _ in self.calls], ["move_mouse", "click_mouse", "press_keys", "screenshot"])
        self.assertEqual(self.calls[0][1]["mode"], "relative")
        self.assertEqual(self.calls[1][1]["mode"], "relative")
        self.assertEqual(result["content"][-1], IMAGE)
        self.assertNotIn("mode", steps[0]["arguments"])

    def test_invalid_later_step_prevents_every_input(self):
        invalid_steps = [
            action("type_text", {"text": "中文"}),
            action("move_mouse", {"mode": "absolute", "deltaX": 0, "deltaY": 0}),
            action("move_mouse", {"deltaX": True, "deltaY": 0}),
            action("move_mouse", {"deltaX": 128, "deltaY": 0}),
            action("press_keys", {"keys": ["NOT_A_KEY"]}),
            action("reboot", {}),
            action("click_mouse", {"button": "left", "clicks": 0}),
        ]
        for invalid in invalid_steps:
            with self.subTest(invalid=invalid):
                result = self.bridge.run_batch(batch([action(), invalid]))
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["outcome"], "validation_failed")
        self.assertEqual(self.calls, [])

    def test_no_unbounded_or_unknown_batch_options(self):
        for arguments in [{"actions": []}, batch([action()] * 13), batch(quality=0),
                          batch(unknown=True), batch(capture="false")]:
            with self.subTest(arguments=arguments):
                self.assertTrue(self.bridge.run_batch(arguments)["isError"])
        self.assertEqual(self.calls, [])

    def test_remote_failure_stops_later_input_but_captures_state(self):
        original = self.bridge.call_tool.side_effect

        def call(name, args, deadline):
            if name == "press_keys":
                self.calls.append((name, args))
                return {"isError": True}
            return original(name, args, deadline)

        self.bridge.call_tool.side_effect = call
        result = self.bridge.run_batch(batch([action(), action("press_keys", {"keys": ["Enter"]}), action()]))
        self.assertEqual(result["structuredContent"]["outcome"], "action_failed")
        self.assertEqual(result["structuredContent"]["completed"], 1)
        self.assertEqual(result["structuredContent"]["stopped_at"], 2)
        self.assertEqual([name for name, _ in self.calls], ["move_mouse", "press_keys", "screenshot"])

    def test_uncertain_input_is_never_retried(self):
        original = self.bridge.call_tool.side_effect

        def call(name, args, deadline):
            if name == "move_mouse":
                self.calls.append((name, args))
                raise TimeoutError("transport timeout")
            return original(name, args, deadline)

        self.bridge.call_tool.side_effect = call
        result = self.bridge.run_batch(batch([action(), action()]))
        self.assertEqual(result["structuredContent"]["outcome"], "action_uncertain")
        self.assertEqual([name for name, _ in self.calls], ["move_mouse", "screenshot"])

    def test_partial_typing_stops_and_does_not_echo_text(self):
        self.bridge.call_tool.return_value = {"structuredContent": {"typed": 1, "skippedCount": 1}}
        self.bridge.call_tool.side_effect = None
        result = self.bridge.run_batch(batch([action("type_text", {"text": "private-test-text"}), action()], capture=False))
        self.assertEqual(result["structuredContent"]["outcome"], "action_failed")
        self.assertEqual(self.bridge.call_tool.call_count, 1)
        self.assertNotIn("private-test-text", json.dumps(result))

    def test_successful_typing_does_not_echo_text(self):
        result = self.bridge.run_batch(batch([action("type_text", {"text": "private-test-text"})], capture=False))
        self.assertFalse(result["isError"])
        self.assertNotIn("private-test-text", json.dumps(result))
        self.assertNotIn(self.bridge.api_key, json.dumps(result))

    def test_failed_capture_does_not_repeat_successful_actions(self):
        original = self.bridge.call_tool.side_effect

        def call(name, args, deadline):
            if name == "screenshot":
                self.calls.append((name, args))
                return {"isError": True, "structuredContent": {"ok": False}}
            return original(name, args, deadline)

        self.bridge.call_tool.side_effect = call
        result = self.bridge.run_batch(batch())
        self.assertEqual(result["structuredContent"]["outcome"], "screenshot_failed")
        self.assertEqual(result["structuredContent"]["completed"], 1)
        self.assertEqual([name for name, _ in self.calls], ["move_mouse", "screenshot"])

    def test_expired_batch_sends_no_request(self):
        self.bridge = make_bridge()
        self.bridge.exchange = Mock()
        with self.assertRaises(TimeoutError):
            self.bridge.call_tool("click_mouse", {"button": "left"}, 0)
        self.bridge.exchange.assert_not_called()


class ProtocolTests(unittest.TestCase):
    def test_read_chat_is_dispatched_natively_without_upstream_tool_call(self):
        instance = make_bridge()
        instance.exchange = Mock()
        response = {'structuredContent': {'ok': True, 'transcript': 'Synthetic text'}, 'content': []}
        with patch.object(bridge, 'run_read_chat', return_value=response) as reader:
            result = instance.handle({'jsonrpc': '2.0', 'id': 88, 'method': 'tools/call',
                                      'params': {'name': 'read_chat', 'arguments': {'group': '示例群', 'pages': 1}}})
        self.assertIs(result['result'], response)
        reader.assert_called_once_with(instance, {'group': '示例群', 'pages': 1})
        instance.exchange.assert_not_called()

    def test_read_chat_notification_or_invalid_arguments_never_executes_reader(self):
        instance = make_bridge()
        with patch.object(bridge, 'run_read_chat') as reader:
            self.assertIsNone(instance.handle({'method': 'tools/call', 'params': {
                'name': 'read_chat', 'arguments': {'group': '示例群'}}}))
            for arguments in [{'group': '示例群', 'pages': 99}, {'group': '示例群', 'output': '/unapproved'},
                              {'group': '示例群', 'pages': True}]:
                result = instance.handle({'id': 1, 'method': 'tools/call', 'params': {
                    'name': 'read_chat', 'arguments': arguments}})
                self.assertTrue(result['result']['isError'])
        reader.assert_not_called()

    def test_default_mode_preserves_upstream_wheel_and_screenshot_behavior(self):
        instance = make_bridge()
        instance.config = load_settings('/offline-example', {'url': 'https://device.example'})
        response = {'result': {'content': [IMAGE]}}
        instance.exchange = Mock(return_value=response)
        for name in ['scroll_mouse', 'screenshot']:
            request = {'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': {}}}
            self.assertIs(instance.handle(request), response)
        self.assertEqual(instance.exchange.call_count, 2)

    def test_default_batch_wheel_is_forwarded_as_wheel(self):
        instance = make_bridge()
        instance.config = load_settings('/offline-example', {'url': 'https://device.example'})
        instance.call_tool = Mock(return_value={'structuredContent': {'ok': True}})
        result = instance.run_batch(batch([action('scroll_mouse', {'deltaY': 3})], capture=False))
        self.assertTrue(result['structuredContent']['ok'])
        self.assertEqual(instance.call_tool.call_args.args[:2], ('scroll_mouse', {'deltaY': 3}))

    def test_legacy_calls_keep_original_request_and_response(self):
        instance = make_bridge()
        response = {"jsonrpc": "2.0", "id": 42, "result": {"content": [IMAGE]}}
        instance.exchange = Mock(return_value=response)
        request = {"jsonrpc": "2.0", "id": 42, "method": "tools/call", "params": {"name": "screenshot"}}
        self.assertIs(instance.handle(request), response)
        instance.exchange.assert_called_once_with(request)

    def test_tool_pagination_keeps_upstream_tools_and_cursor(self):
        instance = make_bridge()
        response = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "screenshot"}], "nextCursor": "next"}}
        instance.exchange = Mock(side_effect=lambda _: copy.deepcopy(response))
        first = instance.handle({"id": 1, "method": "tools/list"})
        later = instance.handle({"id": 2, "method": "tools/list", "params": {"cursor": "next"}})
        self.assertEqual([t["name"] for t in first["result"]["tools"]], ["screenshot", "batch_actions", "phone_control", "read_chat"])
        self.assertEqual(first["result"]["nextCursor"], "next")
        self.assertEqual([t["name"] for t in later["result"]["tools"]], ["screenshot"])

    def test_original_server_instructions_are_preserved(self):
        instance = make_bridge()
        instance.exchange = Mock(return_value={"result": {"protocolVersion": "2024-11-05", "instructions": "Original guidance"}})
        result = instance.handle({"id": 1, "method": "initialize"})
        self.assertIn("Original guidance", result["result"]["instructions"])
        self.assertIn("batch_actions", result["result"]["instructions"])
        self.assertEqual(instance.protocol_version, "2024-11-05")

    def test_batch_notification_does_not_execute_input(self):
        instance = make_bridge()
        instance.run_batch = Mock()
        result = instance.handle({"method": "tools/call", "params": {"name": "batch_actions", "arguments": batch()}})
        self.assertIsNone(result)
        instance.run_batch.assert_not_called()

    def test_phone_notification_does_not_execute_input(self):
        instance = make_bridge()
        instance.run_phone = Mock()
        self.assertIsNone(instance.handle({"method": "tools/call", "params": {
            "name": "phone_control", "arguments": {"action": "tap", "x": 500, "y": 500}}}))
        instance.run_phone.assert_not_called()

    def test_phone_parameters_are_validated_before_constructing_controller(self):
        instance = make_bridge()
        instance.get_phone = Mock()
        for arguments in ({"action": "tap", "x": 500}, {"action": "scroll"},
                          {"action": "scroll", "direction": "up", "pages": 4},
                          {"action": "tap", "x": True, "y": 500},
                          {"action": "scroll", "direction": "down", "password": "DO_NOT_ECHO"}):
            with self.subTest(arguments=arguments):
                result = instance.run_phone(arguments)
                self.assertTrue(result["isError"])
                self.assertEqual(result["structuredContent"]["outcome"], "validation_failed")
                self.assertNotIn("DO_NOT_ECHO", json.dumps(result))
        instance.get_phone.assert_not_called()

    def test_legacy_wheel_uses_one_bounded_gesture_without_remote_wheel(self):
        instance = make_bridge()
        instance.exchange = Mock()
        instance.run_phone = Mock(return_value={"structuredContent": {"ok": True}, "content": [IMAGE]})
        result = instance.handle({"id": 77, "method": "tools/call", "params": {
            "name": "scroll_mouse", "arguments": {"deltaY": 20}}})
        self.assertEqual(result["id"], 77)
        self.assertEqual(result["result"]["content"], [IMAGE])
        instance.run_phone.assert_called_once_with({"action": "scroll", "direction": "up", "distance": 240, "pages": 1})
        instance.exchange.assert_not_called()

    def test_legacy_wheel_notification_never_executes_gesture(self):
        instance = make_bridge()
        instance.run_phone = Mock()
        self.assertIsNone(instance.handle({"method": "tools/call", "params": {
            "name": "scroll_mouse", "arguments": {"deltaY": 20}}}))
        instance.run_phone.assert_not_called()

    def test_close_clears_controller_but_keeps_authentication_and_session(self):
        instance = make_bridge()
        instance.phone = Mock()
        old_phone = instance.phone
        instance.session_id = "session"
        instance.close()
        instance.close()
        old_phone.close.assert_called_once()
        self.assertIsNone(instance.phone)
        self.assertEqual(instance.session_id, "session")
        self.assertEqual(instance.api_key, "TEST_SECRET_DO_NOT_ECHO")

    def test_adapted_tools_extend_output_schema_without_weakening_input_validation(self):
        instance = make_bridge()
        original = {"type": "object", "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"], "additionalProperties": False}
        instance.exchange = Mock(return_value={"result": {"tools": [
            {"name": name, "outputSchema": copy.deepcopy(original),
             "inputSchema": {"type": "object", "additionalProperties": False}}
            for name in ("screenshot", "scroll_mouse")]}})
        result = instance.handle({"id": 1, "method": "tools/list"})
        for definition in result["result"]["tools"][:2]:
            self.assertTrue(definition["outputSchema"]["additionalProperties"])
            self.assertEqual(definition["outputSchema"]["required"], ["ok"])
            self.assertFalse(definition["inputSchema"]["additionalProperties"])

    def test_local_crop_keeps_text_and_structured_dimensions_consistent(self):
        frame = Image.new("RGB", (800, 400), "black")
        ImageDraw.Draw(frame).rectangle((300, 0, 499, 399), fill=(230, 230, 230))
        buffer = io.BytesIO()
        frame.save(buffer, "JPEG", quality=90)
        metadata = {"ok": True, "width": 800, "height": 400, "size": len(buffer.getvalue())}
        result = {"content": [{"type": "text", "text": json.dumps(metadata)},
                              {"type": "image", "mimeType": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode()}],
                  "structuredContent": metadata}
        bridge.Bridge.crop_result(result)
        self.assertLess(result["structuredContent"]["width"], 250)
        self.assertEqual(result["structuredContent"], json.loads(result["content"][0]["text"]))
        self.assertEqual(result["structuredContent"]["source_width"], 800)
        self.assertEqual(result["structuredContent"]["height"], 400)

    def test_batch_propagates_uncertain_buttons_and_stops_later_input(self):
        instance = make_bridge()
        controller = Mock()
        controller.run.return_value = ({"ok": False, "buttons_may_be_held": True}, [])
        instance.get_phone = Mock(return_value=controller)
        instance.call_tool = Mock()
        result = instance.run_batch(batch(actions=[action("scroll_mouse", {"deltaY": 10}), action()], capture=False))
        self.assertTrue(result["isError"])
        self.assertTrue(result["structuredContent"]["buttons_may_be_held"])
        self.assertEqual(result["structuredContent"]["stopped_at"], 1)
        instance.call_tool.assert_not_called()

    def test_changed_certificate_is_rejected_before_credentials_are_sent(self):
        instance = make_bridge()
        tls = Mock()
        tls.getpeercert.return_value = b"unexpected certificate"
        self.assertNotEqual(hashlib.sha256(tls.getpeercert()).hexdigest(), instance.config.cert_sha256)
        instance.context.wrap_socket.return_value = tls
        with patch.object(bridge.socket, "create_connection", return_value=Mock()), \
             patch.object(bridge.http.client, "HTTPConnection") as connection:
            with self.assertRaises(ssl.SSLError):
                instance.exchange({"id": 1, "method": "tools/list"})
            connection.return_value.request.assert_not_called()
            connection.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
