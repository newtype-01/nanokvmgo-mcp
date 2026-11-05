"""Bounded phone gestures using NanoKVM's official relative InputRouter frames."""

import base64
import copy
import time

from .hid_transport import PinnedInputSocket
from .phone_vision import assess_stability, crop_phone, find_pointer, frame_change, measure_scroll


TOOL_NAME = "phone_control"
TOOL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["observe", "tap", "scroll", "release"]},
        "x": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "Tap x in thousandths of the cropped phone width; use the latest screenshot."},
        "y": {"type": "integer", "minimum": 0, "maximum": 1000,
              "description": "Tap y in thousandths of the cropped phone height; use the latest screenshot."},
        "direction": {"type": "string", "enum": ["up", "down"],
                      "description": "Scroll up reveals earlier/older content (finger drags down); down reveals later/newer content."},
        "pages": {"type": "integer", "minimum": 1, "maximum": 3, "default": 1,
                  "description": "Already-authorized consecutive partial pages; returns a fresh image after EACH page."},
        "distance": {"type": "integer", "minimum": 150, "maximum": 600, "default": 400,
                     "description": "Requested partial-page distance in thousandths of phone height; overlapping pages."},
        "duration_ms": {"type": "integer", "minimum": 500, "maximum": 1400, "default": 800},
        "quality": {"type": "integer", "minimum": 50, "maximum": 95, "default": 85},
    },
    "required": ["action"],
}


def tool_definition():
    return {
        "name": TOOL_NAME,
        "description": (
            "Control the connected iPhone with cropped full-resolution screenshots and "
            "bounded relative gestures. Scroll performs pointer positioning, press, smooth "
            "movement, a stop to suppress fling, release, and a fresh screenshot in ONE call. "
            "Use direction up for older chat messages, down for newer. Default one partial "
            "page keeps overlap; up to three already-decided pages return an image per page. "
            "Tap targets thousandths (0..1000) of the cropped phone screenshot, correcting "
            "the actual visible cursor before clicking. Never retries a gesture, never types "
            "or sends messages. If a decision depends on a page, request one page and inspect "
            "it before the next action. Dispatch success is not semantic UI verification."
        ),
        "inputSchema": copy.deepcopy(TOOL_SCHEMA),
        "annotations": {"readOnlyHint": False, "destructiveHint": True,
                        "idempotentHint": False, "openWorldHint": True},
    }


class GestureError(RuntimeError):
    pass


