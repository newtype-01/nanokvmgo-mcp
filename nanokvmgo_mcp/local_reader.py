"""Bounded local WeChat reading: pinned bridge, local OCR, positional dedup.

No messages are typed or sent. The exact group header is checked before every
gesture. OCR text is evidence with image provenance, not a reconstructed log.
"""

import argparse
import base64
import difflib
import hashlib
import json
import math
import os
import pathlib
import platform
import re
import selectors
import subprocess
import sys
import tempfile
import time
import unicodedata


BASE = pathlib.Path(__file__).resolve().parent


def private_write(path, data):
    path = pathlib.Path(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data if isinstance(data, bytes) else data.encode('utf-8'))


def normalized(text):
    return ''.join(unicodedata.normalize('NFKC', text).split())


def group_name(text):
    value = normalized(text).replace('【', '[').replace('】', ']')
    # A count terminates the title; OCR sometimes also sees its trailing icon.
    return re.split(r'\(\d{1,6}\)', value, maxsplit=1)[0]


def header_matches(lines, expected):
    parts = [line['text'] for line in lines
             if .055 <= (line['box'][1] + line['box'][3]) / 2 <= .12
             and .10 <= line['box'][0] < .90]
    return group_name(''.join(parts)) == group_name(expected)


def readable(metadata):
    if not metadata.get('fresh_capture'):
        return False
    return bool(metadata.get('frame_stable') or (
        metadata.get('mask_validated') and metadata.get('text_stable')
        and metadata.get('localized_motion')))


def intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


class OCRUnavailableError(RuntimeError):
    def __init__(self, message, outcome='ocr_unavailable'):
        super().__init__(message)
        self.outcome = outcome


