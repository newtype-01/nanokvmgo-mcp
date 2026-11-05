"""Small, local-only helpers for portrait-phone NanoKVM screenshots.

Coordinates returned by this module always refer to the original image. Image
comparison is evidence of pixels changing, not evidence of a UI action succeeding.
"""

from __future__ import annotations

import io
import math
import operator
import statistics
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat


def _image(data: bytes) -> Image.Image:
    if not isinstance(data, bytes) or not data or len(data) > 24 * 1024 * 1024:
        raise ValueError("Expected bounded image bytes")
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 20_000_000:
                raise ValueError("Screenshot dimensions exceed the limit")
            return source.convert("RGB")
    except (OSError, SyntaxError) as error:
        raise ValueError("Screenshot could not be decoded") from error


def _phone_box(image: Image.Image) -> tuple[tuple[int, int, int, int], str]:
    """Find a portrait panel between two nearly black, full-height margins.

    Fail open to the full image when the geometry is ambiguous. In particular,
    this does not crop a landscape desktop down to a bright window or text blob.
    """
    width, height = image.size
    full = (0, 0, width, height)
    if 0.35 <= width / height <= 0.8:
        return full, "already_portrait"
    if width < height or height < 120:
        return full, "no_portrait_panel"
    nonblack = image.convert("L").point(lambda value: 255 if value > 16 else 0)
    # BOX is used only for measuring column occupancy, never for output pixels.
    occupancy = list(nonblack.resize((width, 1), Image.Resampling.BOX).tobytes())
    active = [index for index, value in enumerate(occupancy) if value >= 3]
    if not active:
        return full, "no_portrait_panel"
    left, right = active[0], active[-1] + 1
    panel_width = right - left
    if not (0.35 <= panel_width / height <= 0.8):
        return full, "ambiguous_geometry"
    if min(left, width - right) < width * 0.05:
        return full, "missing_black_margins"
    # A few isolated pixels in the margins must not affect an otherwise valid
    # panel, but a genuinely lit second display must prevent an automatic crop.
    padding = max(2, round(height * 0.002))
    return (max(0, left - padding), 0, min(width, right + padding), height), "black_margins"


def _decode_frame(data: bytes):
    image = _image(data)
    box, detection = _phone_box(image)
    return image, box, detection


@lru_cache(maxsize=3)
def _cached_frame(data: bytes):
    return _decode_frame(data)


def _frame(data: bytes):
    """Reuse immutable pixel analysis of identical JPEG bytes, not capture state.

    At most three frames no larger than4Mpixels/4MBencoded are retained. Larger
    valid images are analysed without caching their RGB allocation. No capture IDs, times,
    freshness decisions or upstream tool results are cached here. Callers must
    treat the private returned RGB image as immutable.
    """
    if not isinstance(data, bytes) or not data or len(data) > 24 * 1024 * 1024:
        raise ValueError("Expected bounded image bytes")
    cacheable = False
    if len(data) <= 4_000_000:
        try:
            with Image.open(io.BytesIO(data)) as header:
                cacheable = header.width * header.height <= 4_000_000
        except (OSError, SyntaxError) as error:
            raise ValueError("Screenshot could not be decoded") from error
    return _cached_frame(data) if cacheable else _decode_frame(data)


def crop_phone(jpeg_bytes: bytes) -> tuple[bytes, dict]:
    """Return a JPEG at quality85 and crop metadata, without resizing pixels."""
    image, box, detection = _frame(jpeg_bytes)
    cropped = image.crop(box)
    output = io.BytesIO()
    cropped.save(output, format="JPEG", quality=85, subsampling=0)
    return output.getvalue(), {
        "source_width": image.width,
        "source_height": image.height,
        "crop_box": list(box),
        "origin": [box[0], box[1]],
        "width": cropped.width,
        "height": cropped.height,
        "cropped": box != (0, 0, image.width, image.height),
        "detection": detection,
        "coordinate_space": "original_image",
    }


