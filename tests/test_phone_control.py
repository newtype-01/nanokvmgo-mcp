"""Offline phone gesture failure-path checks; no images, credentials or networking."""

import base64
import pathlib
import threading
import unittest
from unittest.mock import Mock, patch

from nanokvmgo_mcp import phone_control as control


def frame(name):
    return {
        "raw": ("raw-" + name).encode(),
        "image": {"type": "image", "mimeType": "image/jpeg",
                  "data": base64.b64encode(("cropped-" + name).encode()).decode()},
        "metadata": {"crop_box": [100, 200, 500, 1000], "captureId": name},
    }


class FakeInput:
    def __init__(self):
        self.events = []
        self.stopped = threading.Event()
        self.relative_error_at = None
        self.relative_error = None
        self.release_error = None
        self.relative_count = 0
        self.release_count = 0
        self.close_count = 0

    def relative(self, buttons=0, dx=0, dy=0, wheel=0):
        self.relative_count += 1
        self.events.append(("relative", buttons, dx, dy, wheel))
        if self.relative_count == self.relative_error_at:
            raise self.relative_error

    def release(self):
        self.release_count += 1
        self.events.append(("release",))
        if self.release_error:
            raise self.release_error

    def close(self):
        self.close_count += 1
        self.events.append(("close",))


def make_controller():
    channel = FakeInput()
    bridge = Mock()
    phone = control.PhoneController(bridge, pathlib.Path("/offline-test-only"),
                                    "192.0.2.1", "localhost", "test-pin")
    phone.connection = Mock(return_value=channel)
    phone._sleep = Mock()
    phone.capture = Mock(side_effect=lambda *args, **kwargs: frame("new"))
    # Most tests isolate gesture cleanup; stability is tested separately below.
    phone.capture_stable = Mock(side_effect=lambda deadline, quality=85, **kwargs: phone.capture(deadline, quality))
    phone.position = Mock(side_effect=lambda current, target, *args, **kwargs: (target, current))
    return phone, channel


class GestureReleaseTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(control.time, "monotonic", return_value=100.0)
        clock.start()
        self.addCleanup(clock.stop)

    def test_drag_releases_on_press_move_hold_and_stop_failure_for_all_exit_types(self):
        for error_type in [RuntimeError, KeyboardInterrupt, SystemExit]:
            for phase in ["press", "first_move", "middle_move", "hold", "move_sleep",
                          "stop_report", "stop_pause"]:
                with self.subTest(error=error_type.__name__, phase=phase):
                    phone, channel = make_controller()
                    failure = error_type("offline injected failure")
                    if phase in ("press", "first_move", "middle_move", "stop_report"):
                        channel.relative_error_at = {"press": 1, "first_move": 2,
                                                     "middle_move": 7, "stop_report": 16}[phase]
                        channel.relative_error = failure
                    else:
                        fail_at = {"hold": 1, "move_sleep": 2, "stop_pause": 16}[phase]
                        sleeps = 0
                        def sleep(seconds, deadline):
                            nonlocal sleeps
                            sleeps += 1
                            if sleeps == fail_at:
                                raise failure
                        phone._sleep.side_effect = sleep
                    with self.assertRaises(error_type):
                        phone.drag(140, 500, 200)
                    self.assertEqual(channel.release_count, 1)
                    self.assertEqual(channel.events[-1], ("release",))
                    self.assertFalse(phone.buttons_may_be_held)
                    # A zero-motion held report at the end is a stop, not a
                    # second click. All reports precede the single release.
                    relative = [event for event in channel.events if event[0] == "relative"]
                    self.assertEqual(relative[0], ("relative", 1, 0, 0, 0))
                    self.assertTrue(all(event[1] == 1 for event in relative))
                    held_zero = [event for event in relative if event[2:] == (0, 0, 0)]
                    self.assertEqual(len(held_zero), 2 if phase in ("stop_report", "stop_pause") else 1)
                    if phase in ("stop_report", "stop_pause"):
                        self.assertEqual(channel.events[-2], ("relative", 1, 0, 0, 0))

    def test_successful_drag_is_bounded_retains_hold_and_stops_before_release(self):
        phone, channel = make_controller()
        phone.drag(-137, 500, 200)
        relative = [event for event in channel.events if event[0] == "relative"]
        self.assertEqual(len(relative), 16)
        self.assertTrue(all(event[1] == 1 for event in relative))
        self.assertEqual(sum(event[3] for event in relative), -137)
        self.assertEqual(relative[0], ("relative", 1, 0, 0, 0))
        self.assertEqual(relative[-1], ("relative", 1, 0, 0, 0))
        self.assertTrue(all(event[3] != 0 for event in relative[1:-1]))
        self.assertEqual(channel.events[-1], ("release",))
        self.assertEqual(phone._sleep.call_args_list[-1].args, (.18, 200))
        self.assertFalse(phone.buttons_may_be_held)

    def test_drag_sends_explicit_held_zero_motion_before_stop_pause_and_release(self):
        phone, channel = make_controller()
        phone._sleep.side_effect = lambda seconds, deadline: channel.events.append(("sleep", seconds))
        phone.drag(140, 500, 200)
        self.assertEqual(channel.events[-3:], [
            ("relative", 1, 0, 0, 0), ("sleep", .18), ("release",)
        ])
        self.assertEqual(channel.relative_count, 16)
        self.assertEqual(channel.release_count, 1)

    def test_failed_stop_report_never_skips_release_or_retries_for_all_exit_types(self):
        for error_type in [RuntimeError, KeyboardInterrupt, SystemExit]:
            with self.subTest(error=error_type.__name__):
                phone, channel = make_controller()
                channel.relative_error_at = 16
                channel.relative_error = error_type("zero-stop send failed")
                with self.assertRaises(error_type):
                    phone.drag(140, 500, 200)
                self.assertEqual(channel.events[-2:], [("relative", 1, 0, 0, 0), ("release",)])
                self.assertEqual(channel.relative_count, 16)
                self.assertEqual(channel.release_count, 1)
                self.assertFalse(phone.buttons_may_be_held)
                # The pause is not entered after its preceding stop report failed.
                self.assertNotIn((.18, 200), [call.args for call in phone._sleep.call_args_list])

    def test_smooth_drag_eases_both_ends_without_direction_reversal_or_lost_distance(self):
        for delta in [100, -100, 340, -340]:
            for duration in [500, 800, 1400]:
                with self.subTest(delta=delta, duration=duration):
                    phone, channel = make_controller()
                    phone.drag(delta, duration, 200)
                    reports = [event for event in channel.events if event[0] == "relative"]
                    self.assertEqual(reports[0], ("relative", 1, 0, 0, 0))
                    self.assertEqual(reports[-1], ("relative", 1, 0, 0, 0))
                    movements = [event[3] for event in reports[1:-1]]
                    nonzero = [abs(value) for value in movements if value]
                    self.assertGreaterEqual(len(nonzero), 3)
                    self.assertLess(nonzero[0], max(nonzero))
                    self.assertLess(nonzero[-1], max(nonzero))
                    self.assertTrue(all(value >= 0 for value in movements) if delta > 0
                                    else all(value <= 0 for value in movements))
                    self.assertEqual(sum(movements), delta)
                    position = 0
                    for movement in movements:
                        position += movement
                        self.assertTrue(0 <= position <= delta if delta > 0 else delta <= position <= 0)
                    self.assertEqual(channel.release_count, 1)
                    self.assertFalse(phone.buttons_may_be_held)

    def test_drag_expired_or_insufficient_budget_never_presses(self):
        for deadline in [99, 100, 101.49]:
            with self.subTest(deadline=deadline):
                phone, channel = make_controller()
                with self.assertRaises(TimeoutError):
                    phone.drag(100, 500, deadline)
                self.assertEqual(channel.events, [])
                self.assertFalse(phone.buttons_may_be_held)

    def test_drag_deadline_expiring_mid_gesture_still_releases(self):
        phone, channel = make_controller()
        phone._check = Mock(side_effect=[None, TimeoutError("deadline")])
        with self.assertRaises(TimeoutError):
            phone.drag(100, 500, 200)
        self.assertEqual(channel.events, [("relative", 1, 0, 0, 0), ("release",)])
        self.assertFalse(phone.buttons_may_be_held)

    def test_drag_release_failure_marks_buttons_uncertain_and_does_not_replay(self):
        for error_type in [RuntimeError, KeyboardInterrupt, SystemExit]:
            with self.subTest(error=error_type.__name__):
                phone, channel = make_controller()
                channel.release_error = error_type("release failed")
                with self.assertRaises(error_type):
                    phone.drag(100, 500, 200)
                self.assertTrue(phone.buttons_may_be_held)
                self.assertEqual(channel.release_count, 1)
                self.assertEqual(channel.relative_count, 16)

    def test_tap_interrupts_propagate_after_releasing_and_do_not_capture_recovery(self):
        for error_type in [KeyboardInterrupt, SystemExit]:
            for phase in ["press", "hold", "settle"]:
                with self.subTest(error=error_type.__name__, phase=phase):
                    phone, channel = make_controller()
                    failure = error_type("cancelled")
                    if phase == "press":
                        channel.relative_error_at, channel.relative_error = 1, failure
                    elif phase == "hold":
                        phone._sleep.side_effect = failure
                    else:
                        phone._sleep.side_effect = [None, failure]
                    with self.assertRaises(error_type):
                        phone.run({"action": "tap", "x": 500, "y": 500}, 200)
                    self.assertEqual(channel.relative_count, 1)
                    self.assertEqual(channel.release_count, 1)
                    self.assertFalse(phone.buttons_may_be_held)
                    self.assertEqual(phone.capture.call_count, 1)

    def test_tap_exception_uses_fresh_recovery_without_replaying_press(self):
        for phase in ["press", "hold", "settle"]:
            with self.subTest(phase=phase):
                phone, channel = make_controller()
                phone.capture.side_effect = [frame("initial"), frame("recovery")]
                if phase == "press":
                    channel.relative_error_at, channel.relative_error = 1, RuntimeError("private detail")
                elif phase == "hold":
                    phone._sleep.side_effect = RuntimeError("private detail")
                else:
                    phone._sleep.side_effect = [None, RuntimeError("private detail")]
                summary, images = phone.run({"action": "tap", "x": 500, "y": 500}, 200)
                self.assertFalse(summary["ok"])
                self.assertEqual(summary["outcome"], "action_uncertain")
                self.assertEqual(images, [frame("recovery")["image"]])
                self.assertEqual(channel.relative_count, 1)
                self.assertEqual(channel.release_count, 1)
                self.assertNotIn("private detail", summary["error"])

    def test_tap_release_failure_reports_buttons_may_be_held(self):
        phone, channel = make_controller()
        channel.release_error = RuntimeError("socket failed")
        summary, _ = phone.run({"action": "tap", "x": 500, "y": 500}, 200)
        self.assertFalse(summary["ok"])
        self.assertTrue(summary["buttons_may_be_held"])
        self.assertEqual(channel.relative_count, 1)
        self.assertEqual(channel.release_count, 1)

    def test_tap_position_failure_or_cancellation_never_presses(self):
        for error_type in [control.GestureError, KeyboardInterrupt, SystemExit]:
            with self.subTest(error=error_type.__name__):
                phone, channel = make_controller()
                phone.position.side_effect = error_type("cannot position")
                if error_type is control.GestureError:
                    summary, _ = phone.run({"action": "tap", "x": 500, "y": 500}, 200)
                    self.assertFalse(summary["ok"])
                else:
                    with self.assertRaises(error_type):
                        phone.run({"action": "tap", "x": 500, "y": 500}, 200)
                self.assertEqual(channel.events, [])
                phone.connection.assert_not_called()

    def test_tap_deadline_exhausted_by_position_or_insufficient_margin_never_presses(self):
        for deadline in [100, 100.49]:
            with self.subTest(deadline=deadline):
                phone, channel = make_controller()
                summary, _ = phone.run({"action": "tap", "x": 500, "y": 500}, deadline)
                self.assertFalse(summary["ok"])
                self.assertEqual(channel.events, [])
                self.assertFalse(phone.buttons_may_be_held)

    def test_close_always_closes_socket_even_when_release_is_interrupted(self):
        for error_type in [None, RuntimeError, KeyboardInterrupt, SystemExit]:
            with self.subTest(error=error_type):
                phone, channel = make_controller()
                phone.input = channel
                phone.buttons_may_be_held = True
                if error_type:
                    channel.release_error = error_type("close interruption")
                if error_type in (KeyboardInterrupt, SystemExit):
                    with self.assertRaises(error_type):
                        phone.close()
                else:
                    phone.close()
                self.assertIsNone(phone.input)
                self.assertEqual(channel.close_count, 1)
                self.assertEqual(channel.release_count, 1)
                self.assertEqual(phone.buttons_may_be_held, error_type is not None)
                phone.close()
                self.assertEqual(channel.close_count, 1)


class FreshFrameAndScrollTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(control.time, "monotonic", return_value=100.0)
        clock.start()
        self.addCleanup(clock.stop)

    def test_successful_tap_returns_only_post_tap_image_with_dispatch_only_status(self):
        phone, channel = make_controller()
        phone.capture.side_effect = [frame("before"), frame("after")]
        summary, images = phone.run({"action": "tap", "x": 1000, "y": 0}, 200)
        self.assertTrue(summary["ok"])
        self.assertFalse(summary["ui_verified"])
        self.assertEqual(images, [frame("after")["image"]])
        self.assertEqual(summary["screenshot"]["captureId"], "after")
        self.assertEqual(phone.position.call_args.args[1], (499, 200))
        self.assertEqual(channel.events, [("relative", 1, 0, 0, 0), ("release",)])

    def test_failed_post_tap_and_recovery_capture_never_returns_initial_image(self):
        phone, channel = make_controller()
        phone.capture.side_effect = [frame("before"), RuntimeError("fresh capture failed"),
                                    RuntimeError("recovery failed")]
        summary, images = phone.run({"action": "tap", "x": 500, "y": 500}, 200)
        self.assertFalse(summary["ok"])
        self.assertEqual(images, [])
        self.assertNotIn("screenshot", summary)
        self.assertIn("screenshot_error", summary)
        self.assertEqual(channel.relative_count, 1)
        self.assertEqual(channel.release_count, 1)

    def test_scroll_direction_uses_opposite_finger_direction_and_new_frame(self):
        for direction, expected_delta, expected_target in [
            ("up", 340, (388, 400)), ("down", -340, (388, 800))
        ]:
            with self.subTest(direction=direction):
                phone, _ = make_controller()
                phone.drag = Mock()
                before, after = frame("before"), frame("after")
                phone.capture.side_effect = None
                phone.capture.return_value = after
                comparison = {"moved": True, "shift_y": expected_delta}
                with patch.object(control, "measure_scroll", return_value=comparison) as measure:
                    actual, metrics = phone.scroll_one(before, direction, 400, 800, 200, 85)
                self.assertIs(actual, after)
                self.assertEqual({key: value for key, value in metrics.items() if key != "diagnostics"},
                                 {**comparison, "drag_comparison": comparison,
                                  "position_comparison": comparison})
                self.assertIn("capture_count", metrics["diagnostics"])
                self.assertEqual(phone.position.call_args.args[1], expected_target)
                self.assertEqual(phone.position.call_args.kwargs, {"tolerance": 55})
                phone.drag.assert_called_once_with(expected_delta, 800, 200)
                self.assertEqual([call.args for call in measure.call_args_list], [
                    (before["raw"], after["raw"]), (before["raw"], after["raw"]),
                    (before["raw"], before["raw"]),
                ])

    def test_scroll_reports_initial_to_final_movement_separately_from_position_and_drag(self):
        phone, channel = make_controller()
        initial, positioned, final = frame("initial"), frame("positioned"), frame("final")
        phone.position.side_effect = None
        phone.position.return_value = ((388, 400), positioned)
        phone.drag = Mock()
        phone.capture.side_effect = None
        phone.capture.return_value = final
        overall = {"matched": True, "content_shift_y": 69, "overlap_fraction": .94}
        dragged = {"matched": True, "content_shift_y": 338, "overlap_fraction": .69}
        repositioned = {"matched": True, "content_shift_y": -269, "overlap_fraction": .75}
        with patch.object(control, "measure_scroll", side_effect=[overall, dragged, repositioned]) as measure:
            after, comparison = phone.scroll_one(initial, "up", 400, 800, 200, 85)
        self.assertIs(after, final)
        self.assertEqual(comparison["content_shift_y"], 69)
        self.assertEqual(comparison["overlap_fraction"], .94)
        self.assertEqual(comparison["drag_comparison"], dragged)
        self.assertEqual(comparison["position_comparison"], repositioned)
        self.assertEqual([call.args for call in measure.call_args_list], [
            (initial["raw"], final["raw"]), (positioned["raw"], final["raw"]),
            (initial["raw"], positioned["raw"]),
        ])
        # Diagnostics reuse the three existing frames, with no extra input or capture.
        phone.position.assert_called_once()
        phone.drag.assert_called_once_with(340, 800, 200)
        phone.capture_stable.assert_called_once_with(200, 85, allow_localized=True)
        phone.capture.assert_called_once_with(200, 85)
        self.assertEqual(channel.events, [])

    def test_scroll_distance_cap_prevents_large_relative_drag(self):
        phone, _ = make_controller()
        phone.drag = Mock()
        with patch.object(control, "measure_scroll", return_value={"moved": True}):
            phone.scroll_one(frame("before"), "up", 600, 800, 200, 85)
        phone.drag.assert_called_once_with(360, 800, 200)

    def test_scroll_failure_does_not_retry_gesture_and_returns_only_new_recovery(self):
        phone, _ = make_controller()
        phone.capture.side_effect = [frame("initial"), frame("recovery")]
        phone.drag = Mock(side_effect=RuntimeError("uncertain drag"))
        summary, images = phone.run({"action": "scroll", "direction": "up", "pages": 3}, 200)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["pages"], [])
        self.assertEqual(images, [frame("recovery")["image"]])
        phone.drag.assert_called_once()
        phone.position.assert_called_once()

    def test_scroll_returns_each_requested_page_with_distinct_fresh_capture(self):
        phone, _ = make_controller()
        phone.drag = Mock()
        pages = [frame("page-1"), frame("page-2")]
        for page in pages:
            page["metadata"]["frame_stable"] = True
        phone.capture.side_effect = [frame("initial"), *pages]
        with patch.object(control, "measure_scroll", return_value={
            "matched": True, "content_shift_y": -240, "overlap_fraction": .7
        }):
            summary, images = phone.run({"action": "scroll", "direction": "down", "pages": 2}, 200)
        self.assertTrue(summary["ok"])
        self.assertEqual([p["screenshot"]["captureId"] for p in summary["pages"]], ["page-1", "page-2"])
        self.assertEqual([item for item in images if item["type"] == "image"],
                         [frame("page-1")["image"], frame("page-2")["image"]])
        self.assertEqual(phone.drag.call_count, 2)
        self.assertEqual([call.kwargs.get("cursor_probe", False)
                          for call in phone.capture_stable.call_args_list], [True, False])

    def test_multi_page_scroll_stops_after_first_unstable_or_unverified_page(self):
        safe = {"matched": True, "content_shift_y": 200, "overlap_fraction": .7}
        cases = [
            (False, safe, "frame_unstable"),
            (True, {**safe, "matched": False}, "scroll_unverified"),
            (True, {**safe, "content_shift_y": 0}, "no_visual_change"),
            (True, {**safe, "content_shift_y": -200}, "unexpected_scroll"),
            (True, {**safe, "overlap_fraction": .2}, "unexpected_scroll"),
        ]
        for stable, comparison, expected_outcome in cases:
            with self.subTest(outcome=expected_outcome, comparison=comparison):
                phone, channel = make_controller()
                latest = frame("observed-after-one-gesture")
                latest["metadata"]["frame_stable"] = stable
                phone.scroll_one = Mock(return_value=(latest, comparison))
                summary, images = phone.run({"action": "scroll", "direction": "up", "pages": 3}, 200)
                self.assertFalse(summary["ok"])
                self.assertEqual(summary["outcome"], expected_outcome)
                self.assertEqual(len(summary["pages"]), 1)
                phone.scroll_one.assert_called_once()
                self.assertEqual([item for item in images if item["type"] == "image"], [latest["image"]])
                self.assertEqual(phone.capture.call_count, 1)
                self.assertEqual(channel.events, [])

    def test_multi_page_scroll_keeps_completed_pages_and_stops_on_second_page_direction_error(self):
        phone, _ = make_controller()
        first, second = frame("page-1"), frame("page-2")
        for page in [first, second]:
            page["metadata"]["frame_stable"] = True
        safe = {"matched": True, "content_shift_y": 200, "overlap_fraction": .7}
        phone.scroll_one = Mock(side_effect=[(first, safe),
                                            (second, {**safe, "content_shift_y": -150})])
        summary, images = phone.run({"action": "scroll", "direction": "up", "pages": 3}, 200)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["outcome"], "unexpected_scroll")
        self.assertEqual(phone.scroll_one.call_count, 2)
        self.assertEqual([page["screenshot"]["captureId"] for page in summary["pages"]], ["page-1", "page-2"])
        self.assertEqual([item for item in images if item["type"] == "image"], [first["image"], second["image"]])
        self.assertEqual(phone.capture.call_count, 1)

    def test_initial_capture_failure_never_sends_mouse_input_or_returns_old_image(self):
        phone, channel = make_controller()
        phone.capture.side_effect = RuntimeError("capture failed")
        summary, images = phone.run({"action": "scroll", "direction": "up"}, 200)
        self.assertFalse(summary["ok"])
        self.assertEqual(images, [])
        self.assertEqual(channel.events, [])
        phone.position.assert_not_called()

    def test_scroll_position_failure_never_calls_drag(self):
        phone, channel = make_controller()
        phone.drag = Mock()
        phone.position.side_effect = control.GestureError("pointer uncertain")
        summary, _ = phone.run({"action": "scroll", "direction": "up"}, 200)
        self.assertFalse(summary["ok"])
        phone.drag.assert_not_called()
        self.assertEqual(channel.events, [])

    def test_observe_does_not_connect_or_touch_input(self):
        phone, channel = make_controller()
        summary, images = phone.run({"action": "observe"}, 200)
        self.assertTrue(summary["ok"])
        self.assertEqual(images, [frame("new")["image"]])
        phone.capture_stable.assert_called_once_with(200, 85, allow_localized=True)
        phone.connection.assert_not_called()
        self.assertEqual(channel.events, [])

    def test_explicit_release_clears_uncertain_hold_and_captures_fresh_screen(self):
        phone, channel = make_controller()
        phone.buttons_may_be_held = True
        summary, images = phone.run({"action": "release"}, 200)
        self.assertTrue(summary["ok"])
        self.assertFalse(summary["buttons_may_be_held"])
        self.assertEqual(channel.events, [("release",)])
        self.assertEqual(images, [frame("new")["image"]])


class CaptureAndPositionTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(control.time, "monotonic", return_value=100.0)
        clock.start()
        self.addCleanup(clock.stop)

    def test_capture_reads_new_mcp_image_and_preserves_capture_identifier(self):
        phone, _ = make_controller()
        del phone.capture
        raw, cropped = b"source-image", b"cropped-image"
        result = {"structuredContent": {"captureId": "fresh-id"},
                  "content": [{"type": "image", "data": base64.b64encode(raw).decode()}]}
        phone.bridge.call_tool.return_value = result
        phone.bridge.tool_succeeded.return_value = True
        with patch.object(control, "crop_phone", return_value=(cropped, {"crop_box": [1, 2, 401, 802]})) as crop:
            actual = phone.capture(200, 90)
        phone.bridge.call_tool.assert_called_once_with("screenshot", {"quality": 90, "timeoutMs": 5000}, 200)
        crop.assert_called_once_with(raw)
        self.assertEqual(actual["raw"], raw)
        self.assertEqual(base64.b64decode(actual["image"]["data"]), cropped)
        self.assertEqual(actual["metadata"]["captureId"], "fresh-id")

    def test_capture_missing_image_or_remote_failure_is_error(self):
        for success, result in [(False, {}), (True, {"content": []})]:
            with self.subTest(success=success):
                phone, _ = make_controller()
                del phone.capture
                phone.bridge.call_tool.return_value = result
                phone.bridge.tool_succeeded.return_value = success
                with self.assertRaises(control.GestureError):
                    phone.capture(200)

    def test_pointer_probe_is_bounded_unpressed_and_fails_without_clicking(self):
        phone, channel = make_controller()
        with patch.object(control, "find_pointer", return_value=None):
            with self.assertRaises(control.GestureError):
                phone.locate(frame("initial"), 200, 85)
        self.assertEqual(channel.release_count, 1)
        self.assertEqual(channel.relative_count, 3)
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))

    def test_position_corrects_actual_cursor_using_unpressed_movement(self):
        phone, channel = make_controller()
        del phone.position
        initial, after = frame("initial"), frame("after")
        phone.locate = Mock(return_value=((200, 400), initial))
        phone.capture.side_effect = None
        phone.capture.return_value = after
        with patch.object(control, "find_pointer", return_value=(400, 400)):
            point, actual = phone.position(initial, (400, 400), 200, 85)
        self.assertEqual(point, (400, 400))
        self.assertIs(actual, after)
        self.assertEqual(channel.events, [("relative", 0, 80, 0, 0)])

    def test_scroll_tolerance_avoids_unnecessary_fine_pointer_adjustment(self):
        phone, channel = make_controller()
        del phone.position
        initial = frame("initial")
        phone.locate = Mock(return_value=((370, 425), initial))
        point, actual = phone.position(initial, (400, 400), 200, 85, tolerance=55)
        self.assertEqual(point, (370, 425))
        self.assertIs(actual, initial)
        self.assertEqual(channel.events, [])
        phone.capture.assert_not_called()

    def test_bounds_reject_non_phone_rectangles_before_any_click(self):
        for bounds in [None, [1, 2, 3], [0, 0, 120, 800], [0, 0, 1600, 900]]:
            with self.subTest(bounds=bounds):
                phone, channel = make_controller()
                invalid = frame("bad")
                invalid["metadata"]["crop_box"] = bounds
                phone.capture.side_effect = None
                phone.capture.return_value = invalid
                summary, _ = phone.run({"action": "tap", "x": 500, "y": 500}, 200)
                self.assertFalse(summary["ok"])
                self.assertEqual(channel.events, [])


class CursorReuseTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(control.time, "monotonic", return_value=100.0)
        clock.start()
        self.addCleanup(clock.stop)

    def hint(self, current, channel, point=(200, 400)):
        return {"point": point, "capture_id": current["metadata"]["captureId"],
                "bounds": (100, 200, 500, 1000), "channel": channel}

    def stable_frame(self, name):
        current = frame(name)
        current["metadata"].update(frame_stable=True, fresh_capture=True)
        return current

    def test_probe_reuses_two_stability_captures_and_caches_only_observed_cursor(self):
        phone, channel = make_controller()
        del phone.capture_stable
        first, following = frame("first"), frame("following")
        phone.capture.side_effect = [first, following]
        change = {"comparable": True, "mean_difference": .4, "changed_fraction": .005}
        with patch.object(control, "frame_change", return_value=change), \
             patch.object(control, "find_pointer", return_value=(380, 700)) as pointer:
            actual = phone.capture_stable(200, cursor_probe=True)
        self.assertIs(actual, following)
        self.assertEqual(phone.capture.call_count, 2)
        self.assertEqual(channel.events, [("release",), ("relative", 0, -12, 0, 0)])
        pointer.assert_called_once_with(first["raw"], following["raw"])
        self.assertEqual(phone._cursor_hint["point"], (380, 700))
        self.assertEqual(phone._cursor_hint["capture_id"], "following")
        self.assertIs(phone._cursor_hint["channel"], channel)
        self.assertEqual(phone._diagnostics["cursor_hint_prepared"], 1)

    def test_probe_difference_does_not_relax_stability_threshold_or_seed_later_pair(self):
        phone, channel = make_controller()
        del phone.capture_stable
        first, probe_frame, final = frame("first"), frame("probe"), frame("final")
        phone.capture.side_effect = [first, probe_frame, final]
        changes = [{"comparable": True, "mean_difference": 1.31, "changed_fraction": .016},
                   {"comparable": True, "mean_difference": .2, "changed_fraction": .001}]
        with patch.object(control, "frame_change", side_effect=changes), \
             patch.object(control, "find_pointer") as pointer:
            actual = phone.capture_stable(200, cursor_probe=True)
        self.assertIs(actual, final)
        self.assertFalse(probe_frame["metadata"]["frame_stable"])
        self.assertTrue(final["metadata"]["frame_stable"])
        self.assertIsNone(phone._cursor_hint)
        self.assertEqual(phone.capture.call_count, 3)
        pointer.assert_not_called()
        self.assertEqual(channel.relative_count, 1)
        del phone.position
        phone.locate = Mock(return_value=((400, 400), final))
        phone.position(final, (400, 400), 200, 85)
        phone.locate.assert_called_once_with(final, 200, 85)

    def test_pointer_failure_in_stable_probe_pair_falls_back_to_normal_locate(self):
        phone, _ = make_controller()
        del phone.capture_stable
        first, following = frame("first"), frame("following")
        phone.capture.side_effect = [first, following]
        zero = {"comparable": True, "mean_difference": 0, "changed_fraction": 0}
        with patch.object(control, "frame_change", return_value=zero), \
             patch.object(control, "find_pointer", return_value=None):
            phone.capture_stable(200, cursor_probe=True)
        self.assertIsNone(phone._cursor_hint)
        del phone.position
        phone.locate = Mock(return_value=((400, 400), following))
        phone.position(following, (400, 400), 200, 85)
        phone.locate.assert_called_once_with(following, 200, 85)

    def test_stale_capture_ids_cannot_seed_hint_even_when_pixels_and_pointer_look_stable(self):
        phone, _ = make_controller()
        del phone.capture_stable
        phone.capture.side_effect = [frame("same")] * 5
        zero = {"comparable": True, "mean_difference": 0, "changed_fraction": 0}
        with patch.object(control, "frame_change", return_value=zero), \
             patch.object(control, "find_pointer", return_value=(380, 700)) as pointer:
            actual = phone.capture_stable(200, cursor_probe=True)
        self.assertFalse(actual["metadata"]["frame_stable"])
        self.assertFalse(actual["metadata"]["fresh_capture"])
        self.assertIsNone(phone._cursor_hint)
        pointer.assert_not_called()

    def test_hint_skips_locate_but_requires_fresh_unpressed_correction_image(self):
        phone, channel = make_controller()
        del phone.position
        current, following = self.stable_frame("cached"), frame("verified")
        phone._cursor_hint = self.hint(current, channel)
        phone.locate = Mock(side_effect=AssertionError("A valid hint should save locate"))
        phone.capture.side_effect = None
        phone.capture.return_value = following
        with patch.object(control, "find_pointer", return_value=(400, 400)) as pointer:
            point, actual = phone.position(current, (400, 400), 200, 85)
        self.assertEqual(point, (400, 400))
        self.assertIs(actual, following)
        self.assertEqual(channel.events, [("relative", 0, 80, 0, 0)])
        phone.capture.assert_called_once_with(200, 85)
        pointer.assert_called_once_with(current["raw"], following["raw"])
        self.assertEqual(phone._diagnostics["cursor_hint_uses"], 1)
        self.assertIsNone(phone._cursor_hint)

    def test_hint_never_allows_press_based_on_duplicate_correction_frame(self):
        phone, channel = make_controller()
        del phone.position
        current = self.stable_frame("cached")
        phone._cursor_hint = self.hint(current, channel)
        phone.locate = Mock(side_effect=control.GestureError("Fresh pointer unavailable"))
        phone.capture.side_effect = None
        phone.capture.return_value = current
        with patch.object(control, "find_pointer", return_value=(400, 400)) as pointer:
            with self.assertRaises(control.GestureError):
                phone.position(current, (400, 400), 200, 85)
        pointer.assert_not_called()
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))
        self.assertIsNone(phone._cursor_hint)

    def test_pointer_between_same_capture_id_never_trusts_pixel_difference(self):
        before, after = frame("same-id"), frame("same-id")
        after["raw"] = b"different-pixels-but-stale-id"
        with patch.object(control, "find_pointer", return_value=(400, 400)) as pointer:
            self.assertIsNone(control.PhoneController._pointer_between(before, after))
        pointer.assert_not_called()

    def test_position_rejects_a_b_a_old_capture_even_when_detector_would_reach_target(self):
        phone, channel = make_controller()
        del phone.position
        initial, following = frame("A"), frame("B")
        phone.locate = Mock(side_effect=[((200, 400), initial), control.GestureError("No fresh cursor")])
        phone.capture.side_effect = [following, initial, initial]
        with patch.object(control, "find_pointer", side_effect=[(440, 400), (400, 400)]) as pointer:
            with self.assertRaises(control.GestureError):
                phone.position(initial, (400, 400), 200, 85)
        self.assertEqual(pointer.call_count, 1)
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))
        self.assertIsNone(phone._position_capture_ids)

    def test_invalid_hint_or_already_close_target_uses_normal_fresh_locate(self):
        for invalid in ["connection", "capture_id", "bounds", "unstable", "not_fresh", "near_target", "held"]:
            with self.subTest(invalid=invalid):
                phone, channel = make_controller()
                del phone.position
                current = self.stable_frame("cached")
                hint = self.hint(current, channel)
                if invalid == "connection":
                    hint["channel"] = FakeInput()
                elif invalid == "capture_id":
                    hint["capture_id"] = "old-id"
                elif invalid == "bounds":
                    hint["bounds"] = (99, 200, 500, 1000)
                elif invalid == "unstable":
                    current["metadata"]["frame_stable"] = False
                elif invalid == "not_fresh":
                    current["metadata"]["fresh_capture"] = False
                elif invalid == "near_target":
                    hint["point"] = (400, 400)
                else:
                    phone.buttons_may_be_held = True
                phone._cursor_hint = hint
                phone.locate = Mock(return_value=((400, 400), current))
                phone.position(current, (400, 400), 200, 85)
                phone.locate.assert_called_once_with(current, 200, 85)
                self.assertEqual(phone._diagnostics["cursor_hint_uses"], 0)
                self.assertIsNone(phone._cursor_hint)

    def test_verified_gain_persists_without_reusing_cursor_position(self):
        phone, channel = make_controller()
        del phone.position
        initial, next_initial = frame("initial-1"), frame("initial-2")
        phone.locate = Mock(side_effect=[((200, 400), initial), ((200, 400), next_initial)])
        phone.capture.side_effect = [frame("corrected-1"), frame("corrected-2"), frame("corrected-3")]
        with patch.object(control, "find_pointer", side_effect=[(440, 400), (401, 400), (401, 400)]):
            phone.position(initial, (400, 400), 200, 85)
            phone.position(next_initial, (400, 400), 200, 85)
        self.assertEqual(channel.events, [("relative", 0, 80, 0, 0),
                                          ("relative", 0, -13, 0, 0),
                                          ("relative", 0, 67, 0, 0)])
        self.assertEqual(phone.locate.call_count, 2)
        self.assertEqual(phone.capture.call_count, 3)
        self.assertEqual(phone._gains, (3.0, 2.5))

    def test_run_hints_never_survive_between_requests_or_interruptions(self):
        phone, _ = make_controller()
        phone._cursor_hint = {"old": True}
        def stable(*args, **kwargs):
            self.assertIsNone(phone._cursor_hint)
            phone._cursor_hint = {"should_be_cleared": True}
            raise SystemExit
        phone.capture_stable.side_effect = stable
        with self.assertRaises(SystemExit):
            phone.run({"action": "observe"}, 200)
        self.assertIsNone(phone._cursor_hint)

    def test_real_capture_diagnostics_count_requests_without_recording_cookie_or_text(self):
        phone, _ = make_controller()
        del phone.capture
        del phone.capture_stable
        phone.bridge.tool_succeeded.return_value = True
        phone.bridge.call_tool.side_effect = [
            {"content": [{"type": "image", "data": base64.b64encode(b"first").decode()}],
             "structuredContent": {"captureId": "one"}},
            {"content": [{"type": "image", "data": base64.b64encode(b"second").decode()}],
             "structuredContent": {"captureId": "two"}},
        ]
        zero = {"comparable": True, "mean_difference": 0, "changed_fraction": 0}
        with patch.object(control, "crop_phone", return_value=(b"cropped", {"crop_box": [100, 200, 500, 1000]})), \
             patch.object(control, "frame_change", return_value=zero):
            summary, _ = phone.run({"action": "observe"}, 200)
        self.assertTrue(summary["ok"])
        diagnostics = summary["diagnostics"]
        self.assertEqual(diagnostics["capture_count"], 2)
        self.assertEqual(diagnostics["position_steps"], 0)
        self.assertEqual(diagnostics["timing_ms"], {"capture": 0.0, "stabilize": 0.0})
        self.assertEqual(set(diagnostics), {"capture_count", "position_steps", "cursor_hint_uses",
                                           "cursor_hint_prepared", "cursor_probe_count", "animation_assessments",
                                           "animation_context_uses", "animation_position_recoveries", "timing_ms"})


