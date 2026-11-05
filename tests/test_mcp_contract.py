"""Exercise stdio -> tool registration -> read_chat -> reader with fake hardware."""

import base64
import io
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nanokvmgo_mcp import bridge_runtime, launcher, read_chat
from nanokvmgo_mcp.config import load_settings


class MCPContractTests(unittest.TestCase):
    def test_real_stdio_dispatch_returns_reading_text_without_upstream_read_chat(self):
        with tempfile.TemporaryDirectory() as folder:
            root = pathlib.Path(folder)
            bridge = object.__new__(bridge_runtime.Bridge)
            bridge.config = load_settings(root, {'url': 'https://device.example'})
            bridge.protocol_version, bridge.session_id = None, None
            upstream_calls = []
            def upstream(message):
                upstream_calls.append(message['method'])
                if message['method'] == 'initialize':
                    result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}}
                elif message['method'] == 'tools/list':
                    result = {'tools': [{'name': 'screenshot', 'inputSchema': {'type': 'object'}}]}
                else:
                    raise AssertionError('read_chat must run locally')
                return {'jsonrpc': '2.0', 'id': message['id'], 'result': result}
            bridge.exchange = upstream
            phone = Mock()
            phone.buttons_may_be_held = False
            phone._diagnostic_snapshot.return_value = {}
            phone._diagnostic_delta.return_value = {'capture_count': 4}
            def frame(index):
                return {'raw': b'synthetic', 'image': {'type': 'image', 'mimeType': 'image/jpeg',
                        'data': base64.b64encode(b'synthetic fixture').decode()},
                        'metadata': {'width': 678, 'height': 1440, 'origin': [0, 0],
                                     'captureId': str(index), 'fresh_capture': True, 'frame_stable': True}}
            phone.capture_stable.return_value = frame(0)
            phone.scroll_one.return_value = (frame(1), {'matched': True, 'content_shift_y': 330,
                                                       'overlap_fraction': .7})
            phone.scroll_status.return_value = ('completed', None)
            bridge.get_phone = Mock(return_value=phone)
            ocr = Mock()
            ocr.recognize.side_effect = [{'elapsed_ms': 1, 'lines': [
                {'text': 'Example', 'confidence': 1, 'box': [.2, .08, .7, .102]},
                {'text': 'Synthetic line ' + str(index), 'confidence': 1, 'box': [.2, .3, .7, .322]}]}
                for index in range(2)]
            requests = [
                {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
                {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
                    'name': 'read_chat', 'arguments': {'group': 'Example', 'pages': 2,
                                                      'include_final_image': False}}},
            ]
            loader = SimpleNamespace(bridge=bridge, refresh=Mock(), close=Mock())
            output, errors = io.StringIO(), io.StringIO()
            with patch.object(launcher, 'HotReloadBridge', return_value=loader), \
                 patch.object(read_chat, 'OCRWorker', return_value=ocr):
                status = launcher.serve(stdin=io.StringIO('\n'.join(map(json.dumps, requests))),
                                        stdout=output, stderr=errors)
            responses = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(status, 0)
            self.assertEqual(errors.getvalue(), '')
            names = [tool['name'] for tool in responses[1]['result']['tools']]
            self.assertEqual(names, ['screenshot', 'batch_actions', 'phone_control', 'read_chat'])
            result = responses[2]['result']
            self.assertFalse(result['isError'])
            self.assertEqual(result['structuredContent']['gestures'], 1)
            self.assertIn('Synthetic line 0', result['structuredContent']['transcript'])
            self.assertIn('Synthetic line 1', result['structuredContent']['transcript'])
            self.assertEqual(upstream_calls, ['initialize', 'tools/list'])
            phone.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