def _components(mask: Image.Image):
    """Yield binary eight-connected components by scanning horizontal runs.

    bytearray.find skips zero spans in C; neighbouring-row runs replace Python
    work for every one of the up-to-million background/foreground pixels.
    """
    width, height = mask.size
    pixels = bytearray(mask.tobytes())
    start = pixels.find(b"\xff")
    while start >= 0:
        pending = [start]
        component = []
        left, right = start % width, start % width + 1
        top = bottom = start // width
        while pending:
            current = pending.pop()
            if not pixels[current]:
                continue
            x, y = current % width, current // width
            row = y * width
            previous_zero = pixels.rfind(b"\x00", row, current)
            first = previous_zero + 1 if previous_zero >= 0 else row
            last = pixels.find(b"\x00", current, row + width)
            if last < 0:
                last = row + width
            pixels[first:last] = b"\x00" * (last - first)
            component.extend(range(first, last))
            x0, x1 = first - row, last - row
            left, right = min(left, x0), max(right, x1)
            top, bottom = min(top, y), max(bottom, y)
            for neighbour_y in (y - 1, y + 1):
                if not 0 <= neighbour_y < height:
                    continue
                scan = neighbour_y * width + max(0, x0 - 1)
                stop = neighbour_y * width + min(width, x1 + 1)
                while scan < stop:
                    seed = pixels.find(b"\xff", scan, stop)
                    if seed < 0:
                        break
                    pending.append(seed)
                    end = pixels.find(b"\x00", seed, stop)
                    if end < 0:
                        break
                    scan = end + 1
        yield (left, top, right, bottom + 1), len(component), component
        start = pixels.find(b"\xff", start + 1)


def _circle_candidates(image: Image.Image, changed: Image.Image, phone_height: int):
    red, green, blue = image.split()
    spread = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(red, green), blue),
                                 ImageChops.darker(ImageChops.darker(red, green), blue))
    neutral = spread.point(lambda value: 255 if value <= 14 else 0)
    middle_grey = image.convert("L").point(lambda value: 255 if 48 <= value <= 205 else 0)
    mask = ImageChops.multiply(neutral, middle_grey)
    changed_pixels = changed.tobytes()
    minimum = max(6, round(phone_height * 0.008))
    maximum = max(minimum + 2, round(phone_height * 0.034))
    mask_width = mask.width
    primary = []
    for box, area, positions in _components(mask):
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        if left == 0 or top == 0 or right == mask.width or bottom == mask.height:
            continue  # A clipped component must not become a fabricated disk.
        if not (minimum <= width <= maximum and minimum <= height <= maximum):
            continue
        if not 0.78 <= width / height <= 1.28 or not 0.61 <= area / (width * height) <= 0.92:
            continue
        fresh = sum(bool(changed_pixels[position]) for position in positions)
        if fresh / area < 0.28:
            continue
        cx, cy = (left + right - 1) / 2, (top + bottom - 1) / 2
        radius_x, radius_y = width / 2, height / 2
        expected = set()
        for y in range(top, bottom):
            for x in range(left, right):
                if ((x - cx) / radius_x) ** 2 + ((y - cy) / radius_y) ** 2 <= 1:
                    expected.add(y * mask_width + x)
        actual = set(positions)
        overlap = len(actual & expected) / len(actual | expected)
        if overlap >= 0.77:
            primary.append((cx, cy))
    if len(primary) > 1:
        yield from primary
        return

    # A real pointer can touch a character, joining the entire word to the
    # connected component. Erosion cuts those narrow text strokes away before
    # looking for a compact solid core. The final fit uses the original pixels;
    # erosion alone never provides a pointer location.
    kernel = max(3, round(phone_height * 0.006))
    kernel += 1 - kernel % 2
    # Include dark glyphs only for fitting: iOS can render text over its grey
    # pointer. Those unchanged ink pixels are holes in the fresh difference,
    # rather than evidence that the circular pointer is absent.
    fit_grey = image.convert("L").point(lambda value: 255 if 20 <= value <= 205 else 0)
    fit_mask = ImageChops.multiply(neutral, fit_grey)
    eroded = fit_mask.filter(ImageFilter.MinFilter(kernel))
    raw_mask = fit_mask.tobytes()
    raw_grey = image.convert("L").tobytes()
    fitted = list(primary)
    for box, area, positions in _components(eroded):
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        if not (3 <= width <= maximum and 3 <= height <= maximum):
            continue
        if not 0.55 <= width / height <= 1.8:
            continue
        if sum(bool(changed_pixels[position]) for position in positions) / area < 0.28:
            continue
        center = ((left + right - 1) / 2, (top + bottom - 1) / 2)
        match = _fit_circle(center, raw_mask, raw_grey, changed_pixels,
                            image.size, minimum, maximum)
        if match and all(math.dist(match, previous) > 3 for previous in fitted):
            fitted.append(match)
    yield from fitted