class PhoneController:
    def __init__(self, bridge, base, host, certificate_name, fingerprint, port=443, cookie_path=None, origin=None):
        self.bridge, self.base = bridge, base
        self.host, self.certificate_name, self.fingerprint = host, certificate_name, fingerprint
        self.port, self.origin = port, origin
        self.cookie_path = cookie_path or (self.base / "web-session.cookie")
        self.input = None
        self.buttons_may_be_held = False
        self._cursor_hint = None
        self._position_capture_ids = None
        self._animation_context = None
        self._position_animation_regions = None
        self._allow_localized_position = False
        self._animation_position_attempted = False
        self._gain_context = None
        self._gains = (2.5, 2.5)
        self._reset_diagnostics()

    def _reset_diagnostics(self):
        self._diagnostics = {"capture_count": 0, "position_steps": 0,
                             "cursor_hint_uses": 0, "cursor_hint_prepared": 0,
                             "cursor_probe_count": 0, "animation_assessments": 0,
                             "animation_context_uses": 0, "animation_position_recoveries": 0,
                             "timing_ms": {}}

    def _timing(self, name, started):
        # Phase timers are inclusive; capture time is also shown separately.
        timings = self._diagnostics["timing_ms"]
        timings[name] = timings.get(name, 0) + (time.monotonic() - started) * 1000

    def _diagnostic_snapshot(self):
        return {**self._diagnostics, "timing_ms": dict(self._diagnostics["timing_ms"])}

    def _diagnostic_delta(self, before=None):
        before = before or {}
        return {**{key: value - before.get(key, 0) for key, value in self._diagnostics.items()
                   if key != "timing_ms"},
                "timing_ms": {key: round(value - before.get("timing_ms", {}).get(key, 0), 1)
                              for key, value in self._diagnostics["timing_ms"].items()}}

    def close(self):
        self._cursor_hint = None
        self._animation_context = None
        self._position_animation_regions = None
        self._gain_context = None
        self._gains = (2.5, 2.5)
        if self.input is not None:
            channel, self.input = self.input, None
            try:
                channel.release()
                self.buttons_may_be_held = False
            except Exception:
                pass
            finally:
                channel.close()

    def connection(self):
        if self.input is not None:
            if self.input.stopped.is_set():
                self.input.close()
                self.input = None
                raise GestureError("Input connection ended; inspect the screen before a new action")
            return self.input
        cookie_file = self.cookie_path
        try:
            from .config import private_secret
            cookie = private_secret(cookie_file, "Web session")
        except OSError:
            raise GestureError("Local NanoKVM web session is missing; a normal login is required") from None
        self.input = PinnedInputSocket(self.bridge.context, self.host, self.certificate_name,
                                       self.fingerprint, cookie, port=self.port, origin=self.origin).connect()
        return self.input

    @staticmethod
    def _check(deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("Phone action deadline exceeded")

    def _sleep(self, seconds, deadline):
        self._check(deadline)
        if seconds > deadline - time.monotonic():
            raise TimeoutError("Insufficient time to complete phone gesture")
        time.sleep(seconds)

    def capture(self, deadline, quality=85):
        self._check(deadline)
        self._diagnostics["capture_count"] += 1
        started = time.monotonic()
        try:
            return self._capture(deadline, quality)
        finally:
            self._timing("capture", started)

    def _capture(self, deadline, quality):
        result = self.bridge.call_tool("screenshot", {"quality": quality, "timeoutMs": 5000}, deadline)
        if not self.bridge.tool_succeeded(result):
            raise GestureError("A fresh screenshot could not be captured")
        for item in result.get("content", []):
            if item.get("type") == "image":
                raw = base64.b64decode(item["data"], validate=True)
                cropped, info = crop_phone(raw)
                info = {**result.get("structuredContent", {}), **info,
                        "source_size": result.get("structuredContent", {}).get("size"),
                        "size": len(cropped), "coordinate_space": "cropped_phone_image"}
                return {"raw": raw, "image": {"type": "image", "mimeType": "image/jpeg",
                                              "data": base64.b64encode(cropped).decode("ascii")},
                        "metadata": info}
        raise GestureError("No fresh screenshot was returned")

    def capture_stable(self, deadline, quality=85, cursor_probe=False, allow_localized=False):
        started = time.monotonic()
        try:
            return self._capture_stable(deadline, quality, cursor_probe, allow_localized)
        finally:
            self._timing("stabilize", started)

    @staticmethod
    def _pointer_between(before, following, seen_ids=None, ignore_regions=None):
        older_id = before["metadata"].get("captureId")
        newer_id = following["metadata"].get("captureId")
        fresh = bool(older_id and newer_id and older_id != newer_id)
        if seen_ids is not None:
            fresh = fresh and newer_id not in seen_ids
            seen_ids.add(newer_id)
        if not fresh:
            return None
        if ignore_regions:
            return find_pointer(before["raw"], following["raw"], ignore_regions=ignore_regions)
        return find_pointer(before["raw"], following["raw"])

    @staticmethod
    def _global_stable(change):
        return (change.get("comparable", False) and change["mean_difference"] < 1.3
                and change["changed_fraction"] < .015)

    def _take_animation_regions(self, frame):
        context, self._animation_context = self._animation_context, None
        metadata = frame["metadata"]
        if (context and context["capture_id"] == metadata.get("captureId")
                and context["bounds"] == self._bounds(frame)
                and metadata.get("fresh_capture") and metadata.get("text_stable")
                and metadata.get("localized_motion") and metadata.get("mask_validated")):
            self._diagnostics["animation_context_uses"] += 1
            return copy.deepcopy(context["regions"])
        return None

    def _assess_localized(self, samples, following):
        if len(samples) < 3:
            return False
        self._diagnostics["animation_assessments"] += 1
        try:
            assessment = assess_stability(samples[-5:])
        except (ValueError, TypeError, KeyError):
            return False
        following["metadata"]["stability_evidence"] = assessment.get("evidence", {})
        regions = assessment.get("ignored_regions") or []
        identifiers = [sample["metadata"].get("captureId") for sample in samples[-5:]]
        evidence = assessment.get("evidence") or {}
        if not (assessment.get("stable") and assessment.get("comparable")
                and assessment.get("localized_motion") and regions
                and evidence.get("capture_ids") == identifiers
                and evidence.get("latest_capture_id") == following["metadata"].get("captureId")
                and evidence.get("zero_shift_verified") is True):
            return False
        metadata = following["metadata"]
        metadata.update(text_stable=True, localized_motion=True, ignored_regions=copy.deepcopy(regions),
                        mask_validated=True, stability_basis="static_text_regions",
                        stability_evidence=assessment.get("evidence", {}))
        self._animation_context = {"capture_id": metadata.get("captureId"),
                                   "bounds": self._bounds(following), "regions": copy.deepcopy(regions)}
        return True

    def _capture_stable(self, deadline, quality, cursor_probe, allow_localized=False,
                        initial_frames=None, max_frames=5):
        samples = list(initial_frames) if initial_frames else [self.capture(deadline, quality)]
        frame = samples[-1]
        seen = {sample["metadata"].get("captureId") for sample in samples}
        if self._position_capture_ids is not None:
            seen.update(self._position_capture_ids)
            self._position_capture_ids.update(seen)
        self._animation_context = None
        channel = None
        if cursor_probe:
            self._cursor_hint = None
            channel = self.connection()
            channel.release()
            self.buttons_may_be_held = False
            self._check(deadline)
            # A small inward horizontal move can be observed in the two frames
            # already needed for stability. No extra screenshot is requested.
            channel.relative(0, -12, 0, 0)
            self._diagnostics["cursor_probe_count"] += 1
        for unused in range(max_frames - len(samples)):
            self._sleep(.12, deadline)
            following = self.capture(deadline, quality)
            change = frame_change(frame["raw"], following["raw"])
            newer_id = following["metadata"].get("captureId")
            fresh = bool(newer_id and newer_id not in seen)
            seen.add(newer_id)
            if self._position_capture_ids is not None:
                self._position_capture_ids.add(newer_id)
            stable = fresh and self._global_stable(change)
            following["metadata"]["frame_stable"] = stable
            following["metadata"]["frame_comparison"] = change
            following["metadata"]["fresh_capture"] = fresh
            following["metadata"].update(text_stable=stable, localized_motion=False,
                                         ignored_regions=[], mask_validated=False,
                                         stability_basis="global_frames")
            if cursor_probe and unused == 0:
                # The first pair contains a pointer input. Animation evidence
                # starts at the frame after that probe, never before it.
                samples = [following]
            elif fresh:
                samples.append(following)
            if stable:
                # Only the directly observed probe pair may seed a hint. If
                # the probe itself or UI motion required more frames, fall back
                # to normal positioning on the next page instead of guessing.
                if cursor_probe and unused == 0:
                    point = self._pointer_between(frame, following)
                    if point is not None:
                        self._cursor_hint = {"point": point, "capture_id": newer_id,
                                             "bounds": self._bounds(following), "channel": channel}
                        self._diagnostics["cursor_hint_prepared"] += 1
                return following
            if fresh and allow_localized and self._assess_localized(samples, following):
                return following
            frame = following
        return frame

    @staticmethod
    def _bounds(frame):
        meta = frame["metadata"]
        # Accept the vision module's explicitly documented source rectangle.
        bounds = meta.get("crop_box") or meta.get("bounds") or meta.get("crop_bounds")
        if not bounds or len(bounds) != 4:
            raise GestureError("Phone screen could not be located; no click or drag was sent")
        left, top, right, bottom = bounds
        if (right - left < 150 or bottom - top < 300
                or not .35 <= (right - left) / (bottom - top) <= .8):
            raise GestureError("Phone screenshot dimensions are invalid")
        return left, top, right, bottom

    def locate(self, frame, deadline, quality):
        channel = self.connection()
        channel.release()
        seen_ids = self._position_capture_ids
        if seen_ids is None:
            seen_ids = {frame["metadata"].get("captureId")}
        # A pointer can finish near the header/footer where detection is
        # intentionally disabled. Move unpressed into the body rather than
        # repeatedly probing sideways outside the detectable region.
        for dx, dy in [(18, 80), (-36, -127), (0, 127)]:
            self._check(deadline)
            before = frame
            channel.relative(0, dx, dy, 0)
            self._diagnostics["position_steps"] += 1
            self._sleep(.12, deadline)
            frame = self.capture(deadline, quality)
            point = self._pointer_between(before, frame, seen_ids, self._position_animation_regions)
            if point is None:
                quiet_before = frame
                self._sleep(.08, deadline)
                frame = self.capture(deadline, quality)
                point = self._pointer_between(before, frame, seen_ids, self._position_animation_regions)
                if (point is None and self._allow_localized_position
                        and not self._position_animation_regions and not self._animation_position_attempted
                        and not self._global_stable(frame_change(quiet_before["raw"], frame["raw"]))):
                    # Both existing quiet frames follow the last unpressed
                    # probe. Further no-input frames can validate animation;
                    # stop at the first sufficient evidence, within the same
                    # five-frame budget as ordinary stability observation.
                    # The next loop iteration must still find a real cursor.
                    self._animation_position_attempted = True
                    frame = self._capture_stable(deadline, quality, False, True,
                                                 initial_frames=[quiet_before, frame], max_frames=5)
                    self._position_animation_regions = self._take_animation_regions(frame)
                    if self._position_animation_regions:
                        self._diagnostics["animation_position_recoveries"] += 1
            if point is not None:
                return point, frame
        raise GestureError("Visible pointer not found; no click or drag was sent")

    def position(self, frame, target, deadline, quality, tolerance=17):
        started = time.monotonic()
        self._position_capture_ids = {frame["metadata"].get("captureId")}
        self._animation_position_attempted = False
        self._position_animation_regions = (self._take_animation_regions(frame)
                                            if self._allow_localized_position else None)
        if not self._allow_localized_position:
            self._animation_context = None
        try:
            return self._position(frame, target, deadline, quality, tolerance)
        finally:
            self._position_capture_ids = None
            self._position_animation_regions = None
            self._timing("position", started)

    def _position(self, frame, target, deadline, quality, tolerance):
        channel = self.connection()
        hint, self._cursor_hint = self._cursor_hint, None
        use_hint = (hint is not None and hint["channel"] is channel
                    and hint["capture_id"] == frame["metadata"].get("captureId")
                    and hint["bounds"] == self._bounds(frame)
                    and frame["metadata"].get("frame_stable")
                    and frame["metadata"].get("fresh_capture")
                    and not self.buttons_may_be_held)
        if use_hint:
            point = hint["point"]
            # A hint never authorizes an immediate press. It can only select an
            # unpressed correction that is followed by a fresh cursor image.
            use_hint = (abs(target[0] - point[0]) > tolerance
                        or abs(target[1] - point[1]) > tolerance)
        if use_hint:
            self._diagnostics["cursor_hint_uses"] += 1
        else:
            point, frame = self.locate(frame, deadline, quality)
            channel = self.connection()
        gain_context = (channel, self._bounds(frame))
        if self._gain_context != gain_context:
            self._gain_context = gain_context
            self._gains = (2.5, 2.5)
        gain_x, gain_y = self._gains
        reacquired = 0
        for unused in range(6):
            error_x, error_y = target[0] - point[0], target[1] - point[1]
            if abs(error_x) <= tolerance and abs(error_y) <= tolerance:
                return point, frame
            dx = max(-127, min(127, round(error_x / gain_x)))
            dy = max(-127, min(127, round(error_y / gain_y)))
            if not dx and not dy:
                return point, frame
            self._check(deadline)
            channel.relative(0, dx, dy, 0)
            self._diagnostics["position_steps"] += 1
            self._sleep(.12, deadline)
            next_frame = self.capture(deadline, quality)
            next_point = self._pointer_between(frame, next_frame, self._position_capture_ids,
                                               self._position_animation_regions)
            reprobing = False
            if next_point is None:
                self._sleep(.08, deadline)
                next_frame = self.capture(deadline, quality)
                next_point = self._pointer_between(frame, next_frame, self._position_capture_ids,
                                                   self._position_animation_regions)
            if next_point is None:
                if reacquired >= 1:
                    raise GestureError("Pointer location became uncertain; no click or drag was sent")
                # Only unpressed probes are repeated. No tap/drag is replayed.
                next_point, next_frame = self.locate(next_frame, deadline, quality)
                reacquired += 1
                reprobing = True
            if (not reprobing and dx and abs(next_point[0] - point[0]) > 2
                    and (next_point[0] - point[0]) * dx > 0):
                gain_x = max(.35, min(8, abs((next_point[0] - point[0]) / dx)))
            if (not reprobing and dy and abs(next_point[1] - point[1]) > 2
                    and (next_point[1] - point[1]) * dy > 0):
                gain_y = max(.35, min(8, abs((next_point[1] - point[1]) / dy)))
            self._gains = (gain_x, gain_y)
            point, frame = next_point, next_frame
        raise GestureError("Pointer did not reach the target safely; no click or drag was sent")

    def drag(self, delta_y, duration_ms, deadline):
        started = time.monotonic()
        try:
            return self._drag(delta_y, duration_ms, deadline)
        finally:
            self._timing("drag", started)

    def _drag(self, delta_y, duration_ms, deadline):
        channel = self.connection()
        self._check(deadline)
        if deadline - time.monotonic() < duration_ms / 1000 + 1:
            raise TimeoutError("Insufficient time to complete phone gesture")
        started = False
        release_ok = False
        try:
            # Mark uncertain BEFORE writing: a send failure can occur after partial delivery.
            started = True
            self.buttons_may_be_held = True
            channel.relative(1)
            self._sleep(.06, deadline)
            steps = max(10, round(duration_ms / 35))
            for index in range(steps):
                self._check(deadline)
                # Ease both ends. In particular, the last real pointer reports
                # decelerate, instead of lifting a finger at full scroll speed.
                before, after = index / steps, (index + 1) / steps
                before = before * before * (3 - 2 * before)
                after = after * after * (3 - 2 * after)
                step = round(delta_y * after) - round(delta_y * before)
                channel.relative(1, 0, step, 0)
                self._sleep(duration_ms / steps / 1000, deadline)
            # Stop while still held, so iOS does not treat release as a fast fling.
            channel.relative(1, 0, 0, 0)
            self._sleep(.18, deadline)
        finally:
            if started:
                try:
                    channel.release()
                    release_ok = True
                finally:
                    self.buttons_may_be_held = not release_ok

    def scroll_one(self, frame, direction, distance, duration_ms, deadline, quality, prepare_cursor=False):
        diagnostics_before = self._diagnostic_snapshot()
        initial_frame = frame
        left, top, right, bottom = self._bounds(frame)
        height = bottom - top
        target = (round(left + (right - left) * .72),
                  round(top + height * (.25 if direction == "up" else .75)))
        self._allow_localized_position = True
        try:
            unused, frame = self.position(frame, target, deadline, quality, tolerance=55)
        finally:
            self._allow_localized_position = False
            self._position_animation_regions = None
        before = frame
        # Smooth per-report movement; exact content displacement is measured below.
        delta = min(360, round(distance * .85))
        self.drag(delta if direction == "up" else -delta, duration_ms, deadline)
        self._animation_context = None  # Old screen coordinates never survive a scroll.
        self._sleep(.22, deadline)
        prepare_cursor = prepare_cursor and not initial_frame["metadata"].get("localized_motion")
        after = (self.capture_stable(deadline, quality, cursor_probe=True, allow_localized=True) if prepare_cursor
                 else self.capture_stable(deadline, quality, allow_localized=True))
        # Report the whole operation's visible displacement. Positioning may
        # coincide with residual UI motion, so the later drag baseline alone
        # must not be mistaken for the user's initial-to-final page movement.
        comparison_started = time.monotonic()
        comparison = {
            **measure_scroll(initial_frame["raw"], after["raw"]),
            "drag_comparison": measure_scroll(before["raw"], after["raw"]),
            "position_comparison": measure_scroll(initial_frame["raw"], before["raw"]),
        }
        self._timing("compare", comparison_started)
        comparison["diagnostics"] = self._diagnostic_delta(diagnostics_before)
        return after, comparison

    @staticmethod
    def scroll_status(frame, comparison, direction):
        metadata = frame["metadata"]
        stable = (metadata.get("frame_stable") or
                  (metadata.get("text_stable") and metadata.get("localized_motion")
                   and metadata.get("mask_validated") and metadata.get("fresh_capture")))
        if not stable:
            return "frame_unstable", "The page is still changing; inspect a stable observation before another input"
        if not comparison.get("matched"):
            return "scroll_unverified", "Page overlap could not be verified; later pages were not scrolled"
        shift = comparison.get("content_shift_y", 0)
        if not shift:
            return "no_visual_change", "The page did not visibly scroll; do not repeat blindly"
        if (shift > 0) != (direction == "up") or comparison.get("overlap_fraction", 0) < .35:
            return "unexpected_scroll", "Scroll direction or overlap was unexpected; later pages were not scrolled"
        return "completed", None

    def run(self, arguments, deadline):
        self._cursor_hint = None  # Cursor hints never survive between requests.
        self._animation_context = None
        self._position_animation_regions = None
        self._allow_localized_position = False
        self._reset_diagnostics()
        started = time.monotonic()
        action, quality = arguments["action"], arguments.get("quality", 85)
        summary = {"ok": False, "action": action, "ui_verified": False,
                   "outcome": "action_failed", "pages": [], "buttons_may_be_held": False}
        images, frame = [], None
        try:
            summary["outcome"] = "completed"
            if action == "release":
                self.connection().release()
                self.buttons_may_be_held = False
            else:
                frame = (self.capture_stable(deadline, quality, allow_localized=True) if action == "observe"
                         else self.capture(deadline, quality))
                if action == "tap":
                    if "x" not in arguments or "y" not in arguments:
                        raise ValueError("Tap requires both x and y")
                    left, top, right, bottom = self._bounds(frame)
                    target = (round(left + (right - left - 1) * arguments["x"] / 1000),
                              round(top + (bottom - top - 1) * arguments["y"] / 1000))
                    unused, frame = self.position(frame, target, deadline, quality)
                    channel = self.connection()
                    self._check(deadline)
                    if deadline - time.monotonic() < .5:
                        raise TimeoutError("Insufficient time to complete phone tap")
                    release_ok = False
                    try:
                        self.buttons_may_be_held = True
                        channel.relative(1)
                        self._sleep(.055, deadline)
                    finally:
                        try:
                            channel.release()
                            release_ok = True
                        finally:
                            self.buttons_may_be_held = not release_ok
                    self._sleep(.4, deadline)
                    frame = self.capture_stable(deadline, quality)
                elif action == "scroll":
                    if arguments.get("direction") not in ("up", "down"):
                        raise ValueError("Scroll requires direction up or down")
                    for index in range(arguments.get("pages", 1)):
                        frame, comparison = self.scroll_one(
                            frame, arguments["direction"], arguments.get("distance", 400),
                            arguments.get("duration_ms", 800), deadline, quality,
                            prepare_cursor=index + 1 < arguments.get("pages", 1))
                        summary["pages"].append({"index": index + 1, **comparison,
                                                 "screenshot": frame["metadata"]})
                        images.extend([{"type": "text", "text": "Fresh phone page %d" % (index + 1)}, frame["image"]])
                        outcome, error = self.scroll_status(frame, comparison, arguments["direction"])
                        if error:
                            summary["outcome"], summary["error"] = outcome, error
                            break
                elif action != "observe":
                    raise ValueError("Unsupported phone action")
            if action != "scroll":
                if frame is None:
                    frame = self.capture(deadline, quality)
                images.append(frame["image"])
                summary["screenshot"] = frame["metadata"]
            summary["ok"] = summary["outcome"] == "completed"
        except Exception as error:
            summary["error"] = str(error) if isinstance(error, (GestureError, ValueError, TimeoutError)) else "Phone action failed or may have partly taken effect; inspect the screen before proceeding"
            summary["outcome"] = "action_uncertain"
            # Capture a NEW frame where possible; never return an old frame as final.
            if time.monotonic() < deadline - .3:
                try:
                    frame = self.capture(deadline, quality)
                    images.append(frame["image"])
                    summary["screenshot"] = frame["metadata"]
                except Exception:
                    summary["screenshot_error"] = "Could not capture the final phone screen"
        finally:
            self._cursor_hint = None
            self._animation_context = None
            self._position_animation_regions = None
            self._allow_localized_position = False
            summary["diagnostics"] = self._diagnostic_delta()
        summary["buttons_may_be_held"] = self.buttons_may_be_held
        summary["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return summary, images
