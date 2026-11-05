import io
import random
import unittest

from PIL import Image, ImageDraw

from nanokvmgo_mcp import phone_vision as vision
from nanokvmgo_mcp.phone_vision import assess_stability, crop_phone, find_pointer, frame_change, measure_scroll


def jpeg(image):
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=96, subsampling=0)
    return output.getvalue()


def frame():
    image = Image.new("RGB", (1000, 600), "black")
    ImageDraw.Draw(image).rectangle((350, 0, 649, 599), fill=(232, 232, 232))
    return image


def pointer(image, center, radius=8):
    x, y = center
    ImageDraw.Draw(image).ellipse((x - radius, y - radius, x + radius, y + radius), fill=(135, 135, 135))


def glyph_strokes(image, center):
    x, y = center
    draw = ImageDraw.Draw(image)
    draw.line((x - 17, y, x + 17, y), fill=(30, 30, 30), width=2)
    draw.line((x - 3, y - 15, x - 3, y + 15), fill=(30, 30, 30), width=2)


def translucent_pointer(image, center, radius=8):
    overlay = Image.new("RGBA", image.size)
    x, y = center
    ImageDraw.Draw(overlay).ellipse((x - radius, y - radius, x + radius, y + radius), fill=(40, 40, 40, 100))
    image.paste(Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB"))


def green_frame():
    image = frame()
    ImageDraw.Draw(image).rectangle((480, 250, 620, 500), fill=(150, 235, 100))
    return image


def chat_content(seed=92):
    image = frame()
    draw = ImageDraw.Draw(image)
    rng = random.Random(seed)
    for y in range(50, 580, 33):
        draw.rounded_rectangle((390, y, 390 + rng.randrange(80, 220), y + 25), radius=4, fill=(248, 248, 248))
        for line in range(2):
            for x in range(400, 400 + rng.randrange(40, 150), 9):
                draw.rectangle((x, y + 4 + line * 9, x + 4, y + 8 + line * 9), fill=(45, 45, 45))
    return image


class CropTests(unittest.TestCase):
    def test_crops_black_margins_without_downsampling_and_maps_original_coordinates(self):
        data, meta = crop_phone(jpeg(frame()))
        result = Image.open(io.BytesIO(data))
        self.assertEqual(result.height, 600)
        self.assertTrue(300 <= result.width <= 310)
        self.assertEqual(meta["origin"][1], 0)
        self.assertTrue(346 <= meta["origin"][0] <= 350)
        self.assertEqual(meta["crop_box"][2] - meta["crop_box"][0], result.width)
        self.assertEqual(meta["source_width"], 1000)
        self.assertEqual(meta["detection"], "black_margins")

    def test_already_portrait_preserves_entire_dimensions(self):
        image = Image.new("RGB", (300, 600), (200, 200, 200))
        data, meta = crop_phone(jpeg(image))
        self.assertEqual(Image.open(io.BytesIO(data)).size, (300, 600))
        self.assertFalse(meta["cropped"])
        self.assertEqual(meta["origin"], [0, 0])

    def test_landscape_content_and_all_black_frames_are_not_guessed_as_phone(self):
        for image in [Image.new("RGB", (1000, 600), "black"),
                      Image.new("RGB", (1000, 600), (230, 230, 230))]:
            _, meta = crop_phone(jpeg(image))
            self.assertFalse(meta["cropped"])
            self.assertEqual(meta["width"], 1000)

    def test_invalid_bytes_are_rejected(self):
        with self.assertRaises(ValueError):
            crop_phone(b"not an image")


class PointerTests(unittest.TestCase):
    def test_moved_pointer_reports_new_center_in_original_image_coordinates(self):
        before, after = frame(), frame()
        pointer(before, (450, 320))
        pointer(after, (500, 340))
        actual = find_pointer(jpeg(before), jpeg(after))
        self.assertIsNotNone(actual)
        self.assertLessEqual(abs(actual[0] - 500), 1)
        self.assertLessEqual(abs(actual[1] - 340), 1)

    def test_newly_visible_pointer_can_be_found(self):
        before, after = frame(), frame()
        pointer(after, (520, 400))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (520, 400))

    def test_pointer_just_below_navigation_bar_is_detectable_without_a_notification(self):
        before, after = frame(), frame()
        pointer(before, (450, 200))
        # 14%height is below the navigation bar but above the old17%mask.
        pointer(after, (520, 84))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (520, 84))

    def test_dynamic_notification_mask_rejects_a_small_grey_avatar_inside_new_banner(self):
        before, after = frame(), frame()
        draw = ImageDraw.Draw(after)
        draw.rounded_rectangle((365, 45, 635, 120), radius=12, fill=(248, 248, 248))
        pointer(after, (400, 96))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_dynamic_notification_mask_preserves_a_real_pointer_below_the_banner(self):
        before, after = frame(), frame()
        draw = ImageDraw.Draw(after)
        draw.rounded_rectangle((365, 45, 635, 120), radius=12, fill=(248, 248, 248))
        pointer(after, (400, 96))
        pointer(after, (520, 350))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (520, 350))

    def test_pointer_is_found_on_a_dark_phone_background(self):
        before, after = frame(), frame()
        for image in (before, after):
            ImageDraw.Draw(image).rectangle((350, 0, 649, 599), fill=(30, 30, 30))
        pointer(before, (450, 320))
        pointer(after, (500, 340))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (500, 340))

    def test_pointer_behind_text_is_not_lost_when_its_grey_component_joins_glyphs(self):
        before, after = frame(), frame()
        glyph_strokes(before, (520, 400))
        pointer(after, (520, 400))
        glyph_strokes(after, (520, 400))
        detected = find_pointer(jpeg(before), jpeg(after))
        self.assertIsNotNone(detected)
        self.assertLessEqual(abs(detected[0] - 520), 2)
        self.assertLessEqual(abs(detected[1] - 400), 2)

    def test_local_list_reflow_under_ten_percent_can_coexist_with_pointer_probe(self):
        before, after = frame(), frame()
        ImageDraw.Draw(after).rectangle((370, 130, 459, 204), fill=(60, 60, 60))
        pointer(after, (520, 400))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (520, 400))

    def test_mixed_clean_circle_and_text_occluded_circle_are_ambiguous(self):
        before, after = frame(), frame()
        glyph_strokes(before, (520, 400))
        pointer(after, (450, 300))
        pointer(after, (520, 400))
        glyph_strokes(after, (520, 400))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_difference_window_keeps_a_distant_second_occluded_candidate(self):
        before, after = frame(), frame()
        glyph_strokes(before, (600, 510))
        pointer(after, (400, 170))
        pointer(after, (600, 510))
        glyph_strokes(after, (600, 510))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_local_rectangles_rings_and_ink_changes_without_a_disk_are_not_pointers(self):
        before, after = frame(), frame()
        draw = ImageDraw.Draw(after)
        draw.rectangle((410, 260, 425, 275), fill=(135, 135, 135))
        draw.ellipse((500, 350, 518, 368), outline=(135, 135, 135), width=3)
        glyph_strokes(after, (540, 430))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_tinted_pointer_at_a_coloured_bubble_edge_has_directional_pair_evidence(self):
        before, after = green_frame(), green_frame()
        translucent_pointer(before, (430, 350))
        translucent_pointer(after, (480, 350))
        detected = find_pointer(jpeg(before), jpeg(after))
        self.assertIsNotNone(detected)
        self.assertLessEqual(abs(detected[0] - 480), 1)
        self.assertLessEqual(abs(detected[1] - 350), 1)

    def test_pointer_can_move_entirely_over_coloured_background(self):
        before, after = green_frame(), green_frame()
        translucent_pointer(before, (510, 350))
        translucent_pointer(after, (550, 350))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (550, 350))

    def test_isolated_new_coloured_circle_does_not_count_as_a_pointer(self):
        before, after = green_frame(), green_frame()
        translucent_pointer(after, (550, 350))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_multiple_directionally_paired_tinted_disks_remain_ambiguous(self):
        before, after = green_frame(), green_frame()
        translucent_pointer(before, (510, 350))
        translucent_pointer(after, (550, 350))
        translucent_pointer(after, (550, 420))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_status_notification_large_assistive_touch_and_text_are_not_pointers(self):
        before, after = frame(), frame()
        draw = ImageDraw.Draw(after)
        pointer(after, (500, 35))
        draw.rounded_rectangle((365, 48, 635, 95), radius=13, fill=(170, 170, 170))
        pointer(after, (610, 330), radius=26)
        draw.text((430, 280), "NEW WORDS 123 OOO", fill=(135, 135, 135))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_notification_does_not_hide_a_unique_pointer_in_the_body(self):
        before, after = frame(), frame()
        ImageDraw.Draw(after).rounded_rectangle((365, 48, 635, 95), radius=13, fill=(170, 170, 170))
        pointer(after, (520, 400))
        self.assertEqual(find_pointer(jpeg(before), jpeg(after)), (520, 400))

    def test_multiple_new_circle_candidates_are_ambiguous(self):
        before, after = frame(), frame()
        pointer(after, (450, 300))
        pointer(after, (530, 400))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after)))

    def test_navigation_or_dimension_changes_prevent_pointer_guess(self):
        self.assertIsNone(find_pointer(jpeg(frame()), jpeg(chat_content())))
        self.assertIsNone(find_pointer(jpeg(frame()), jpeg(Image.new("RGB", (300, 600), "white"))))