def _fit_circle(center, neutral_pixels, grey_pixels, changed_pixels, size, minimum, maximum):
    """Fit a small filled disk; tolerate attached thin glyphs, not arbitrary text."""
    width, height = size
    base_x, base_y = map(round, center)
    best = None
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            cx, cy = base_x + dx, base_y + dy
            rays = []
            for index in range(16):
                angle = index * math.pi / 8
                distance = 0
                for distance in range(round(maximum / 2) + 3):
                    x = round(cx + math.cos(angle) * distance)
                    y = round(cy + math.sin(angle) * distance)
                    if not (0 <= x < width and 0 <= y < height) or not neutral_pixels[y * width + x]:
                        break
                rays.append(distance)
            radius = round(statistics.median(rays))
            if not minimum / 2 <= radius <= maximum / 2:
                continue
            left, right = cx - radius, cx + radius + 1
            top, bottom = cy - radius, cy + radius + 1
            if left < 0 or top < 0 or right > width or bottom > height:
                continue
            inside = outside = actual_inside = actual_outside = fresh = 0
            histogram = [0] * 256
            for y in range(top, bottom):
                for x in range(left, right):
                    position = y * width + x
                    if (x - cx) ** 2 + (y - cy) ** 2 <= radius ** 2:
                        inside += 1
                        actual_inside += bool(neutral_pixels[position])
                        if changed_pixels[position]:
                            fresh += 1
                            histogram[grey_pixels[position]] += 1
                    else:
                        outside += 1
                        actual_outside += bool(neutral_pixels[position])
            coverage = actual_inside / inside
            corner_fill = actual_outside / max(1, outside)
            uniform = max(sum(histogram[start:start + 21]) for start in range(236)) / max(1, fresh)
            # A pointer is a mostly uniform filled disk with unfilled corners.
            # A wide text stroke, ring icon, square and greyscale photograph do
            # not meet all three conditions, even if their bounding boxes fit.
            if coverage < 0.86 or corner_fill > 0.35 or fresh / inside < 0.28 or uniform < 0.65:
                continue
            score = coverage - corner_fill + uniform
            if best is None or score > best[0]:
                best = (score, (cx, cy))
    return best[1] if best else None


def _difference_disks(magnitude: Image.Image, phone_height: int):
    """Small circular components of directional change, repairing ink holes."""
    minimum = max(6, round(phone_height * 0.008))
    maximum = max(minimum + 2, round(phone_height * 0.034))
    kernel = max(3, round(phone_height * 0.003))
    kernel += 1 - kernel % 2
    binary = magnitude.point(lambda value: 255 if value >= 12 else 0)
    closed = binary.filter(ImageFilter.MaxFilter(kernel)).filter(ImageFilter.MinFilter(kernel))
    for box, area, positions in _components(closed):
        left, top, right, bottom = box
        width, height = right - left, bottom - top
        if left == 0 or top == 0 or right == closed.width or bottom == closed.height:
            continue
        if not (minimum <= width <= maximum and minimum <= height <= maximum):
            continue
        if not 0.75 <= width / height <= 1.33 or not 0.60 <= area / (width * height) <= 0.92:
            continue
        cx, cy = (left + right - 1) / 2, (top + bottom - 1) / 2
        expected = set()
        for y in range(top, bottom):
            for x in range(left, right):
                if ((x - cx) / (width / 2)) ** 2 + ((y - cy) / (height / 2)) ** 2 <= 1:
                    expected.add(y * closed.width + x)
        actual = set(positions)
        iou = len(actual & expected) / len(actual | expected)
        if iou >= 0.75:
            yield {"center": (cx, cy), "diameter": (width + height) / 2, "iou": iou}


