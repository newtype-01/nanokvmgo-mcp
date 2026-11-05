"""Hot reload tests with fake local modules: no credentials or networking."""

import io
import json
import os
import pathlib
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from nanokvmgo_mcp import launcher


def runtime_source(version, *, constructor_fails=False, slots=False):
    slot_definition = "    __slots__ = ('incompatible_layout',)\n" if slots else ""
    init_body = "        raise RuntimeError('PRIVATE CONSTRUCTOR DETAIL')\n" if constructor_fails else """        self.base = config.BASE
        self.config = config.CONFIG
        self.session_id = 'initial-session'
        self.protocol_version = 'test-protocol'
        self.api_key = object()
        self.context = object()
        self.close_count = 0
        self.phone = object()
"""
    return f"""from . import config, hid_transport, read_chat

VERSION = {version!r}
CLOSE_COUNT = 0
class Bridge:
{slot_definition}    def __init__(self):
{init_body}    def handle(self, message):
        if message.get('method') == 'change_session':
            self.session_id = message['params']['session']
        if message.get('method') == 'fail':
            raise RuntimeError('PRIVATE REQUEST DETAIL')
        if 'id' not in message:
            return None
        return {{'jsonrpc': '2.0', 'id': message['id'], 'result': {{'version': VERSION,
                'dependency': hid_transport.VALUE, 'session': self.session_id}}}}
    def close(self):
        global CLOSE_COUNT
        CLOSE_COUNT += 1
        self.close_count += 1
        self.phone = None
"""


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = pathlib.Path(self.directory.name)
        # A distinct package namespace prevents fake implementations from ever
        # replacing the installed nanokvmgo_mcp modules or their attributes.
        self.package_name = "nanokvmgo_mcp_launcher_fixture"
        self.module_files = tuple((self.package_name + "." + name.rpartition(".")[2], filename)
                                  for name, filename in launcher.MODULE_FILES)
        self.modules_before = {name: sys.modules.get(name, launcher.MISSING)
                               for name, _ in self.module_files}
        self.package_before = sys.modules.get(self.package_name, launcher.MISSING)
        package = types.ModuleType(self.package_name)
        package.__path__ = [str(self.base)]
        sys.modules[self.package_name] = package
        self.addCleanup(self.restore_modules)
        module_patch = patch.object(launcher, "MODULE_FILES", self.module_files)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.stderr = io.StringIO()
        (self.base / "config.py").write_text(
            "VALUE = 'config-one'\nBASE = 'user-config-directory'\nCONFIG = object()\n")
        (self.base / "hid_transport.py").write_text("from . import config\nVALUE = 'one'\n")
        (self.base / "phone_vision.py").write_text("from . import config\n")
        (self.base / "phone_control.py").write_text("from . import hid_transport, phone_vision\n")
        (self.base / "local_reader.py").write_text("from . import phone_control\n")
        (self.base / "read_chat.py").write_text("from . import local_reader\n")
        self.write_runtime("A")

    def restore_modules(self):
        for name, previous in self.modules_before.items():
            if previous is launcher.MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
        if self.package_before is launcher.MISSING:
            sys.modules.pop(self.package_name, None)
        else:
            sys.modules[self.package_name] = self.package_before

    def module(self, name):
        return sys.modules[self.package_name + "." + name]

    def write_runtime(self, version, **options):
        (self.base / "bridge_runtime.py").write_text(runtime_source(version, **options))

    def request(self, identifier=1, method="test"):
        return {"jsonrpc": "2.0", "id": identifier, "method": method}

    def test_initial_load_uses_launcher_directory_without_touching_credentials(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        self.assertTrue(loader.refresh())
        self.assertEqual(loader.bridge.base, "user-config-directory")
        self.assertEqual(loader.bridge.handle(self.request())["result"]["version"], "A")
        self.assertEqual(self.stderr.getvalue(), "")

    def test_all_snapshot_modules_use_package_names_and_relative_dependencies(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        package = sys.modules[self.package_name]
        for name, _ in self.module_files:
            module = sys.modules[name]
            self.assertEqual(module.__name__, name)
            self.assertEqual(module.__package__, self.package_name)
            self.assertIs(getattr(package, name.rpartition(".")[2]), module)
        self.assertIs(loader.runtime.read_chat.local_reader.phone_control,
                      self.module("phone_control"))
        self.assertIs(self.module("hid_transport").config, self.module("config"))

    def test_global_module_names_are_never_replaced(self):
        sentinels = {name.rpartition(".")[2]: types.ModuleType("unrelated")
                     for name, _ in self.module_files}
        with patch.dict(sys.modules, sentinels):
            loader = launcher.HotReloadBridge(self.base, self.stderr)
            loader.refresh()
            self.write_runtime("B")
            loader.refresh()
            for name, sentinel in sentinels.items():
                self.assertIs(sys.modules[name], sentinel)

    def test_unchanged_content_never_closes_or_reloads_working_instance(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        instance, runtime = loader.bridge, loader.runtime
        self.assertFalse(loader.refresh())
        self.assertIs(loader.bridge, instance)
        self.assertIs(loader.runtime, runtime)
        self.assertEqual(instance.close_count, 0)

    def test_install_marker_defers_loading_a_partial_multi_file_update(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        (self.base / ".updating").touch()
        self.write_runtime("B")
        self.assertFalse(loader.refresh())
        self.assertEqual(loader.bridge.handle(self.request())["result"]["version"], "A")
        self.assertEqual(loader.bridge.close_count, 0)
        (self.base / ".updating").unlink()
        self.assertTrue(loader.refresh())
        self.assertEqual(loader.bridge.handle(self.request())["result"]["version"], "B")

    def test_content_hash_detects_same_size_same_mtime_update_and_preserves_state(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        instance, old_class = loader.bridge, type(loader.bridge)
        key, context, config = instance.api_key, instance.context, instance.config
        instance.session_id = "negotiated-session"
        instance.protocol_version = "negotiated-protocol"
        path = self.base / "bridge_runtime.py"
        before = path.stat()
        self.write_runtime("B")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(path.stat().st_size, before.st_size)
        self.assertTrue(loader.refresh())
        self.assertIs(loader.bridge, instance)
        self.assertIsNot(type(instance), old_class)
        self.assertIs(instance.api_key, key)
        self.assertIs(instance.context, context)
        self.assertIs(instance.config, config)
        self.assertEqual(instance.session_id, "negotiated-session")
        self.assertEqual(instance.protocol_version, "negotiated-protocol")
        self.assertEqual(instance.base, "user-config-directory")
        self.assertEqual(instance.close_count, 1)
        self.assertIsNone(instance.phone)
        self.assertEqual(instance.handle(self.request())["result"]["version"], "B")

    def test_update_never_runs_constructor_again(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        self.write_runtime("B", constructor_fails=True)
        self.assertTrue(loader.refresh())
        self.assertEqual(loader.bridge.handle(self.request())["result"]["version"], "B")

    def test_dependency_only_update_reloads_runtime_bindings_from_same_snapshot(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        (self.base / "hid_transport.py").write_text("VALUE = 'two'\n")
        self.assertTrue(loader.refresh())
        self.assertEqual(loader.bridge.handle(self.request())["result"]["dependency"], "two")
        self.assertEqual(loader.bridge.close_count, 1)

    def test_config_code_refresh_preserves_original_loaded_user_configuration(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        original_config = loader.bridge.config
        original_context = loader.bridge.context
        (self.base / "config.py").write_text(
            "VALUE = 'config-two'\nBASE = 'other-user-config-directory'\nCONFIG = object()\n")
        self.assertTrue(loader.refresh())
        self.assertEqual(loader.runtime.config.VALUE, "config-two")
        self.assertIsNot(loader.runtime.config.CONFIG, original_config)
        self.assertIs(loader.bridge.config, original_config)
        self.assertIs(loader.bridge.context, original_context)
        self.assertEqual(loader.bridge.base, "user-config-directory")

    def test_old_phone_connection_is_closed_before_updated_module_executes(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        events = []
        loader.bridge.close = Mock(side_effect=lambda: events.append("closed"))
        (self.base / "phone_control.py").write_text("from nanokvmgo_mcp import launcher\nlauncher._test_events.append('loaded')\n")
        with patch.object(launcher, "_test_events", events, create=True):
            self.assertTrue(loader.refresh())
        self.assertEqual(events, ["closed", "loaded"])

    def test_failed_update_keeps_working_code_and_session_and_does_not_spam(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        old_class, old_runtime = type(loader.bridge), loader.runtime
        old_dependency = self.module("hid_transport")
        old_package_attributes = {name: getattr(sys.modules[self.package_name],
                                               name.rpartition(".")[2])
                                  for name, _ in self.module_files}
        loader.bridge.session_id = "preserved"
        (self.base / "bridge_runtime.py").write_text("raise RuntimeError('PRIVATE UPDATE DETAIL')\n")
        self.assertFalse(loader.refresh())
        self.assertIs(type(loader.bridge), old_class)
        self.assertIs(loader.runtime, old_runtime)
        self.assertIs(self.module("bridge_runtime"), old_runtime)
        self.assertIs(self.module("hid_transport"), old_dependency)
        for name, previous in old_package_attributes.items():
            self.assertIs(getattr(sys.modules[self.package_name], name.rpartition(".")[2]),
                          previous)
        self.assertEqual(loader.bridge.handle(self.request())["result"]["session"], "preserved")
        self.assertEqual(self.stderr.getvalue(), launcher.RELOAD_ERROR)
        self.assertNotIn("PRIVATE", self.stderr.getvalue())
        self.assertFalse(loader.refresh())
        self.assertEqual(loader.bridge.close_count, 1)
        self.assertEqual(self.stderr.getvalue(), launcher.RELOAD_ERROR)
        self.write_runtime("C")
        self.assertTrue(loader.refresh())
        self.assertEqual(loader.bridge.handle(self.request())["result"]["version"], "C")

    def test_missing_runtime_or_invalid_dependency_rolls_back_module_table(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        old_runtime = loader.runtime
        old_hid = self.module("hid_transport")
        (self.base / "hid_transport.py").write_text("this is ! invalid Python\n")
        self.assertFalse(loader.refresh())
        self.assertIs(self.module("bridge_runtime"), old_runtime)
        self.assertIs(self.module("hid_transport"), old_hid)
        (self.base / "hid_transport.py").write_text("VALUE = 'one'\n")
        (self.base / "bridge_runtime.py").unlink()
        self.assertFalse(loader.refresh())
        self.assertIs(self.module("bridge_runtime"), old_runtime)
        self.assertEqual(loader.bridge.handle(self.request())["result"]["version"], "A")

    def test_missing_snapshot_dependency_cannot_import_from_package_path(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        old_runtime = loader.runtime
        old_reader = self.module("local_reader")
        sources, _ = loader._snapshot()
        sources[self.package_name + ".local_reader"] = None
        with self.assertRaises(ImportError):
            loader._load(sources)
        self.assertIs(loader.runtime, old_runtime)
        self.assertIs(self.module("local_reader"), old_reader)

    def test_incompatible_class_layout_retains_previous_class_and_modules(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        old_class, old_runtime = type(loader.bridge), loader.runtime
        self.write_runtime("B", slots=True)
        self.assertFalse(loader.refresh())
        self.assertIs(type(loader.bridge), old_class)
        self.assertIs(loader.runtime, old_runtime)
        self.assertIs(self.module("bridge_runtime"), old_runtime)

    def test_failed_phone_cleanup_prevents_loading_new_code(self):
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        loader.bridge.close = Mock(side_effect=RuntimeError("PRIVATE CLEANUP DETAIL"))
        old_runtime = loader.runtime
        self.write_runtime("B")
        self.assertFalse(loader.refresh())
        self.assertIs(loader.runtime, old_runtime)
        self.assertEqual(self.stderr.getvalue(), launcher.RELOAD_ERROR)

    def test_stream_refreshes_between_requests_and_preserves_negotiated_session(self):
        def lines():
            yield json.dumps({**self.request(1, "change_session"), "params": {"session": "changed"}})
            self.write_runtime("B")
            yield json.dumps(self.request(2))
        stdout = io.StringIO()
        self.assertEqual(launcher.serve(self.base, lines(), stdout, self.stderr), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r["result"]["version"] for r in responses], ["A", "B"])
        self.assertEqual([r["result"]["session"] for r in responses], ["changed", "changed"])
        self.assertEqual(self.module("bridge_runtime").CLOSE_COUNT, 1)

    def test_notification_has_no_response_and_shutdown_closes_bridge(self):
        stdout = io.StringIO()
        stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.assertEqual(launcher.serve(self.base, stdin, stdout, self.stderr), 0)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(self.module("bridge_runtime").CLOSE_COUNT, 1)

    def test_request_failure_is_generic_not_retried_and_next_request_still_works(self):
        stdin = io.StringIO("\n".join(json.dumps(self.request(i, method))
                                       for i, method in [(1, "fail"), (2, "test")]))
        stdout = io.StringIO()
        self.assertEqual(launcher.serve(self.base, stdin, stdout, self.stderr), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(responses[0]["error"]["code"], -32603)
        self.assertEqual(responses[1]["result"]["version"], "A")
        self.assertNotIn("PRIVATE", stdout.getvalue() + self.stderr.getvalue())

    def test_invalid_json_and_non_object_requests_return_protocol_errors(self):
        stdout = io.StringIO()
        self.assertEqual(launcher.serve(self.base, io.StringIO("\nnot json\n[]\n"), stdout, self.stderr), 0)
        responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([r["error"]["code"] for r in responses], [-32700, -32600])

    def test_keyboard_interrupt_always_closes_connection(self):
        def interrupted():
            yield json.dumps(self.request())
            raise KeyboardInterrupt
        self.assertEqual(launcher.serve(self.base, interrupted(), io.StringIO(), self.stderr), 0)
        self.assertEqual(self.module("bridge_runtime").CLOSE_COUNT, 1)

    def test_sigterm_handler_interrupts_and_finally_closes_then_restores_handler(self):
        handlers = {}
        prior = object()
        def set_signal(sig, handler):
            handlers.setdefault(sig, []).append(handler)
        def terminated():
            yield json.dumps(self.request())
            handlers[launcher.signal.SIGTERM][0](launcher.signal.SIGTERM, None)
        with patch.object(launcher.signal, "getsignal", return_value=prior), \
             patch.object(launcher.signal, "signal", side_effect=set_signal):
            self.assertEqual(launcher.serve(self.base, terminated(), io.StringIO(), self.stderr,
                                           install_signals=True), 0)
        self.assertEqual(self.module("bridge_runtime").CLOSE_COUNT, 1)
        self.assertIs(handlers[launcher.signal.SIGTERM][-1], prior)

    def test_initial_failure_emits_only_generic_stderr(self):
        (self.base / "bridge_runtime.py").write_text("raise RuntimeError('PRIVATE START DETAIL')\n")
        stdout = io.StringIO()
        self.assertEqual(launcher.serve(self.base, io.StringIO(), stdout, self.stderr), 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(self.stderr.getvalue(), launcher.START_ERROR)

    def test_sys_path_is_restored_after_success_and_failure(self):
        original = sys.path[:]
        loader = launcher.HotReloadBridge(self.base, self.stderr)
        loader.refresh()
        self.assertEqual(sys.path, original)
        (self.base / "bridge_runtime.py").write_text("!syntax error\n")
        loader.refresh()
        self.assertEqual(sys.path, original)


if __name__ == "__main__":
    unittest.main()