class ScrollTests(unittest.TestCase):
    def test_vertical_translation_is_measured_with_sign_and_overlap(self):
        before = chat_content()
        after = frame()
        # Shift the underlying body down by60pixels while leaving the frame fixed.
        after.paste(before.crop((350, 70, 650, 540)), (350, 130))
        measured = measure_scroll(jpeg(before), jpeg(after))
        self.assertTrue(measured["matched"], measured)
        self.assertLessEqual(abs(measured["content_shift_y"] - 60), 3)
        self.assertTrue(0.7 < measured["overlap_fraction"] < 1)

    def test_unchanged_textured_frame_matches_zero(self):
        data = jpeg(chat_content())
        measured = measure_scroll(data, data)
        self.assertTrue(measured["matched"], measured)
        self.assertEqual(measured["content_shift_y"], 0)


class FrameChangeTests(unittest.TestCase):
    def test_identical_frames_have_zero_change(self):
        data = jpeg(chat_content())
        self.assertEqual(frame_change(data, data), {
            "mean_difference": 0.0, "changed_fraction": 0.0, "comparable": True})

    def test_clock_and_notification_changes_do_not_destabilize_chat_body(self):
        before, after = frame(), frame()
        ImageDraw.Draw(after).rectangle((360, 10, 640, 90), fill=(120, 120, 120))
        measured = frame_change(jpeg(before), jpeg(after))
        self.assertEqual(measured["mean_difference"], 0.0)
        self.assertEqual(measured["changed_fraction"], 0.0)

    def test_changed_message_body_is_measurably_unstable(self):
        before, after = frame(), frame()
        ImageDraw.Draw(after).rectangle((410, 200, 570, 400), fill=(90, 90, 90))
        measured = frame_change(jpeg(before), jpeg(after))
        self.assertGreater(measured["mean_difference"], 10)
        self.assertGreater(measured["changed_fraction"], 0.1)

    def test_different_dimensions_fail_closed_as_unstable(self):
        measured = frame_change(jpeg(frame()), jpeg(Image.new("RGB", (300, 600), "white")))
        self.assertFalse(measured["comparable"])
        self.assertEqual(measured["changed_fraction"], 1)

    def test_negative_translation_preserves_its_direction(self):
        before = chat_content()
        after = frame()
        after.paste(before.crop((350, 115, 650, 600)), (350, 70))
        measured = measure_scroll(jpeg(before), jpeg(after))
        self.assertTrue(measured["matched"], measured)
        self.assertLessEqual(abs(measured["content_shift_y"] + 45), 3)

    def test_blank_and_differently_sized_images_do_not_produce_scroll_claims(self):
        data = jpeg(frame())
        self.assertFalse(measure_scroll(data, data)["matched"])
        self.assertFalse(measure_scroll(data, jpeg(Image.new("RGB", (300, 600), "white")))["matched"])

    def test_tiny_black_edge_detection_drift_does_not_imply_scrolling(self):
        before = chat_content()
        after = before.copy()
        ImageDraw.Draw(after).rectangle((348, 0, 349, 599), fill=(28, 28, 28))
        measured = measure_scroll(jpeg(before), jpeg(after))
        self.assertTrue(measured["matched"], measured)
        self.assertEqual(measured["content_shift_y"], 0)


