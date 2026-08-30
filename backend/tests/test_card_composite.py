import io
import unittest

from PIL import Image

from services.card_composite import (
    CELL_SIZE,
    GUTTER,
    HIGH_RESOLUTION_CELL_SIZE,
    LABEL_BOX,
    build_composite,
)


def _jpeg(color: str) -> bytes:
    image = Image.new("RGB", (250, 350), color)
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


class CardCompositeTests(unittest.TestCase):
    def test_two_cards_use_one_row(self):
        result = Image.open(io.BytesIO(build_composite([_jpeg("red"), _jpeg("blue")])))
        self.assertGreater(result.width, result.height)

    def test_three_cards_keep_a_two_by_two_canvas(self):
        result = Image.open(io.BytesIO(build_composite([
            _jpeg("red"), _jpeg("blue"), _jpeg("green"),
        ])))
        self.assertGreater(result.height, result.width)

    def test_high_resolution_setting_changes_only_the_composite_cell_cap(self):
        images = [_jpeg("red"), _jpeg("blue"), _jpeg("green"), _jpeg("yellow")]

        low_resolution = Image.open(io.BytesIO(build_composite(images, high_resolution=False)))
        high_resolution = Image.open(io.BytesIO(build_composite(images, high_resolution=True)))

        self.assertEqual(low_resolution.size, (1340, 1980))
        self.assertEqual(high_resolution.size, (2620, 3772))
        self.assertEqual(HIGH_RESOLUTION_CELL_SIZE, (1280, 1792))

    def test_position_label_uses_reserved_space_above_card(self):
        result = Image.open(io.BytesIO(build_composite([_jpeg("red"), _jpeg("blue")])))
        label_pixel = result.getpixel((GUTTER + 8, GUTTER + 8))
        card_pixel = result.getpixel((
            GUTTER + CELL_SIZE[0] // 2,
            GUTTER + LABEL_BOX + CELL_SIZE[1] // 2,
        ))

        self.assertLess(max(label_pixel), 40)
        self.assertGreater(card_pixel[0], 180)
        self.assertLess(card_pixel[1], 80)

    def test_rejects_single_or_oversized_groups(self):
        with self.assertRaises(ValueError):
            build_composite([_jpeg("red")])
        with self.assertRaises(ValueError):
            build_composite([_jpeg("red")] * 5)


if __name__ == "__main__":
    unittest.main()