def _paired_dark_pointer(before: Image.Image, after: Image.Image, phone_height: int):
    """Locate a tinted pointer using new darkening plus its old lightening disk.

    The RGB-neutral rule is not relaxed. This fallback needs both directional
    changes to be circular, similarly sized and plausibly separated. An isolated
    new avatar, text or circular icon is therefore insufficient evidence.
    """
    dark = ImageChops.subtract(before, after).convert("L")
    light = ImageChops.subtract(after, before).convert("L")
    new_disks = list(_difference_disks(dark, phone_height))
    old_disks = list(_difference_disks(light, phone_height))
    paired = []
    for new in new_disks:
        for old in old_disks:
            ratio = new["diameter"] / old["diameter"]
            distance = math.dist(new["center"], old["center"])
            if 0.70 <= ratio <= 1.43 and new["diameter"] * 0.20 < distance <= phone_height * 0.35:
                point = new["center"]
                if all(math.dist(point, previous) > 3 for previous in paired):
                    paired.append(point)
                break
    return paired


def find_pointer(before_jpeg: bytes, after_jpeg: bytes, ignore_regions=None) -> tuple[int, int] | None:
    """Find the newly visible small grey pointer after a relative-mouse probe.

    This intentionally returns None for ambiguous results, substantial scrolling,
    large AssistiveTouch circles, status/notification bands, or text-like shapes.
    It is not a general cursor detector and must not be used after app navigation.
    """
    before, unused_box, unused_detection = _frame(before_jpeg)
    after, box, unused_detection = _frame(after_jpeg)
    if before.size != after.size:
        return None
    left, top, right, bottom = box
    height = bottom - top
    # Keep the app's real content just below its navigation bar detectable.
    # A newly appearing broad notification is excluded dynamically instead of
    # permanently masking the upper17% and stranding the cursor there.
    band = (left, top, right, top + round(height * .22))
    top_difference = ImageChops.difference(before.crop(band), after.crop(band)).convert("L")
    top_changed = top_difference.point(lambda value: 255 if value >= 14 else 0).getbbox()
    body_top = .12
    if (top_changed and top_changed[2] - top_changed[0] > (right - left) * .6
            and top_changed[3] - top_changed[1] > height * .035):
        body_top = .22
    body = (left, top + round((bottom - top) * body_top), right,
            top + round((bottom - top) * 0.94))
    before_body, after_body = before.crop(body), after.crop(body)
    difference = ImageChops.difference(before_body, after_body).convert("L")
    changed = difference.point(lambda value: 255 if value >= 14 else 0)
    changed_count = changed.histogram()[255]
    changed_fraction = changed_count / (changed.width * changed.height)
    minimum_evidence = max(16, round(after.height * 0.008) ** 2 // 4)
    if changed_count < minimum_evidence or changed_fraction > 0.10:
        return None
    exclusions = []
    if ignore_regions:
        if not isinstance(ignore_regions, (list, tuple)) or len(ignore_regions) > 4:
            return None
        for region in ignore_regions:
            if (not isinstance(region, (list, tuple)) or len(region) != 4
                    or any(type(value) is not int for value in region)):
                return None
            x0, y0, x1, y1 = region
            if not (0 <= x0 < x1 <= after.width and 0 <= y0 < y1 <= after.height):
                return None
            exclusions.append((x0, y0, x1, y1))
        exclusion_area = sum((r - l) * (b - t) for l, t, r, b in exclusions)
        if not _animation_area_allowed(exclusion_area, changed.width * changed.height,
                                       (right - left) * (bottom - top)):
            return None
        maximum_radius = max(5, math.ceil(after.height * .017) + 2)
        for source_box in exclusions:
            # A pointer-like disk touching a proposed mask remains ambiguous;
            # do not discard it and silently choose a second candidate outside.
            patch = (max(body[0], source_box[0] - maximum_radius * 2),
                     max(body[1], source_box[1] - maximum_radius * 2),
                     min(body[2], source_box[2] + maximum_radius * 2),
                     min(body[3], source_box[3] + maximum_radius * 2))
            if patch[0] >= patch[2] or patch[1] >= patch[3]:
                continue
            local = (patch[0] - body[0], patch[1] - body[1], patch[2] - body[0], patch[3] - body[1])
            for px, py in _circle_candidates(after_body.crop(local), changed.crop(local), after.height):
                px, py = px + patch[0], py + patch[1]
                if (source_box[0] - maximum_radius <= px <= source_box[2] + maximum_radius
                        and source_box[1] - maximum_radius <= py <= source_box[3] + maximum_radius):
                    return None
            masked = (max(0, source_box[0] - body[0]), max(0, source_box[1] - body[1]),
                      min(changed.width, source_box[2] - body[0]), min(changed.height, source_box[3] - body[1]))
            for image in (before_body, after_body):
                ImageDraw.Draw(image).rectangle((masked[0], masked[1], masked[2] - 1, masked[3] - 1),
                                               fill=(0, 0, 0))
        difference = ImageChops.difference(before_body, after_body).convert("L")
        changed = difference.point(lambda value: 255 if value >= 14 else 0)
        if changed.histogram()[255] < minimum_evidence:
            return None
    # All possible new/old-pointer changes are inside this difference envelope.
    # Keep more than a full maximum disk diameter around it, so the geometric
    # tests retain their context without scanning/filtering the entire chat.
    # The looser10threshold is only an analysis window; evidence thresholds and
    # global change/notification guards above remain unchanged.
    envelope = difference.point(lambda value: 255 if value >= 10 else 0).getbbox()
    margin = max(12, math.ceil(after.height * 0.045))
    region = (max(0, envelope[0] - margin), max(0, envelope[1] - margin),
              min(changed.width, envelope[2] + margin), min(changed.height, envelope[3] + margin))
    local_before, local_after = before_body.crop(region), after_body.crop(region)
    candidates = list(_circle_candidates(local_after, changed.crop(region), after.height))
    if not candidates:
        candidates = _paired_dark_pointer(local_before, local_after, after.height)
    if len(candidates) != 1:
        return None
    x, y = candidates[0]
    result = (round(body[0] + region[0] + x), round(body[1] + region[1] + y))
    maximum_radius = max(5, math.ceil(after.height * .017) + 2)
    if any(l - maximum_radius <= result[0] <= r + maximum_radius
           and t - maximum_radius <= result[1] <= b + maximum_radius for l, t, r, b in exclusions):
        return None
    return result


def _row_features(image: Image.Image, box: tuple[int, int, int, int]):
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    body = image.crop((left + round(width * 0.14), top + round(height * 0.13),
                       left + round(width * 0.83), top + round(height * 0.90))).convert("L")
    rows = min(256, body.height)
    reduced = body.resize((48, rows), Image.Resampling.BOX).filter(ImageFilter.FIND_EDGES)
    data = reduced.tobytes()
    features = [tuple(sum(data[y * 48 + x:y * 48 + x + 6]) / 6
                      for x in range(0, 48, 6)) for y in range(rows)]
    return features, body.height / rows


@lru_cache(maxsize=4)
def _cached_row_features(jpeg_bytes: bytes, box: tuple[int, int, int, int]):
    image, unused_box, unused_detection = _frame(jpeg_bytes)
    features, scale = _row_features(image, box)
    return tuple(features), scale


def frame_change(before_jpeg: bytes, after_jpeg: bytes) -> dict:
    """Measure visual change in a shared phone-body ROI on small thumbnails.

    Differences use0..255luminance units. The status/notification band, chat
    composer and right AssistiveTouch area are excluded. This is a stability
    signal only; identical stale frames are also visually stable.
    """
    before, first, unused_detection = _frame(before_jpeg)
    after, second, unused_detection = _frame(after_jpeg)
    failure = {"mean_difference": 255.0, "changed_fraction": 1.0, "comparable": False}
    if before.size != after.size:
        return failure
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    width, height = right - left, bottom - top
    if width < 20 or height < 40:
        return failure
    body = (left + round(width * 0.14), top + round(height * 0.17),
            left + round(width * 0.83), top + round(height * 0.90))
    target = (min(128, body[2] - body[0]), min(256, body[3] - body[1]))
    first_body = before.crop(body).convert("L").resize(target, Image.Resampling.BOX)
    second_body = after.crop(body).convert("L").resize(target, Image.Resampling.BOX)
    difference = ImageChops.difference(first_body, second_body)
    changed = difference.point(lambda value: 255 if value >= 12 else 0)
    return {"mean_difference": round(ImageStat.Stat(difference).mean[0], 4),
            "changed_fraction": round(changed.histogram()[255] / (target[0] * target[1]), 6),
            "comparable": True}


def _stationary_tiles(bodies, exclusions):
    """Verify distributed native-pixel edges uniquely favour zero translation."""
    width, height = bodies[0].size
    allowed = Image.new("L", (width, height), 0)
    ImageDraw.Draw(allowed).rectangle((4, 4, width - 5, height - 5), fill=255)
    for left, top, right, bottom in exclusions:
        ImageDraw.Draw(allowed).rectangle((left - 4, top - 4, right + 3, bottom + 3), fill=0)
    edges = bodies[0].filter(ImageFilter.FIND_EDGES).point(lambda value: 255 if value >= 24 else 0)
    evidence = ImageChops.multiply(edges, allowed)
    trusted = []
    for row in range(4):
        for column in range(3):
            box = (round(width * column / 3), round(height * row / 4),
                   round(width * (column + 1) / 3), round(height * (row + 1) / 4))
            mask = evidence.crop(box)
            edge_count = mask.histogram()[255]
            if edge_count < max(32, mask.width * mask.height * .006):
                continue
            reference = bodies[0].crop(box)
            worst_zero = 0
            for following in bodies[1:]:
                scores = {}
                for dy in range(-2, 3):
                    for dx in range(-1, 2):
                        shifted = following.crop((box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy))
                        difference = ImageChops.difference(reference, shifted)
                        scores[(dx, dy)] = ImageStat.Stat(difference, mask).mean[0]
                zero = scores.pop((0, 0))
                if zero > 2.5 or zero + .35 >= min(scores.values()):
                    break
                worst_zero = max(worst_zero, zero)
            else:
                trusted.append({"row": row, "column": column, "edges": edge_count,
                                "zero_error": round(worst_zero, 3)})
    return trusted


def _animation_area_allowed(mask_pixels, body_pixels, phone_pixels):
    return mask_pixels <= body_pixels * .12 and mask_pixels <= phone_pixels * .06


def _fixed_region_border(bodies, region):
    """Require a stationary rectangular media boundary around an ignored patch."""
    left, top, right, bottom = region
    width, height = bodies[0].size
    anchors = []
    for axis, side in (("x", left), ("x", right), ("y", top), ("y", bottom)):
        line_start, line_end = (top + 3, bottom - 3) if axis == "x" else (left + 3, right - 3)
        if line_end - line_start < 12:
            return []
        options = []
        for position in range(side - 12, side + 13):
            if not 1 <= position < (width - 1 if axis == "x" else height - 1):
                continue
            coverages = []
            for body in bodies:
                if axis == "x":
                    before = body.crop((position - 1, line_start, position, line_end))
                    after = body.crop((position, line_start, position + 1, line_end))
                else:
                    before = body.crop((line_start, position - 1, line_end, position))
                    after = body.crop((line_start, position, line_end, position + 1))
                edge = ImageChops.difference(before, after).point(lambda value: 255 if value >= 18 else 0)
                coverages.append(edge.histogram()[255] / (line_end - line_start))
            if min(coverages) >= .45 and coverages[0] >= .60:
                options.append((min(coverages), position))
        if not options:
            return []
        coverage, position = max(options)
        anchors.append({"axis": axis, "position": position, "coverage": round(coverage, 3)})
    return anchors


def assess_stability(frames) -> dict:
    """Assess3..5 no-input observations, optionally validating small GIF motion.

    Each frame is the controller's {raw:bytes,metadata:{captureId:str}}. Only
    fresh distinct capture IDs plus persistent localized change and distributed
    stationary native-pixel edges can authorize an animation exclusion. The
    caller must bind exclusions to this latest capture and discard them after
    navigation or a gesture. This function cannot attest that no input occurred.
    """
    evidence = {"reason": "invalid_frames", "zero_shift_verified": False}
    result = {"stable": False, "comparable": False, "localized_motion": False,
              "ignored_regions": [], "evidence": evidence}
    if not isinstance(frames, (list, tuple)) or not 3 <= len(frames) <= 5:
        return result
    raw, identifiers = [], []
    for frame in frames:
        if not isinstance(frame, dict) or not isinstance(frame.get("raw"), bytes):
            return result
        metadata = frame.get("metadata") or {}
        identifier = metadata.get("captureId") if isinstance(metadata, dict) else None
        if not isinstance(identifier, str) or not identifier:
            evidence["reason"] = "missing_capture_id"
            return result
        raw.append(frame["raw"])
        identifiers.append(identifier)
    evidence.update({"capture_ids": identifiers, "latest_capture_id": identifiers[-1],
                     "frame_count": len(frames)})
    if len(set(identifiers)) != len(identifiers):
        evidence["reason"] = "repeated_capture_id"
        return result
    if len(set(raw)) == 1:
        evidence["reason"] = "repeated_pixel_frames"
        return result
    dimensions, boxes = [], []
    for data in raw:
        image, box, unused = _frame(data)
        dimensions.append(image.size)
        boxes.append(box)
    if len(set(dimensions)) != 1:
        evidence["reason"] = "different_dimensions"
        return result
    left, top = max(box[0] for box in boxes), max(box[1] for box in boxes)
    right, bottom = min(box[2] for box in boxes), min(box[3] for box in boxes)
    width, height = right - left, bottom - top
    if width < 150 or height < 300 or not .35 <= width / height <= .8:
        evidence["reason"] = "invalid_phone_region"
        return result
    body_box = (left + round(width * .14), top + round(height * .17),
                left + round(width * .83), top + round(height * .90))
    bodies = [_frame(data)[0].crop(body_box).convert("L") for data in raw]
    result["comparable"] = True
    evidence.update({"source_size": list(dimensions[0]), "phone_box": [left, top, right, bottom],
                     "body_roi": list(body_box)})
    changes = [frame_change(first, second) for first, second in zip(raw, raw[1:])]
    evidence["pair_changes"] = changes
    if all(change["comparable"] and change["mean_difference"] < 1.3
           and change["changed_fraction"] < .015 for change in changes):
        result["stable"] = True
        evidence["reason"] = "globally_stable"
        return result
    size = (128, 256)
    thumbnails = [body.resize(size, Image.Resampling.BOX) for body in bodies]
    masks = [ImageChops.difference(first, second).point(lambda value: 255 if value >= 14 else 0)
             for first, second in zip(thumbnails, thumbnails[1:])]
    union = masks[0]
    for mask in masks[1:]:
        union = ImageChops.lighter(union, mask)
    connected = union.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.MinFilter(3))
    regions = []
    for box, area, unused in _components(connected):
        if area < 12:
            continue
        l, t, r, b = box
        regions.append((max(0, l - 1), max(0, t - 1), min(size[0], r + 1), min(size[1], b + 1)))
    if not regions or len(regions) > 4:
        evidence["reason"] = "motion_not_localized"
        return result
    mask_fraction = sum((r - l) * (b - t) for l, t, r, b in regions) / (size[0] * size[1])
    evidence["mask_fraction"] = round(mask_fraction, 5)
    if mask_fraction > .12:
        evidence["reason"] = "motion_region_too_large"
        return result
    for region in regions:
        minimum = max(4, (region[2] - region[0]) * (region[3] - region[1]) * .03)
        if any(mask.crop(region).histogram()[255] < minimum for mask in masks):
            evidence["reason"] = "motion_not_persistent"
            return result
    local_regions = [(math.floor(l * bodies[0].width / size[0]), math.floor(t * bodies[0].height / size[1]),
                      math.ceil(r * bodies[0].width / size[0]), math.ceil(b * bodies[0].height / size[1]))
                     for l, t, r, b in regions]
    mask_pixels = sum((r - l) * (b - t) for l, t, r, b in local_regions)
    evidence["phone_mask_fraction"] = round(mask_pixels / (width * height), 5)
    if not _animation_area_allowed(mask_pixels, bodies[0].width * bodies[0].height, width * height):
        evidence["reason"] = "motion_region_too_large"
        return result
    anchors = [_fixed_region_border(bodies, region) for region in local_regions]
    evidence["media_border_anchors"] = anchors
    if any(len(border) != 4 for border in anchors):
        evidence["reason"] = "media_boundary_not_stationary"
        return result
    static = _stationary_tiles(bodies, local_regions)
    evidence["static_tiles"] = static
    rows, columns = {tile["row"] for tile in static}, {tile["column"] for tile in static}
    if len(static) < 6 or len(rows) < 3 or len(columns) < 2:
        evidence["reason"] = "insufficient_stationary_edges"
        return result
    result.update({"stable": True, "localized_motion": True,
                   "ignored_regions": [[body_box[0] + l, body_box[1] + t, body_box[0] + r, body_box[1] + b]
                                       for l, t, r, b in local_regions]})
    evidence.update({"reason": "localized_animation_with_stationary_text", "zero_shift_verified": True})
    return result