class OCRWorker:
    """Apple Vision helper, compiled exclusively into the configured user cache."""

    def __init__(self, cache_dir, deadline=None):
        self.process, self.selector, self.buffer = None, None, b''
        self.ident = 0
        if sys.platform != 'darwin':
            raise OCRUnavailableError('read_chat requires macOS Apple Vision OCR; other MCP tools remain available',
                                      'unsupported_platform')
        deadline = deadline if deadline is not None else time.monotonic() + 60
        source = BASE / 'vision_ocr.swift'
        cache_dir = pathlib.Path(cache_dir)
        cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_dir.chmod(0o700)
        digest = hashlib.sha256(source.read_bytes() + platform.machine().encode()).hexdigest()[:20]
        binary = cache_dir / ('vision-ocr-' + digest)
        if not binary.is_file():
            compiler = pathlib.Path('/usr/bin/swiftc')
            if not compiler.is_file():
                raise OCRUnavailableError('Apple Swift compiler is unavailable; install Xcode Command Line Tools')
            modules = cache_dir / 'swift-module-cache'
            modules.mkdir(mode=0o700, exist_ok=True)
            modules.chmod(0o700)
            fd, temporary = tempfile.mkstemp(prefix='.vision-ocr-', dir=cache_dir)
            os.close(fd)
            try:
                subprocess.run([str(compiler), '-O', '-module-cache-path', str(modules),
                                str(source), '-o', temporary], check=True, capture_output=True,
                               timeout=self._remaining(deadline, 60))
                os.chmod(temporary, 0o700)
                os.replace(temporary, binary)
            except subprocess.CalledProcessError:
                raise OCRUnavailableError('Apple Vision OCR helper could not be compiled') from None
            finally:
                pathlib.Path(temporary).unlink(missing_ok=True)
        try:
            checked = subprocess.run([str(binary), '--check'], check=True, capture_output=True,
                                     timeout=self._remaining(deadline, 5))
            capability = json.loads(checked.stdout)
            if capability.get('ok') is not True or not {'zh-Hans', 'en-US'}.issubset(capability.get('languages', [])):
                raise OCRUnavailableError('Apple Vision Chinese and English text recognition is unavailable')
            self._remaining(deadline, 1)
            self.process = subprocess.Popen([str(binary)], stdin=subprocess.PIPE,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.selector = selectors.DefaultSelector()
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
        except (OSError, ValueError, subprocess.CalledProcessError):
            self.close()
            raise OCRUnavailableError('Apple Vision OCR capability check failed') from None
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _remaining(deadline, limit):
        remaining = min(limit, deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError('Local OCR deadline exceeded')
        return remaining

    def recognize(self, path, deadline):
        self._remaining(deadline, 8)
        self.ident += 1
        request_id = str(self.ident)
        self.process.stdin.write((json.dumps({'id': request_id, 'path': str(path)}) + '\n').encode())
        self.process.stdin.flush()
        response_deadline = min(deadline, time.monotonic() + 8)
        while b'\n' not in self.buffer:
            if not self.selector.select(self._remaining(response_deadline, 8)):
                raise TimeoutError('Local OCR deadline exceeded')
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise RuntimeError('Local OCR helper ended unexpectedly')
            self.buffer += chunk
            if len(self.buffer) > 2 * 1024 * 1024:
                raise RuntimeError('Local OCR output exceeded its limit')
        response, self.buffer = self.buffer.split(b'\n', 1)
        result = json.loads(response)
        if not isinstance(result, dict) or result.get('id') != request_id or result.get('ok') is not True:
            raise RuntimeError('Local OCR failed')
        self._validate_result(result)
        return result

    @staticmethod
    def _validate_result(result):
        lines = result.get('lines')
        elapsed = result.get('elapsed_ms')
        if (not isinstance(lines, list) or len(lines) > 2000 or type(elapsed) not in (int, float)
                or not math.isfinite(elapsed) or elapsed < 0):
            raise RuntimeError('Invalid local OCR response')
        for line in lines:
            box = line.get('box') if isinstance(line, dict) else None
            confidence = line.get('confidence') if isinstance(line, dict) else None
            if (not isinstance(line, dict) or not isinstance(line.get('text'), str)
                    or len(line['text']) > 8192 or not isinstance(box, list) or len(box) != 4
                    or any(type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1 for value in box)
                    or box[0] >= box[2] or box[1] >= box[3]
                    or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                raise RuntimeError('Invalid local OCR response')

    def close(self):
        process, self.process = self.process, None
        try:
            if process is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                finally:
                    process.stdout.close()
        finally:
            if self.selector is not None:
                self.selector.close()
                self.selector = None


class Transcript:
    def __init__(self):
        self.lines = []
        self.duplicates = 0
        self.dynamic_lines_excluded = 0

    def add(self, lines, metadata, document_offset, page):
        width, height = metadata['width'], metadata['height']
        ox, oy = metadata.get('origin', [0, 0])
        added = 0
        for source in lines:
            box = source['box']
            center_y = (box[1] + box[3]) / 2
            if not .12 <= center_y <= .895 or box[3] - box[1] < .008:
                continue
            source_box = [ox + box[0] * width, oy + box[1] * height,
                          ox + box[2] * width, oy + box[3] * height]
            if any(intersects(source_box, region) for region in metadata.get('ignored_regions', [])):
                self.dynamic_lines_excluded += 1
                continue
            text = source['text'].strip()
            if not text:
                continue
            item = {**source, 'text': text, 'page': page,
                    'document_y': center_y * height + document_offset,
                    'x': box[0] * width, 'pixel_height': (box[3] - box[1]) * height}
            candidates = []
            for previous in self.lines:
                tolerance = max(8, min(24, max(item['pixel_height'], previous['pixel_height']) * .65))
                if abs(previous['document_y'] - item['document_y']) > tolerance or abs(previous['x'] - item['x']) > 30:
                    continue
                first, second = normalized(previous['text']), normalized(text)
                similarity = difflib.SequenceMatcher(None, first, second, autojunk=False).ratio()
                if first == second or (min(len(first), len(second)) >= 8 and similarity >= .90):
                    candidates.append((similarity, previous))
            if candidates:
                self.duplicates += 1
                # Keep an actual observed line; never splice OCR strings together.
                previous = max(candidates, key=lambda pair: pair[0])[1]
                old_margin = min(previous['box'][1] - .12, .895 - previous['box'][3])
                new_margin = min(box[1] - .12, .895 - box[3])
                if new_margin > old_margin + .01:
                    self.lines[self.lines.index(previous)] = item
                continue
            self.lines.append(item)
            added += 1
        self.lines.sort(key=lambda item: (item['document_y'], item['x']))
        return added


class LocalReader:
    def __init__(self, phone, ocr, output, target, direction='up', pages=4, seconds=60, close_phone=True):
        if (direction not in ('up', 'down') or type(pages) is not int or not 1 <= pages <= 6
                or type(seconds) is not int or not 10 <= seconds <= 60):
            raise ValueError('Invalid bounded reading parameters')
        if not isinstance(target, str) or not group_name(target) or len(target) > 256:
            raise ValueError('An exact group name is required')
        self.phone, self.ocr = phone, ocr
        self.output, self.target = pathlib.Path(output), target
        self.direction, self.pages, self.seconds = direction, pages, seconds
        self.transcript = Transcript()
        self.close_phone = close_phone
        self.last_frame = None

    def observe(self, frame, index, offset, deadline, comparison=None, validated=True):
        self.last_frame = frame
        path = self.output / ('page-%02d.jpg' % index)
        private_write(path, base64.b64decode(frame['image']['data'], validate=True))
        result = self.ocr.recognize(path, deadline)
        matches = header_matches(result['lines'], self.target)
        valid = validated and readable(frame['metadata']) and matches
        added = self.transcript.add(result['lines'], frame['metadata'], offset, index) if valid else 0
        page = {'index': index, 'image': str(path.resolve()), 'group_verified': matches,
                'readable': readable(frame['metadata']), 'new_lines': added,
                'ocr_elapsed_ms': round(result['elapsed_ms'], 1), 'screenshot': frame['metadata'],
                'comparison': comparison}
        private_write(self.output / ('page-%02d-ocr.json' % index), json.dumps(result, ensure_ascii=False, indent=2))
        return page

    def run(self, deadline=None):
        self.output.mkdir(mode=0o700, parents=True, exist_ok=False)
        started = time.monotonic()
        deadline = min(deadline, started + self.seconds) if deadline is not None else started + self.seconds
        diagnostics_before = self.phone._diagnostic_snapshot()
        report = {'ok': False, 'target': self.target, 'direction': self.direction,
                  'requested_pages': self.pages, 'pages': [], 'gestures': 0, 'gesture_attempts': 0,
                  'outcome': 'started', 'ocr_engine': 'Apple Vision on this Mac'}
        offset = 0
        stage = 'initial_capture'
        try:
            frame = self.phone.capture_stable(deadline, allow_localized=True)
            for index in range(self.pages):
                comparison = None
                if index:
                    if deadline - time.monotonic() < 5:
                        report['outcome'] = 'deadline_before_next_gesture'
                        break
                    stage = 'scroll'
                    self.last_frame = None
                    report['gesture_attempts'] += 1
                    frame, comparison = self.phone.scroll_one(
                        frame, self.direction, 300, 800, deadline, 85,
                        prepare_cursor=index + 1 < self.pages)
                    report['gestures'] += 1
                    outcome, error = self.phone.scroll_status(frame, comparison, self.direction)
                    report['outcome'] = outcome
                    if error:
                        report['error'] = error
                    if comparison.get('matched'):
                        offset -= comparison['content_shift_y']
                stage = 'ocr_verification'
                page = self.observe(frame, index, offset, deadline, comparison,
                                    validated=not index or report['outcome'] == 'completed')
                report['pages'].append(page)
                if not page['group_verified']:
                    report['outcome'] = 'wrong_group'
                    break
                if not page['readable']:
                    report['outcome'] = 'frame_unstable'
                    break
                if index and report['outcome'] != 'completed':
                    break
                if index and page['new_lines'] == 0:
                    report['outcome'] = 'no_new_text'
                    break
                report['outcome'] = 'completed'
            report['ok'] = report['outcome'] in ('completed', 'no_new_text')
        except Exception as error:
            report['outcome'] = 'deadline_exceeded' if isinstance(error, TimeoutError) else 'reader_failed'
            report['error'] = 'Local reading failed during %s; no gesture was retried' % stage
        except BaseException:
            report.update(outcome='cancelled', error='Reading interrupted; no gesture was retried')
            raise
        finally:
            if self.close_phone:
                self.phone.close()
            report['buttons_may_be_held'] = self.phone.buttons_may_be_held
            report['elapsed_ms'] = round((time.monotonic() - started) * 1000, 1)
            report['unique_lines'] = len(self.transcript.lines)
            report['duplicates_removed'] = self.transcript.duplicates
            report['dynamic_lines_excluded'] = self.transcript.dynamic_lines_excluded
            report['source_lines'] = self.transcript.lines
            report['diagnostics'] = self.phone._diagnostic_delta(diagnostics_before)
            private_write(self.output / 'report.json', json.dumps(report, ensure_ascii=False, indent=2))
            text = '\n'.join(item['text'] for item in self.transcript.lines)
            private_write(self.output / 'transcript.txt', text + '\n')
            report['transcript'] = text
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly permit bounded device reading')
    parser.add_argument('--group', required=True)
    parser.add_argument('--pages', type=int, default=4)
    parser.add_argument('--direction', choices=['up', 'down'], default='up')
    parser.add_argument('--seconds', type=int, default=60)
    parser.add_argument('--output', type=pathlib.Path, required=True)
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({'ready': True, 'device_actions': 0, 'requires_live_flag': True}))
        return
    from .bridge_runtime import Bridge
    bridge = Bridge()
    ocr = OCRWorker(bridge.config.cache_dir)
    try:
        bridge.handle({'jsonrpc': '2.0', 'id': 'local-reader-init', 'method': 'initialize',
                       'params': {'protocolVersion': '2024-11-05', 'capabilities': {},
                                  'clientInfo': {'name': 'bounded-local-reader', 'version': '1'}}})
        bridge.handle({'jsonrpc': '2.0', 'method': 'notifications/initialized', 'params': {}})
        report = LocalReader(bridge.get_phone(), ocr, args.output, args.group,
                             args.direction, args.pages, args.seconds).run()
        print(json.dumps({key: report.get(key) for key in [
            'ok', 'outcome', 'elapsed_ms', 'gestures', 'unique_lines',
            'duplicates_removed', 'dynamic_lines_excluded', 'buttons_may_be_held']}, ensure_ascii=False))
    finally:
        bridge.close()
        ocr.close()


if __name__ == '__main__':
    main()
