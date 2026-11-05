import base64
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nanokvmgo_mcp import local_reader, read_chat


TARGET = '[Example]技术群'


def frame(index=0):
    return {'raw': b'synthetic image',
            'image': {'type': 'image', 'mimeType': 'image/jpeg',
                      'data': base64.b64encode(('synthetic-image-%d' % index).encode()).decode()},
            'metadata': {'width': 678, 'height': 1440, 'origin': [941, 0],
                         'frame_stable': True, 'fresh_capture': True, 'captureId': str(index)}}


def recognized(index=0, group=TARGET):
    return {'ok': True, 'elapsed_ms': 12,
            'lines': [{'text': group, 'box': [.2, .08, .7, .102], 'confidence': 1},
                      {'text': '合成示例正文%d' % index, 'box': [.2, .3, .7, .322], 'confidence': 1}]}


def setup_bridge(folder, pages=3):
    phone = Mock()
    phone.buttons_may_be_held = False
    phone._diagnostic_snapshot.return_value = {}
    phone._diagnostic_delta.return_value = {'capture_count': pages * 2}
    phone.capture_stable.return_value = frame()
    phone.scroll_one.side_effect = [(frame(index), {'matched': True, 'content_shift_y': 330,
                                                   'overlap_fraction': .75})
                                    for index in range(1, pages)]
    phone.scroll_status.return_value = ('completed', None)
    ocr = Mock()
    ocr.recognize.side_effect = [recognized(index) for index in range(pages)]
    bridge = SimpleNamespace(config=SimpleNamespace(output_dir=pathlib.Path(folder) / 'output',
                                                     cache_dir=pathlib.Path(folder) / 'cache'),
                             get_phone=Mock(return_value=phone), close=Mock())
    return bridge, phone, ocr


class ArgumentTests(unittest.TestCase):
    def test_schema_defaults_and_definition_are_independent(self):
        result = read_chat.validate_arguments({'group': TARGET})
        self.assertEqual(result, {'group': TARGET, 'direction': 'up', 'pages': 4,
                                  'timeout_seconds': 60, 'include_final_image': True})
        definition = read_chat.tool_definition()
        self.assertEqual(definition['name'], 'read_chat')
        self.assertFalse(definition['annotations']['idempotentHint'])
        definition['inputSchema']['required'].append('output')
        self.assertEqual(read_chat.TOOL_SCHEMA['required'], ['group'])
        self.assertFalse(read_chat.TOOL_SCHEMA['additionalProperties'])

    def test_invalid_arguments_never_start_ocr_touch_output_or_get_phone(self):
        invalid = [None, [], {}, {'group': ''}, {'group': '  '}, {'group': 3},
                   {'group': 'x' * 201}, {'group': 'title\nbody'},
                   {'group': TARGET, 'output': '/tmp/arbitrary'},
                   {'group': TARGET, 'include_final_image': 1},
                   {'group': TARGET, 'include_final_image': 'true'},
                   {'group': TARGET, 'direction': 'left'}, {'group': TARGET, 'direction': []}]
        invalid.extend({'group': TARGET, 'pages': value} for value in (True, 0, 7, 1.0, '2'))
        invalid.extend({'group': TARGET, 'timeout_seconds': value} for value in (False, 9, 61, 30.0, '60'))
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, _ = setup_bridge(folder)
            with patch.object(read_chat, 'OCRWorker') as worker:
                for arguments in invalid:
                    with self.subTest(arguments=arguments):
                        result = read_chat.run_read_chat(bridge, arguments)
                        self.assertTrue(result['isError'])
                        self.assertEqual(result['structuredContent']['outcome'], 'invalid_arguments')
            worker.assert_not_called()
            bridge.get_phone.assert_not_called()
            self.assertFalse(bridge.config.output_dir.exists())
            phone.close.assert_not_called()


