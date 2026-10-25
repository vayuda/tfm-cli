"""Per-map global parameters: maximums and track bonuses. Run: python3 -m unittest discover tests"""
import unittest

from tfm import data, digest, value
from test_value import view

AMAZONIS = "amazonis planitia"


def on_map(board, **kw):
    v = view(**kw)
    v["game"]["gameOptions"]["boardName"] = board
    return v


class Steps(unittest.TestCase):
    def test_standard_and_amazonis_totals(self):
        self.assertEqual([data.total_steps(p, "tharsis") for p in data.PARAMS], [19, 14, 9, 15])
        self.assertEqual([data.total_steps(p, AMAZONIS) for p in data.PARAMS], [22, 18, 11, 18])

    def test_venus_goes_up_in_one_percent_steps_above_30(self):
        self.assertEqual(data.steps_left("venus", 30, AMAZONIS), 3)
        self.assertEqual(data.param_level("venus", 16), 31)
        self.assertEqual(data.steps_left("venus", 30, "tharsis"), 0)

    def test_unknown_map_uses_standard(self):
        self.assertEqual(data.param_max("temperature", "some new map"), 8)


class Outlook(unittest.TestCase):
    def test_steps_left_follow_the_map(self):
        std = value.Outlook(on_map("tharsis", temperature=8, oxygen=14, oceans=9), gens_left=2)
        self.assertEqual(std.remaining_steps, 0)
        self.assertEqual(std.heat, 0.0)
        ama = value.Outlook(on_map(AMAZONIS, temperature=8, oxygen=14, oceans=9), gens_left=2)
        self.assertEqual(ama.left, {"temperature": 3, "oxygen": 4, "oceans": 2, "venus": 13})
        self.assertGreater(ama.heat, 0.0)  # heat still raises temperature past +8 on Amazonis

    def test_typical_length_scales_with_track_length(self):
        std = value.Outlook(on_map("tharsis", generation=1, temperature=-30, oxygen=0, oceans=0))
        ama = value.Outlook(on_map(AMAZONIS, generation=1, temperature=-30, oxygen=0, oceans=0))
        self.assertGreater(ama.by_length, std.by_length)


class TrackBonuses(unittest.TestCase):
    def labels(self, board, card, **kw):
        ctx = value.Outlook(on_map(board, **kw), gens_left=4)
        return " | ".join(f"{label}" for label, _ in value.value_card(card, ctx).parts)

    def test_oxygen_temperature_bonus_moves_to_11_percent(self):
        # Strip Mine raises oxygen 2 steps.
        self.assertIn("oxygen reaches 8%: bonus temperature step", self.labels("tharsis", "Strip Mine", oxygen=7))
        self.assertNotIn("bonus temperature step", self.labels(AMAZONIS, "Strip Mine", oxygen=7))
        self.assertIn("oxygen reaches 11%: bonus temperature step", self.labels(AMAZONIS, "Strip Mine", oxygen=10))
        self.assertNotIn("bonus temperature step", self.labels("tharsis", "Strip Mine", oxygen=10))

    def test_venus_16_percent_bonus_depends_on_the_map(self):
        # GHG Import From Venus raises Venus 1 step: 14% → 16%.
        self.assertIn("Venus reaches 16%: +1 TR", self.labels("tharsis", "GHG Import From Venus", venus=14))
        self.assertIn("Venus reaches 16%: build a colony", self.labels(AMAZONIS, "GHG Import From Venus", venus=14))
        self.assertIn("Venus reaches 22%: +1 TR", self.labels(AMAZONIS, "GHG Import From Venus", venus=20))


class Globals(unittest.TestCase):
    def test_globals_line_shows_the_map_maximums(self):
        line = digest.globals_line(on_map(AMAZONIS, temperature=8, oxygen=14, oceans=9)["game"])
        self.assertIn("/+14", line)
        self.assertIn("%/18", line)
        self.assertIn("/11", line)
        self.assertIn("differ from the standard maps", line)
        self.assertNotIn("differ", digest.globals_line(on_map("tharsis")["game"]))


if __name__ == "__main__":
    unittest.main()
