import base64
import io
import json
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from nanokvmgo_mcp import local_reader
from nanokvmgo_mcp.local_reader import LocalReader, OCRUnavailableError, OCRWorker, Transcript, group_name, header_matches, readable


TARGET = '【示例】技术交流群'


def line(text, y, x=.20, height=.022):
    return {'text': text, 'confidence': 1, 'box': [x, y, x + .5, y + height]}


def image_frame(index=0, stable=True):
    return {'raw': b'unused', 'image': {'data': base64.b64encode(b'private-fixture').decode()},
            'metadata': {'width': 678, 'height': 1440, 'origin': [941, 0],
                         'frame_stable': stable, 'fresh_capture': True, 'captureId': str(index)}}


class HeaderTests(unittest.TestCase):
    def test_exact_header_accepts_count_icons_and_width_variants(self):
        self.assertTrue(header_matches([line('【示例】技 术交流群（203） 込', .08)], TARGET))
        self.assertEqual(group_name('[示例]技术交流群(203)'), group_name(TARGET))

    def test_similar_group_or_body_mention_does_not_authorize_input(self):
        self.assertFalse(header_matches([line(TARGET + '二群（203）', .08)], TARGET))
        self.assertFalse(header_matches([line(TARGET, .3)], TARGET))


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.transcript = Transcript()
        self.meta = image_frame()['metadata']

    def test_same_text_at_different_positions_is_preserved(self):
        self.transcript.add([line('示例成员', .2), line('示例成员', .5)], self.meta, 0, 0)
        self.assertEqual(len(self.transcript.lines), 2)

    def test_overlap_is_deduplicated_by_document_coordinates(self):
        self.transcript.add([line('示例产品本周开始公开测试', .2)], self.meta, 0, 0)
        self.transcript.add([line('示例产品本周开始公开测试', .45)], self.meta, -360, 1)
        self.assertEqual(len(self.transcript.lines), 1)
        self.assertEqual(self.transcript.duplicates, 1)

    def test_matching_coordinates_do_not_merge_unrelated_text(self):
        self.transcript.add([line('甲方的新产品已发布', .2)], self.meta, 0, 0)
        self.transcript.add([line('乙方讨论的是另一件事', .2)], self.meta, 0, 1)
        self.assertEqual(len(self.transcript.lines), 2)

    def test_animation_text_is_excluded_in_source_coordinates(self):
        meta = {**self.meta, 'ignored_regions': [[1070, 280, 1450, 340]]}
        self.transcript.add([line('GIF中的字', .2), line('聊天正文', .6)], meta, 0, 0)
        self.assertEqual([item['text'] for item in self.transcript.lines], ['聊天正文'])
        self.assertEqual(self.transcript.dynamic_lines_excluded, 1)

    def test_header_and_composer_are_not_transcribed(self):
        self.transcript.add([line(TARGET, .08), line('正文', .4), line('输入框', .94)], self.meta, 0, 0)
        self.assertEqual([item['text'] for item in self.transcript.lines], ['正文'])