class EntryPointTests(unittest.TestCase):
    def test_native_reading_uses_shared_phone_returns_provenance_and_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder)
            order = []
            def initialize(*args, **kwargs):
                order.append('ocr_preflight')
                return ocr
            bridge.get_phone.side_effect = lambda: order.append('phone') or phone
            with patch.object(read_chat, 'OCRWorker', side_effect=initialize) as worker:
                result = read_chat.run_read_chat(bridge, {'group': TARGET, 'pages': 3})
            data = result['structuredContent']
            self.assertFalse(result['isError'])
            self.assertEqual(order, ['ocr_preflight', 'phone'])
            self.assertEqual(worker.call_args.args, (bridge.config.cache_dir,))
            self.assertIn('deadline', worker.call_args.kwargs)
            bridge.get_phone.assert_called_once()
            self.assertEqual(phone.scroll_one.call_count, 2)
            self.assertEqual(phone.scroll_status.call_count, 2)
            self.assertEqual(phone.scroll_one.call_args_list[0].kwargs, {'prepare_cursor': True})
            self.assertEqual(phone.scroll_one.call_args_list[1].kwargs, {'prepare_cursor': False})
            phone.close.assert_called_once()
            ocr.close.assert_called_once()
            bridge.close.assert_not_called()
            self.assertEqual(len(data['source_lines']), 3)
            self.assertEqual(data['gestures'], 2)
            self.assertIn('not a complete chat history', data['limitation'])
            self.assertIn('合成示例正文0', data['transcript'])
            output = pathlib.Path(data['files']['directory'])
            self.assertEqual(output.parent, bridge.config.output_dir.resolve())
            self.assertTrue(output.name.startswith('read-chat-'))
            self.assertEqual(output.stat().st_mode & 0o777, 0o700)
            self.assertEqual(bridge.config.output_dir.stat().st_mode & 0o777, 0o700)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in output.iterdir()))
            self.assertEqual(json.loads(pathlib.Path(data['files']['report']).read_text()), data)
            self.assertEqual(len(data['files']['images']), 3)
            self.assertEqual(len(data['files']['ocr']), 3)
            self.assertEqual(result['content'][-1], frame(2)['image'])
            self.assertEqual(json.loads(result['content'][0]['text']), data)

    def test_one_page_is_observation_only_and_image_can_be_omitted(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder, pages=1)
            with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                result = read_chat.run_read_chat(bridge, {'group': TARGET, 'pages': 1,
                                                         'include_final_image': False})
            self.assertFalse(result['isError'])
            self.assertEqual(len(result['content']), 1)
            phone.scroll_one.assert_not_called()
            phone.capture_stable.assert_called_once()
            self.assertTrue(phone.capture_stable.call_args.kwargs['allow_localized'])

    def test_new_read_resets_old_diagnostics_without_releasing_existing_connection_first(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder, pages=1)
            connection = object()
            phone.input = connection
            counters = {'capture_count': 400}
            def reset():
                counters['capture_count'] = 0
            def capture(*args, **kwargs):
                self.assertIs(phone.input, connection)
                phone.close.assert_not_called()
                self.assertEqual(counters['capture_count'], 0)
                counters['capture_count'] += 2
                return frame()
            phone._reset_diagnostics.side_effect = reset
            phone._diagnostic_snapshot.side_effect = lambda: dict(counters)
            phone._diagnostic_delta.side_effect = lambda before: {
                'capture_count': counters['capture_count'] - before['capture_count']}
            phone.capture_stable.side_effect = capture
            with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                result = read_chat.run_read_chat(bridge, {'group': TARGET, 'pages': 1})
            self.assertFalse(result['isError'])
            self.assertEqual(result['structuredContent']['diagnostics']['capture_count'], 2)
            phone._reset_diagnostics.assert_called_once()
            phone.close.assert_called_once()

    def test_unsupported_platform_is_explicit_before_creating_output_or_accessing_device(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, _ = setup_bridge(folder)
            with patch.object(local_reader.sys, 'platform', 'linux'):
                result = read_chat.run_read_chat(bridge, {'group': TARGET})
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['outcome'], 'unsupported_platform')
            self.assertEqual(result['structuredContent']['transcript'], '')
            bridge.get_phone.assert_not_called()
            phone.close.assert_not_called()
            self.assertFalse(bridge.config.output_dir.exists())

    def test_failed_ocr_capability_prevents_all_device_access(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, _ = setup_bridge(folder)
            with patch.object(read_chat, 'OCRWorker', side_effect=local_reader.OCRUnavailableError('OCR unavailable')):
                result = read_chat.run_read_chat(bridge, {'group': TARGET})
            self.assertEqual(result['structuredContent']['outcome'], 'ocr_unavailable')
            bridge.get_phone.assert_not_called()
            phone.close.assert_not_called()

    def test_total_deadline_includes_ocr_cold_start_and_never_starts_late_capture(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder)
            now = [100.0]
            def initialize(*args, **kwargs):
                now[0] = 161.0
                return ocr
            with patch.object(read_chat.time, 'monotonic', side_effect=lambda: now[0]), \
                 patch.object(read_chat, 'OCRWorker', side_effect=initialize):
                result = read_chat.run_read_chat(bridge, {'group': TARGET})
            self.assertEqual(result['structuredContent']['outcome'], 'deadline_exceeded')
            bridge.get_phone.assert_not_called()
            ocr.close.assert_called_once()

    def test_wrong_group_is_not_transcribed_and_sends_no_scroll(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder)
            ocr.recognize.side_effect = [recognized(group='另一个合成群')]
            with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                result = read_chat.run_read_chat(bridge, {'group': TARGET})
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['outcome'], 'wrong_group')
            self.assertEqual(result['structuredContent']['transcript'], '')
            phone.scroll_one.assert_not_called()

    def test_capture_failure_after_scroll_never_returns_old_frame_as_final_image(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder)
            phone.scroll_one.side_effect = RuntimeError('sensitive transport detail')
            with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                result = read_chat.run_read_chat(bridge, {'group': TARGET, 'pages': 3})
            self.assertEqual(len(result['content']), 1)
            self.assertEqual(result['structuredContent']['outcome'], 'reader_failed')
            self.assertEqual(len(result['structuredContent']['source_lines']), 1)
            self.assertEqual(result['structuredContent']['gesture_attempts'], 1)
            self.assertNotIn('final_screenshot', result['structuredContent'])
            self.assertNotIn('sensitive transport detail', json.dumps(result))
            phone.scroll_one.assert_called_once()
            phone.close.assert_called_once()

    def test_interrupt_and_sigterm_release_phone_and_ocr_without_closing_bridge(self):
        for interrupt in (KeyboardInterrupt, SystemExit):
            with self.subTest(interrupt=interrupt), tempfile.TemporaryDirectory() as folder:
                bridge, phone, ocr = setup_bridge(folder)
                ocr.recognize.side_effect = interrupt()
                with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                    with self.assertRaises(interrupt):
                        read_chat.run_read_chat(bridge, {'group': TARGET})
                phone.close.assert_called_once()
                ocr.close.assert_called_once()
                bridge.close.assert_not_called()
                report_path = next(bridge.config.output_dir.glob('*/report.json'))
                self.assertEqual(json.loads(report_path.read_text())['outcome'], 'cancelled')

    def test_unconfirmed_release_marks_result_uncertain(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder, pages=1)
            def close():
                phone.buttons_may_be_held = True
            phone.close.side_effect = close
            with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                result = read_chat.run_read_chat(bridge, {'group': TARGET, 'pages': 1})
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['outcome'], 'release_uncertain')
            self.assertTrue(result['structuredContent']['buttons_may_be_held'])
            ocr.close.assert_called_once()

    def test_cleanup_failure_still_closes_ocr_and_returns_explicit_error(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge, phone, ocr = setup_bridge(folder, pages=1)
            phone.close.side_effect = OSError('private connection detail')
            with patch.object(read_chat, 'OCRWorker', return_value=ocr):
                result = read_chat.run_read_chat(bridge, {'group': TARGET, 'pages': 1})
            self.assertTrue(result['isError'])
            self.assertEqual(result['structuredContent']['outcome'], 'cleanup_failed')
            self.assertNotIn('private connection detail', json.dumps(result))
            ocr.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