class LocalizedAnimationTests(unittest.TestCase):
    REGIONS = [[170, 400, 290, 560]]
    MOVING = {"comparable": True, "mean_difference": 4, "changed_fraction": .04}
    QUIET = {"comparable": True, "mean_difference": .1, "changed_fraction": .001}

    def setUp(self):
        clock = patch.object(control.time, "monotonic", return_value=100.0)
        clock.start()
        self.addCleanup(clock.stop)

    def assessment(self, captures):
        ids = [capture["metadata"]["captureId"] for capture in captures]
        return {"stable": True, "comparable": True, "localized_motion": True,
                "ignored_regions": self.REGIONS,
                "evidence": {"capture_ids": ids, "latest_capture_id": ids[-1],
                             "zero_shift_verified": True, "reason": "static_text_verified"}}

    def animated_frame(self, name):
        current = frame(name)
        current["metadata"].update(frame_stable=False, text_stable=True, localized_motion=True,
                                   mask_validated=True, fresh_capture=True,
                                   ignored_regions=self.REGIONS, stability_basis="static_text_regions")
        return current

    def test_ordinary_global_stability_keeps_two_captures_and_never_assesses_animation(self):
        phone, channel = make_controller()
        del phone.capture_stable
        captures = [frame("one"), frame("two")]
        phone.capture.side_effect = captures
        with patch.object(control, "frame_change", return_value=self.QUIET), \
             patch.object(control, "assess_stability") as assess:
            result = phone.capture_stable(200, allow_localized=True)
        self.assertIs(result, captures[-1])
        self.assertEqual(phone.capture.call_count, 2)
        self.assertTrue(result["metadata"]["frame_stable"])
        self.assertFalse(result["metadata"]["localized_motion"])
        self.assertEqual(result["metadata"]["ignored_regions"], [])
        self.assertEqual(channel.events, [])
        assess.assert_not_called()

    def test_three_no_input_frames_can_validate_local_motion_without_claiming_global_stability(self):
        phone, channel = make_controller()
        del phone.capture_stable
        captures = [frame("one"), frame("two"), frame("three")]
        phone.capture.side_effect = captures
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability", return_value=self.assessment(captures)) as assess:
            result = phone.capture_stable(200, allow_localized=True)
        self.assertIs(result, captures[-1])
        self.assertEqual(phone.capture.call_count, 3)
        self.assertEqual(assess.call_args.args[0], captures)
        self.assertFalse(result["metadata"]["frame_stable"])
        self.assertTrue(result["metadata"]["text_stable"])
        self.assertTrue(result["metadata"]["localized_motion"])
        self.assertTrue(result["metadata"]["mask_validated"])
        self.assertEqual(result["metadata"]["ignored_regions"], self.REGIONS)
        self.assertEqual(phone._animation_context["capture_id"], "three")
        self.assertEqual(channel.events, [])

    def test_probe_pair_cannot_be_used_as_no_input_animation_evidence(self):
        phone, channel = make_controller()
        del phone.capture_stable
        captures = [frame(str(index)) for index in range(4)]
        phone.capture.side_effect = captures
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability", return_value=self.assessment(captures[1:])) as assess:
            result = phone.capture_stable(200, cursor_probe=True, allow_localized=True)
        self.assertIs(result, captures[-1])
        self.assertEqual(phone.capture.call_count, 4)
        self.assertEqual(assess.call_args.args[0], captures[1:])
        self.assertEqual(channel.events, [("release",), ("relative", 0, -12, 0, 0)])
        self.assertIsNone(phone._cursor_hint)

    def test_tap_default_stability_never_uses_local_motion_mask(self):
        phone, _ = make_controller()
        del phone.capture_stable
        phone.capture.side_effect = [frame(str(index)) for index in range(5)]
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability") as assess:
            result = phone.capture_stable(200)
        self.assertFalse(result["metadata"]["frame_stable"])
        self.assertFalse(result["metadata"]["localized_motion"])
        self.assertIsNone(phone._animation_context)
        assess.assert_not_called()
        phone, _ = make_controller()
        phone.run({"action": "tap", "x": 500, "y": 500}, 200)
        phone.capture_stable.assert_called_once_with(200, 85)

    def test_old_capture_ids_cannot_validate_animation_even_if_pixels_move(self):
        phone, _ = make_controller()
        del phone.capture_stable
        phone.capture.side_effect = [frame("A"), frame("B"), frame("A"), frame("A"), frame("B")]
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability") as assess:
            result = phone.capture_stable(200, allow_localized=True)
        self.assertFalse(result["metadata"]["fresh_capture"])
        self.assertFalse(result["metadata"]["text_stable"])
        self.assertIsNone(phone._animation_context)
        assess.assert_not_called()

    def test_global_page_motion_is_not_relabelled_as_animation(self):
        phone, _ = make_controller()
        del phone.capture_stable
        phone.capture.side_effect = [frame(str(index)) for index in range(5)]
        rejected = {"stable": False, "comparable": True, "localized_motion": False,
                    "ignored_regions": [], "evidence": {"reason": "static_text_moved"}}
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability", return_value=rejected):
            result = phone.capture_stable(200, allow_localized=True)
        self.assertFalse(result["metadata"]["frame_stable"])
        self.assertFalse(result["metadata"]["text_stable"])
        self.assertFalse(result["metadata"]["mask_validated"])
        self.assertEqual(result["metadata"]["ignored_regions"], [])
        self.assertEqual(result["metadata"]["stability_evidence"]["reason"], "static_text_moved")
        self.assertIsNone(phone._animation_context)

    def test_localized_assessment_must_match_current_fresh_frames_and_verify_zero_text_shift(self):
        for invalid in ["capture_ids", "latest_capture_id", "zero_shift_verified"]:
            with self.subTest(invalid=invalid):
                phone, _ = make_controller()
                captures = [frame("one"), frame("two"), frame("three")]
                captures[-1]["metadata"]["fresh_capture"] = True
                assessment = self.assessment(captures)
                assessment["evidence"][invalid] = False if invalid == "zero_shift_verified" else "old-frame"
                with patch.object(control, "assess_stability", return_value=assessment):
                    self.assertFalse(phone._assess_localized(captures, captures[-1]))
                self.assertIsNone(phone._animation_context)
                self.assertFalse(captures[-1]["metadata"].get("mask_validated", False))

    def test_scroll_status_accepts_only_fresh_validated_text_stability_and_keeps_other_guards(self):
        current = self.animated_frame("current")
        good = {"matched": True, "content_shift_y": 200, "overlap_fraction": .7}
        self.assertEqual(control.PhoneController.scroll_status(current, good, "up"), ("completed", None))
        for field in ["text_stable", "localized_motion", "mask_validated", "fresh_capture"]:
            with self.subTest(field=field):
                unsafe = self.animated_frame("current")
                unsafe["metadata"][field] = False
                self.assertEqual(control.PhoneController.scroll_status(unsafe, good, "up")[0], "frame_unstable")
        for comparison, outcome in [({**good, "matched": False}, "scroll_unverified"),
                                    ({**good, "content_shift_y": 0}, "no_visual_change"),
                                    ({**good, "content_shift_y": -200}, "unexpected_scroll"),
                                    ({**good, "overlap_fraction": .1}, "unexpected_scroll")]:
            self.assertEqual(control.PhoneController.scroll_status(current, comparison, "up")[0], outcome)

    def test_validated_mask_is_scoped_to_scroll_position_and_never_consumed_by_tap(self):
        phone, channel = make_controller()
        del phone.position
        current, following = self.animated_frame("verified"), frame("corrected")
        context = {"capture_id": "verified", "bounds": (100, 200, 500, 1000), "regions": self.REGIONS}
        phone._animation_context = context
        phone._allow_localized_position = True
        phone.locate = Mock(return_value=((200, 400), current))
        phone.capture.side_effect = None
        phone.capture.return_value = following
        with patch.object(control, "find_pointer", return_value=(400, 400)) as pointer:
            phone.position(current, (400, 400), 200, 85)
        pointer.assert_called_once_with(current["raw"], following["raw"], ignore_regions=self.REGIONS)
        self.assertIsNone(phone._position_animation_regions)
        self.assertIsNone(phone._animation_context)
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))
        phone, _ = make_controller()
        del phone.position
        phone._animation_context = context
        phone.locate = Mock(return_value=((400, 400), current))
        phone.position(current, (400, 400), 200, 85)
        self.assertIsNone(phone._position_animation_regions)
        self.assertEqual(phone._diagnostics["animation_context_uses"], 0)

    def test_initial_animation_recovery_needs_quiet_three_frames_then_new_unpressed_probe(self):
        phone, channel = make_controller()
        phone._allow_localized_position = True
        first, b, c, d, final = [frame(name) for name in ["initial", "B", "C", "D", "final"]]
        phone.capture.side_effect = [b, c, d, final]
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability", return_value=self.assessment([b, c, d])) as assess, \
             patch.object(control, "find_pointer", side_effect=[None, None, (400, 400)]) as pointer:
            point, actual = phone.locate(first, 200, 85)
        self.assertEqual(point, (400, 400))
        self.assertIs(actual, final)
        self.assertEqual(assess.call_args.args[0], [b, c, d])
        self.assertEqual(pointer.call_args.kwargs, {"ignore_regions": self.REGIONS})
        self.assertEqual(channel.relative_count, 2)
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))
        self.assertEqual(phone._diagnostics["animation_position_recoveries"], 1)

    def test_initial_animation_recovery_can_wait_for_five_quiet_frames_before_new_probe(self):
        phone, channel = make_controller()
        phone._allow_localized_position = True
        first, b, c, d, e, f, final = [frame(name) for name in ["initial", "B", "C", "D", "E", "F", "final"]]
        phone.capture.side_effect = [b, c, d, e, f, final]
        observed = []

        def assess(captures):
            observed.append([capture["metadata"]["captureId"] for capture in captures])
            if len(captures) < 5:
                return {"stable": False, "comparable": True, "localized_motion": False,
                        "ignored_regions": [], "evidence": {"reason": "motion_not_persistent"}}
            return self.assessment(captures)

        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability", side_effect=assess), \
             patch.object(control, "find_pointer", side_effect=[None, None, (400, 400)]) as pointer:
            point, actual = phone.locate(first, 200, 85)
        self.assertEqual(point, (400, 400))
        self.assertIs(actual, final)
        self.assertEqual(observed, [["B", "C", "D"], ["B", "C", "D", "E"],
                                    ["B", "C", "D", "E", "F"]])
        self.assertEqual(phone.capture.call_count, 6)
        self.assertEqual(pointer.call_args.kwargs, {"ignore_regions": self.REGIONS})
        self.assertEqual(channel.relative_count, 2)
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))
        self.assertEqual(phone._diagnostics["animation_position_recoveries"], 1)

    def test_recovery_capture_ids_cannot_be_reused_as_fresh_cursor_evidence(self):
        phone, channel = make_controller()
        phone._allow_localized_position = True
        phone._position_capture_ids = {"initial"}
        first, b, c, d, old_d, final = [frame(name) for name in ["initial", "B", "C", "D", "D", "new"]]
        phone.capture.side_effect = [b, c, d, old_d, final]
        with patch.object(control, "frame_change", return_value=self.MOVING), \
             patch.object(control, "assess_stability", return_value=self.assessment([b, c, d])), \
             patch.object(control, "find_pointer", side_effect=[None, None, (400, 400)]) as pointer:
            point, actual = phone.locate(first, 200, 85)
        self.assertEqual(point, (400, 400))
        self.assertIs(actual, final)
        self.assertEqual(pointer.call_count, 3)
        self.assertEqual(pointer.call_args.args, (d["raw"], final["raw"]))
        self.assertEqual(phone._position_capture_ids, {"initial", "B", "C", "D", "new"})
        self.assertEqual(channel.relative_count, 2)

    def test_animation_context_with_old_capture_or_changed_crop_never_masks_position(self):
        for invalid in ["capture_id", "bounds"]:
            with self.subTest(invalid=invalid):
                phone, _ = make_controller()
                del phone.position
                current, after = self.animated_frame("current"), frame("after")
                context = {"capture_id": "current", "bounds": (100, 200, 500, 1000), "regions": self.REGIONS}
                context[invalid] = "old-id" if invalid == "capture_id" else (0, 0, 400, 800)
                phone._animation_context = context
                phone._allow_localized_position = True
                phone.locate = Mock(return_value=((200, 400), current))
                phone.capture.side_effect = None
                phone.capture.return_value = after
                with patch.object(control, "find_pointer", return_value=(400, 400)) as pointer:
                    phone.position(current, (400, 400), 200, 85)
                pointer.assert_called_once_with(current["raw"], after["raw"])
                self.assertEqual(phone._diagnostics["animation_context_uses"], 0)

    def test_mask_touching_cursor_candidate_is_not_expanded_or_retried_as_click(self):
        phone, channel = make_controller()
        del phone.position
        current = self.animated_frame("validated")
        phone._animation_context = {"capture_id": "validated", "bounds": (100, 200, 500, 1000),
                                    "regions": self.REGIONS}
        phone._allow_localized_position = True
        phone.locate = Mock(side_effect=[((200, 400), current), control.GestureError("Cursor touches animation")])
        phone.capture.side_effect = [frame("one"), frame("two")]
        with patch.object(control, "find_pointer", return_value=None) as pointer:
            with self.assertRaises(control.GestureError):
                phone.position(current, (400, 400), 200, 85)
        self.assertTrue(all(call.kwargs.get("ignore_regions") == self.REGIONS for call in pointer.call_args_list))
        self.assertTrue(all(event[1] == 0 for event in channel.events if event[0] == "relative"))
        self.assertIsNone(phone._position_animation_regions)

    def test_scroll_invalidates_old_mask_and_revalidates_without_probe_on_known_animation(self):
        phone, _ = make_controller()
        initial, final = self.animated_frame("old"), self.animated_frame("new")
        phone._animation_context = {"capture_id": "old", "bounds": (100, 200, 500, 1000),
                                    "regions": self.REGIONS}
        phone.drag = Mock()
        def capture_new(deadline, quality, **kwargs):
            self.assertIsNone(phone._animation_context)
            phone._animation_context = {"capture_id": "new", "bounds": (100, 200, 500, 1000),
                                        "regions": [[300, 500, 400, 600]]}
            return final
        phone.capture_stable.side_effect = capture_new
        with patch.object(control, "measure_scroll", return_value={"matched": True, "content_shift_y": 200,
                                                                   "overlap_fraction": .7}):
            phone.scroll_one(initial, "up", 300, 800, 200, 85, prepare_cursor=True)
        phone.capture_stable.assert_called_once_with(200, 85, allow_localized=True)
        self.assertEqual(phone._animation_context["capture_id"], "new")
        self.assertFalse(phone._allow_localized_position)


class StableCaptureTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(control.time, "monotonic", return_value=100.0)
        clock.start()
        self.addCleanup(clock.stop)

    def test_two_distinct_fresh_capture_ids_can_stabilize_and_only_newest_is_returned(self):
        phone, channel = make_controller()
        del phone.capture_stable
        before, after = frame("capture-1"), frame("capture-2")
        phone.capture.side_effect = [before, after]
        change = {"comparable": True, "mean_difference": .2, "changed_fraction": .001}
        with patch.object(control, "frame_change", return_value=change) as compare:
            actual = phone.capture_stable(200, 90)
        self.assertIs(actual, after)
        self.assertIsNot(actual, before)
        self.assertEqual(actual["metadata"]["captureId"], "capture-2")
        self.assertTrue(actual["metadata"]["fresh_capture"])
        self.assertTrue(actual["metadata"]["frame_stable"])
        self.assertEqual(phone.capture.call_count, 2)
        compare.assert_called_once_with(before["raw"], after["raw"])
        self.assertEqual(channel.events, [])

    def test_identical_capture_ids_never_count_as_stable_even_with_identical_pixels(self):
        phone, channel = make_controller()
        del phone.capture_stable
        captures = [frame("capture-%d" % index) for index in range(5)]
        for capture in captures:
            capture["raw"] = b"identical-pixels"
            capture["metadata"]["captureId"] = "repeated-id"
        phone.capture.side_effect = captures
        change = {"comparable": True, "mean_difference": 0, "changed_fraction": 0}
        with patch.object(control, "frame_change", return_value=change) as compare:
            actual = phone.capture_stable(200)
        self.assertIs(actual, captures[-1])
        self.assertFalse(actual["metadata"]["fresh_capture"])
        self.assertFalse(actual["metadata"]["frame_stable"])
        self.assertEqual(phone.capture.call_count, 5)
        self.assertEqual(compare.call_count, 4)
        self.assertEqual(channel.events, [])

    def test_motion_requires_third_new_frame_and_comparison_uses_latest_pair(self):
        phone, _ = make_controller()
        del phone.capture_stable
        first, moving, settled = frame("frame-1"), frame("frame-2"), frame("frame-3")
        phone.capture.side_effect = [first, moving, settled]
        differences = [{"comparable": True, "mean_difference": 12, "changed_fraction": .3},
                       {"comparable": True, "mean_difference": .3, "changed_fraction": .003}]
        with patch.object(control, "frame_change", side_effect=differences) as compare:
            actual = phone.capture_stable(200)
        self.assertIs(actual, settled)
        self.assertTrue(actual["metadata"]["fresh_capture"])
        self.assertTrue(actual["metadata"]["frame_stable"])
        self.assertFalse(moving["metadata"]["frame_stable"])
        self.assertEqual([call.args for call in compare.call_args_list],
                         [(first["raw"], moving["raw"]), (moving["raw"], settled["raw"])])
        self.assertEqual(phone.capture.call_count, 3)

    def test_capture_id_rollback_a_b_a_never_labels_old_a_fresh_or_stable(self):
        phone, _ = make_controller()
        del phone.capture_stable
        first, moving = frame("A"), frame("B")
        captures = [first, moving, frame("A"), frame("A"), frame("A")]
        phone.capture.side_effect = captures
        differences = [{"comparable": True, "mean_difference": 10, "changed_fraction": .2},
                       *[{"comparable": True, "mean_difference": 0, "changed_fraction": 0}] * 3]
        with patch.object(control, "frame_change", side_effect=differences):
            actual = phone.capture_stable(200)
        self.assertIs(actual, captures[-1])
        self.assertEqual(actual["metadata"]["captureId"], "A")
        self.assertFalse(actual["metadata"]["fresh_capture"])
        self.assertFalse(actual["metadata"]["frame_stable"])
        self.assertEqual(phone.capture.call_count, 5)

    def test_continuing_motion_stops_at_five_fresh_frames_without_claiming_stability(self):
        phone, _ = make_controller()
        del phone.capture_stable
        captures = [frame("frame-%d" % index) for index in range(5)]
        phone.capture.side_effect = captures
        change = {"comparable": True, "mean_difference": 8, "changed_fraction": .2}
        with patch.object(control, "frame_change", return_value=change) as compare:
            actual = phone.capture_stable(200)
        self.assertIs(actual, captures[-1])
        self.assertTrue(actual["metadata"]["fresh_capture"])
        self.assertFalse(actual["metadata"]["frame_stable"])
        self.assertEqual(phone.capture.call_count, 5)
        self.assertEqual(compare.call_count, 4)


if __name__ == "__main__":
    unittest.main()