class ReaderTests(unittest.TestCase):
    def setup_reader(self, folder, groups, statuses=None, stable=True):
        phone = Mock()
        phone.buttons_may_be_held = False
        phone._diagnostic_delta.return_value = {'capture_count': 0}
        phone._diagnostic_snapshot.return_value = {}
        phone.capture_stable.return_value = image_frame(stable=stable)
        phone.scroll_one.side_effect = [(image_frame(i + 1), {'matched': True, 'content_shift_y': 330})
                                        for i in range(max(0, len(groups) - 1))]
        phone.scroll_status.side_effect = statuses or [('completed', None)] * max(0, len(groups) - 1)
        ocr = Mock()
        ocr.recognize.side_effect = [
            {'ok': True, 'lines': [line(group, .08), line('第%d幅独立正文' % i, .3)], 'elapsed_ms': 100}
            for i, group in enumerate(groups)]
        reader = LocalReader(phone, ocr, pathlib.Path(folder) / 'reading', TARGET, pages=len(groups))
        return reader, phone

    def test_wrong_initial_group_sends_zero_gestures_and_zero_transcript(self):
        with tempfile.TemporaryDirectory() as folder:
            reader, phone = self.setup_reader(folder, ['其他群'])
            report = reader.run()
        self.assertEqual(report['outcome'], 'wrong_group')
        self.assertEqual(report['unique_lines'], 0)
        phone.scroll_one.assert_not_called()
        phone.close.assert_called_once()

    def test_group_change_stops_before_another_gesture(self):
        with tempfile.TemporaryDirectory() as folder:
            reader, phone = self.setup_reader(folder, [TARGET, '其他群', TARGET])
            report = reader.run()
        self.assertEqual(report['outcome'], 'wrong_group')
        self.assertEqual(report['gestures'], 1)
        self.assertEqual(phone.scroll_one.call_count, 1)

    def test_unverified_scroll_is_saved_but_not_added_or_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            reader, phone = self.setup_reader(folder, [TARGET, TARGET, TARGET],
                                               statuses=[('scroll_unverified', 'Uncertain overlap')])
            report = reader.run()
        self.assertEqual(report['outcome'], 'scroll_unverified')
        self.assertEqual(report['gestures'], 1)
        self.assertEqual(report['pages'][1]['new_lines'], 0)
        self.assertEqual(report['unique_lines'], 1)

    def test_initial_unstable_frame_sends_zero_gestures(self):
        with tempfile.TemporaryDirectory() as folder:
            reader, phone = self.setup_reader(folder, [TARGET], stable=False)
            report = reader.run()
        self.assertEqual(report['outcome'], 'frame_unstable')
        phone.scroll_one.assert_not_called()

    def test_completed_task_is_bounded_and_records_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            reader, phone = self.setup_reader(folder, [TARGET, TARGET, TARGET])
            report = reader.run()
            transcript = (reader.output / 'transcript.txt').read_text()
            modes = [(path.stat().st_mode & 0o777) for path in reader.output.iterdir()]
        self.assertTrue(report['ok'])
        self.assertEqual(report['gestures'], 2)
        self.assertEqual(report['unique_lines'], 3)
        self.assertIn('第0幅独立正文', transcript)
        self.assertTrue(all(mode == 0o600 for mode in modes))
        self.assertTrue(all('page' in item for item in report['source_lines']))

    def test_text_stability_requires_all_fresh_validated_evidence(self):
        valid = {'fresh_capture': True, 'text_stable': True,
                 'localized_motion': True, 'mask_validated': True}
        self.assertTrue(readable(valid))
        for key in valid:
            self.assertFalse(readable({**valid, key: False}))


class OCRWorkerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cache = pathlib.Path(self.folder.name) / 'cache'
        self.capability = {'ok': True, 'languages': ['zh-Hans', 'en-US']}
        self.commands = []
        self.process = Mock(stdin=io.BytesIO(), stdout=Mock())
        self.selector = Mock()
        self.selector.select.return_value = [(None, None)]
        original_is_file = pathlib.Path.is_file
        patches = [patch.object(local_reader.sys, 'platform', 'darwin'),
                   patch.object(local_reader.platform, 'machine', return_value='test-architecture'),
                   patch.object(local_reader.pathlib.Path, 'is_file', autospec=True,
                                side_effect=lambda path: path == pathlib.Path('/usr/bin/swiftc') or original_is_file(path)),
                   patch.object(local_reader.subprocess, 'run', side_effect=self.run_process),
                   patch.object(local_reader.subprocess, 'Popen', return_value=self.process),
                   patch.object(local_reader.selectors, 'DefaultSelector', return_value=self.selector)]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def run_process(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[0] == '/usr/bin/swiftc':
            pathlib.Path(command[-1]).write_bytes(b'synthetic executable fixture')
            return subprocess.CompletedProcess(command, 0, stdout=b'')
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(self.capability).encode())

    def worker(self):
        worker = OCRWorker(self.cache)
        self.addCleanup(worker.close)
        return worker

    def test_compile_and_module_cache_stay_inside_configured_private_cache(self):
        worker = self.worker()
        compile_command = self.commands[0][0]
        self.assertEqual(compile_command[0], '/usr/bin/swiftc')
        module_path = pathlib.Path(compile_command[compile_command.index('-module-cache-path') + 1])
        self.assertEqual(module_path.parent, self.cache)
        self.assertEqual(pathlib.Path(compile_command[-1]).parent, self.cache)
        self.assertEqual(pathlib.Path(compile_command[-3]), local_reader.BASE / 'vision_ocr.swift')
        binary = pathlib.Path(self.commands[1][0][0])
        self.assertEqual(binary.parent, self.cache)
        self.assertEqual(binary.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
        self.assertEqual(module_path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.commands[1][0][1], '--check')
        self.assertFalse(list(self.cache.glob('.vision-ocr-*')))
        worker.close()

    def test_matching_source_cache_reuses_binary_but_still_checks_capability(self):
        self.worker().close()
        self.commands.clear()
        self.worker()
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][0][1], '--check')

    def test_missing_language_fails_before_starting_worker(self):
        self.capability = {'ok': True, 'languages': ['en-US']}
        with self.assertRaisesRegex(OCRUnavailableError, 'Chinese and English'):
            OCRWorker(self.cache)
        local_reader.subprocess.Popen.assert_not_called()

    def test_compile_failure_does_not_expose_stderr_or_leave_temporary_binary(self):
        with patch.object(local_reader.subprocess, 'run', side_effect=subprocess.CalledProcessError(
                1, 'swiftc', stderr=b'private machine path')):
            with self.assertRaises(OCRUnavailableError) as raised:
                OCRWorker(self.cache)
        self.assertNotIn('private machine path', str(raised.exception))
        self.assertFalse(list(self.cache.glob('.vision-ocr-*')))
        local_reader.subprocess.Popen.assert_not_called()

    def test_recognize_handles_partial_pipe_json_and_validates_request_id(self):
        worker = self.worker()
        result = {'id': '1', 'ok': True, 'elapsed_ms': 10,
                  'lines': [line('合成OCR文字', .3)]}
        encoded = json.dumps(result).encode() + b'\n'
        with patch.object(local_reader.os, 'read', side_effect=[encoded[:10], encoded[10:]]):
            self.assertEqual(worker.recognize(pathlib.Path('/synthetic/image.jpg'), time.monotonic() + 10), result)
        request = json.loads(self.process.stdin.getvalue())
        self.assertEqual(request, {'id': '1', 'path': '/synthetic/image.jpg'})
        self.assertEqual(self.selector.select.call_count, 2)

    def test_recognize_rejects_old_response_and_eof_without_retry(self):
        for response in (b'{"id":"old","ok":true}\n', b''):
            with self.subTest(response=response):
                worker = self.worker()
                with patch.object(local_reader.os, 'read', return_value=response):
                    with self.assertRaises(RuntimeError):
                        worker.recognize('/synthetic/image.jpg', time.monotonic() + 10)
                self.assertEqual(worker.ident, 1)

    def test_expired_ocr_budget_never_writes_a_request(self):
        worker = self.worker()
        with self.assertRaises(TimeoutError):
            worker.recognize('/synthetic/image.jpg', time.monotonic() - 1)
        self.assertEqual(self.process.stdin.getvalue(), b'')

    def test_invalid_ocr_coordinates_or_nonfinite_values_are_rejected(self):
        for box in ([0, .2, 2, .3], [0, .2, 1, float('nan')], [0, .2, True, .3], [0, .4, 1, .3]):
            with self.subTest(box=box), self.assertRaises(RuntimeError):
                OCRWorker._validate_result({'lines': [{'text': '示例', 'box': box, 'confidence': 1}], 'elapsed_ms': 2})


if __name__ == '__main__':
    unittest.main()