def measure_scroll(before_jpeg: bytes, after_jpeg: bytes) -> dict:
    """Estimate signed content displacement and overlap in the chat body.

    Positive content_shift_y means image content moved down in original pixels.
    No UI/navigation meaning is inferred from that direction. A low-confidence
    match is returned as matched:false instead of guessing a page distance.
    """
    before, before_box, unused_detection = _frame(before_jpeg)
    after, after_box, unused_detection = _frame(after_jpeg)
    failed = {"matched": False, "content_shift_y": None,
              "overlap_fraction": None, "confidence": 0.0}
    if before.size != after.size:
        return {**failed, "reason": "different_dimensions"}
    tolerance = max(4, round((after_box[2] - after_box[0]) * 0.02))
    if max(abs(a - b) for a, b in zip(before_box, after_box)) > tolerance:
        return {**failed, "reason": "different_phone_regions"}
    # JPEG ringing can move a detected black edge by a few pixels. Analyse the
    # same union region for both images so such compression noise is harmless.
    common_box = (min(before_box[0], after_box[0]), min(before_box[1], after_box[1]),
                  max(before_box[2], after_box[2]), max(before_box[3], after_box[3]))
    earlier, scale = _cached_row_features(before_jpeg, common_box)
    later, _ = _cached_row_features(after_jpeg, common_box)
    rows = len(earlier)
    if rows < 20:
        return {**failed, "reason": "insufficient_rows"}
    mean_energy = sum(sum(row) for row in earlier[2:-2]) / ((rows - 4) * 8)
    if mean_energy < 1.3:
        return {**failed, "reason": "insufficient_texture"}
    scores = []
    earlier_flat = tuple(value for row in earlier for value in row)
    later_flat = tuple(value for row in later for value in row)
    for shift in range(-round(rows * 0.55), round(rows * 0.55) + 1):
        start, end = max(2, 2 - shift), min(rows - 2, rows - 2 - shift)
        count = end - start
        # Same absolute-error score, with flattened C-level iterators instead
        # of half-a-million nested Python generator callbacks per comparison.
        error = sum(map(abs, map(operator.sub, earlier_flat[start * 8:end * 8],
                                 later_flat[(start + shift) * 8:(end + shift) * 8]))) / (count * 8)
        scores.append((error, shift, count))
    scores.sort()
    best, shift, count = scores[0]
    zero = next(error for error, candidate_shift, _ in scores if candidate_shift == 0)
    alternatives = [error for error, candidate_shift, _ in scores if abs(candidate_shift - shift) >= 4]
    rival = min(alternatives) if alternatives else best
    separation = max(0.0, (rival - best) / max(rival, 1))
    if shift == 0 and best <= 1.2:
        confidence = max(0.5, separation)
    else:
        improvement = max(0.0, (zero - best) / max(zero, 1))
        confidence = min(separation, improvement)
    # Edge magnitude varies with the density of text. Compare the residual to
    # that signal rather than applying one threshold to sparse and dense pages.
    if confidence < 0.35 or best > max(3, min(18, mean_energy * 0.40)):
        return {**failed, "confidence": round(confidence, 3), "reason": "ambiguous_alignment"}
    return {"matched": True, "content_shift_y": round(shift * scale),
            "overlap_fraction": round(count / (rows - 4), 3),
            "confidence": round(confidence, 3), "reason": "vertical_alignment"}