class ScrollSafetyTests(unittest.TestCase):
    def test_unrelated_chat_layouts_do_not_gain_matches_from_relaxed_residual_limit(self):
        before = jpeg(chat_content())
        for seed in range(200, 212):
            with self.subTest(seed=seed):
                result = measure_scroll(before, jpeg(chat_content(seed=seed)))
                self.assertFalse(result["matched"], result)

    def test_repeating_rows_with_multiple_equally_plausible_shifts_are_rejected(self):
        # At this height the analysed body has256rows, preserving the exact
        # periodic ambiguity rather than introducing resampling phase clues.
        before = Image.new("RGB", (1000, 332), "black")
        after = before.copy()
        for image, phase in ((before, 0), (after, 12)):
            draw = ImageDraw.Draw(image)
            draw.rectangle((417, 0, 582, 331), fill=(232, 232, 232))
            for y in range(phase - 24, 332, 24):
                draw.rectangle((430, y, 565, y + 4), fill=(60, 60, 60))
        result = measure_scroll(jpeg(before), jpeg(after))
        self.assertFalse(result["matched"], result)



class VisionPerformanceSafetyTests(unittest.TestCase):
    def test_scanline_components_match_reference_eight_connected_flood_fill(self):
        rng = random.Random(179)
        for density in (0.05, 0.3, 0.8, 1.0):
            for trial in range(12):
                width, height = rng.randrange(3, 13), rng.randrange(3, 10)
                active = {y * width + x for y in range(height) for x in range(width)
                          if rng.random() < density}
                image = Image.frombytes("L", (width, height),
                                        bytes(255 if index in active else 0 for index in range(width * height)))
                remaining = set(active)
                expected = set()
                while remaining:
                    seed = remaining.pop()
                    component, pending = {seed}, [seed]
                    while pending:
                        current = pending.pop()
                        x, y = current % width, current // width
                        for ny in range(max(0, y - 1), min(height, y + 2)):
                            for nx in range(max(0, x - 1), min(width, x + 2)):
                                neighbour = ny * width + nx
                                if neighbour in remaining:
                                    remaining.remove(neighbour)
                                    component.add(neighbour)
                                    pending.append(neighbour)
                    expected.add(frozenset(component))
                measured = list(vision._components(image))
                with self.subTest(density=density, trial=trial):
                    self.assertEqual({frozenset(positions) for _, _, positions in measured}, expected)
                    for box, area, positions in measured:
                        self.assertEqual(area, len(positions))
                        xs, ys = [index % width for index in positions], [index // width for index in positions]
                        self.assertEqual(box, (min(xs), min(ys), max(xs) + 1, max(ys) + 1))

    def test_cached_frames_do_not_leak_mutable_metadata_between_public_calls(self):
        data = jpeg(frame())
        first_image, first_meta = crop_phone(data)
        expected_box = first_meta["crop_box"][:]
        first_meta["crop_box"][0] = -100
        first_meta["origin"][0] = -100
        self.assertEqual(frame_change(data, data)["mean_difference"], 0)
        second_image, second_meta = crop_phone(data)
        self.assertEqual(second_image, first_image)
        self.assertEqual(second_meta["crop_box"], expected_box)
        self.assertGreaterEqual(second_meta["origin"][0], 0)

    def test_large_decoded_frames_are_not_retained_by_the_small_frame_cache(self):
        vision._cached_frame.cache_clear()
        try:
            crop_phone(jpeg(Image.new("RGB", (2100, 2000), "white")))
            self.assertEqual(vision._cached_frame.cache_info().currsize, 0)
        finally:
            vision._cached_frame.cache_clear()


def observations(images):
    return [{"raw": jpeg(image), "metadata": {"captureId": "fresh-%d" % index}}
            for index, image in enumerate(images)]


def animated_chat_frames(box=(550, 240, 590, 280), shifts=None):
    images = []
    for index, shade in enumerate((55, 115, 175)):
        image = chat_content()
        offset = shifts[index] if shifts else 0
        left, top, right, bottom = box
        ImageDraw.Draw(image).rectangle((left, top + offset, right, bottom + offset), fill=(shade, shade, shade))
        images.append(image)
    return images


class AnimationStabilityTests(unittest.TestCase):
    def test_three_fresh_local_gif_frames_need_distributed_stationary_edges(self):
        result = assess_stability(observations(animated_chat_frames()))
        self.assertTrue(result["stable"], result)
        self.assertTrue(result["localized_motion"])
        self.assertTrue(result["evidence"]["zero_shift_verified"])
        self.assertGreaterEqual(len(result["evidence"]["static_tiles"]), 6)
        self.assertEqual(result["evidence"]["latest_capture_id"], "fresh-2")
        self.assertTrue(all(350 <= box[0] < box[2] <= 650 for box in result["ignored_regions"]))

    def test_slow_whole_page_scroll_is_not_exempted_as_animation(self):
        original = chat_content()
        images = []
        for shift in (0, 1, 2):
            image = frame()
            image.paste(original.crop((350, 0, 650, 600 - shift)), (350, shift))
            images.append(image)
        self.assertFalse(assess_stability(observations(images))["stable"])

    def test_new_popup_and_new_message_insertion_are_rejected(self):
        original = chat_content()
        popup = original.copy()
        ImageDraw.Draw(popup).rounded_rectangle((410, 220, 570, 400), radius=12, fill=(110, 110, 110))
        self.assertFalse(assess_stability(observations([original, popup, popup]))["stable"])
        inserted = original.copy()
        inserted.paste(original.crop((350, 120, 650, 560)), (350, 160))
        self.assertFalse(assess_stability(observations([original, inserted, inserted]))["stable"])

    def test_large_animation_and_moving_media_box_are_rejected(self):
        self.assertFalse(assess_stability(observations(animated_chat_frames((430, 180, 590, 360))))["stable"])
        moving = assess_stability(observations(animated_chat_frames(shifts=(0, 8, 16))))
        self.assertFalse(moving["stable"], moving)

    def test_uniform_background_has_no_stationary_text_evidence(self):
        images = []
        for shade in (55, 115, 175):
            image = frame()
            ImageDraw.Draw(image).rectangle((550, 240, 590, 280), fill=(shade, shade, shade))
            images.append(image)
        result = assess_stability(observations(images))
        self.assertFalse(result["stable"], result)

    def test_duplicate_capture_ids_and_repeated_pixels_are_untrusted(self):
        frames = observations(animated_chat_frames())
        frames[2]["metadata"]["captureId"] = frames[1]["metadata"]["captureId"]
        self.assertFalse(assess_stability(frames)["stable"])
        self.assertFalse(assess_stability(observations([chat_content()] * 3))["stable"])
        self.assertFalse(assess_stability(observations(animated_chat_frames())[:2])["stable"])

    def test_area_policy_rejects_either_independent_limit(self):
        self.assertTrue(vision._animation_area_allowed(120, 1000, 2000))
        self.assertFalse(vision._animation_area_allowed(121, 1000, 2200))
        self.assertFalse(vision._animation_area_allowed(122, 1100, 2000))

    def test_pointer_inside_or_touching_verified_mask_is_not_a_click_basis(self):
        before, after = frame(), frame()
        pointer(before, (430, 350))
        pointer(after, (520, 350))
        for region in ((510, 340, 530, 360), (527, 340, 547, 360)):
            self.assertIsNone(find_pointer(jpeg(before), jpeg(after), ignore_regions=[region]))

    def test_masked_pointer_does_not_make_a_second_outside_circle_unambiguous(self):
        before, after = frame(), frame()
        pointer(after, (520, 350))
        pointer(after, (430, 250))
        self.assertIsNone(find_pointer(jpeg(before), jpeg(after), ignore_regions=[(510, 340, 530, 360)]))



if __name__ == "__main__":
    unittest.main()
