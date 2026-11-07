"""Output fixes from agent game reports: award metrics, concrete colony trades. Run: python3 -m unittest discover tests"""
import contextlib
import io
import unittest
from types import SimpleNamespace

from tfm import cli, data, digest, value
from test_value import view


def colony_view(*cols):
    v = view()
    v["thisPlayer"].update({"fleetSize": 1, "tradesThisGeneration": 0, "coloniesCount": 0})
    v["game"]["colonies"] = [{"name": n, "trackPosition": pos, "colonies": [], "isActive": True} for n, pos in cols]
    return v


class TradeNow(unittest.TestCase):
    def test_reward_names_the_resource_under_the_marker(self):
        self.assertEqual(data.trade_now("Europa", 0), "Gain 1 M€ production")
        self.assertEqual(data.trade_now("Europa", 4), "Gain 1 plants production")
        self.assertEqual(data.trade_now("Mercury", 5), "Gain 1 titanium production")
        self.assertTrue(data.trade_now("Hygiea", 6).startswith("Steal 3 titanium"))

    def test_terra_names_the_parameter(self):
        self.assertIn("raises temperature", data.trade_now("Terra", 0))
        self.assertIn("raises oxygen", data.trade_now("Terra", 3))
        self.assertIn("places an ocean", data.trade_now("Terra", 6))

    def test_counted_rewards_fill_in_n(self):
        self.assertEqual(data.trade_now("Titania", 1), "Gain 2 VP")
        self.assertEqual(data.trade_now("Luna", 3), "Gain 7 M€")

    def test_colonies_section_prints_the_reward_now(self):
        v = colony_view(("Titania", 1), ("Europa", 4))
        lines = digest.colonies(v["game"], v, {})
        self.assertIn("Titania: trade now Gain 2 VP", lines[1])
        self.assertIn("Lose 3 M€", lines[1])  # owners pay when anyone trades there
        self.assertIn("Europa: trade now Gain 1 plants production", lines[2])
        self.assertIn("track M€, M€, energy, energy, plants", lines[2])

    def test_trade_value_follows_the_resource(self):
        low = value.trade_value({"name": "Europa", "trackPosition": 0, "colonies": []}, value.Outlook(colony_view()))
        high = value.trade_value({"name": "Mercury", "trackPosition": 5, "colonies": []}, value.Outlook(colony_view()))
        self.assertGreater(high, low)  # titanium production beats M€ production


class AresAdjacency(unittest.TestCase):
    def test_space_next_to_nuclear_zone_shows_the_fee(self):
        from tfm import options
        v = view()
        v["game"]["gameOptions"].update({"boardName": "tharsis", "expansions": {"ares": True}})
        sid, nbr = "03", data.adjacency("tharsis", "03")[0]
        v["game"]["spaces"] = [{"id": sid, "spaceType": "land", "bonus": []},
                               {"id": nbr, "spaceType": "land", "bonus": [], "tileType": 12, "color": "green"}]
        ctx = options.Context(v, set(), False)
        self.assertIn("costs 2 M€ to build here", options.describe_space(sid, ctx))
        v["game"]["gameOptions"]["expansions"] = {}
        self.assertNotIn("costs 2 M€", options.describe_space(sid, options.Context(v, set(), False)))


class Awards(unittest.TestCase):
    def test_award_text_printed_every_turn(self):
        a = {"name": "Kingpin", "scores": [{"color": "red", "score": 2}]}
        v = view(awards=[a])
        ctx = SimpleNamespace(show_text=lambda key: False)
        lines = digest.awards(v["game"], v["thisPlayer"], {}, ctx)
        self.assertIn("crime tags", lines[1])

    def test_card_looks_up_awards_and_milestones(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.cmd_card(SimpleNamespace(names=["manufacturer", "Tactician"]))
        self.assertIn("Manufacturer [award] — Have the highest production of steel and heat", out.getvalue())
        self.assertIn("Tactician [milestone]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
