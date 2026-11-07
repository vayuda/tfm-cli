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


class GameLength(unittest.TestCase):
    def test_past_typical_length_the_estimate_follows_the_steps_left(self):
        history = [{"temperature": -20, "oxygen": 4, "oceans": 4}, {"temperature": -16, "oxygen": 5, "oceans": 5},
                   {"temperature": -14, "oxygen": 7, "oceans": 6}]
        v = view(generation=10, temperature=-14, oxygen=7, oceans=6, players=3)
        v["game"]["globalsPerGeneration"] = history
        ctx = value.Outlook(v)
        self.assertEqual(ctx.remaining_steps, 11 + 7 + 3)
        self.assertGreaterEqual(ctx.gens_left, 4)  # not the 1 generation the typical-length cap alone would give

    def test_before_typical_length_the_cap_still_applies(self):
        ctx = value.Outlook(view(generation=3, temperature=-30, oxygen=0, oceans=0, players=3))
        self.assertEqual(ctx.gens_left, ctx.by_length)


class PlayOutput(unittest.TestCase):
    def test_globals_a_play_moved_are_reported(self):
        before = {"temperature": -14, "oxygenLevel": 5, "oceans": 3, "venusScaleLevel": 10}
        self.assertEqual(cli._globals_moved(before, dict(before)), "")
        after = dict(before, temperature=-12)
        self.assertEqual(cli._globals_moved(before, after), "temperature -12°C (was -14°C)")


class DroppedConnection(unittest.TestCase):
    def client(self, failures):
        from tfm.client import Client
        c = Client("http://example.invalid", "p1")
        calls = []

        def fake_open(req):
            calls.append(req.get_method())
            if len(calls) <= failures:
                raise ConnectionResetError("reset by peer")
            return "{}"
        c._open = fake_open
        return c, calls

    def test_a_read_is_retried_once(self):
        c, calls = self.client(failures=1)
        self.assertEqual(c.player(), {})
        self.assertEqual(calls, ["GET", "GET"])

    def test_a_submit_is_not_retried(self):
        from tfm.client import TfmError
        c, calls = self.client(failures=1)
        with self.assertRaisesRegex(TfmError, "may have been applied"):
            c.submit({"type": "option"})
        self.assertEqual(calls, ["POST"])


class Payment(unittest.TestCase):
    def ctx(self, **stock):
        from tfm import options
        v = view()
        v["thisPlayer"].update({"megacredits": 0, "heat": 0, "steel": 0, "titanium": 0, "plants": 0, **stock})
        v["game"]["gameOptions"]["boardName"] = "tharsis"
        return options.Context(v, set(), False)

    def test_martian_lumber_plants_cover_the_shortfall(self):
        from tfm import options
        card = {"name": "Mine", "calculatedCost": 4}  # a building card
        node = {"paymentOptions": {"plants": True}}
        pay, short = options.default_payment(card, node, self.ctx(megacredits=1, plants=4))
        self.assertEqual((pay, short), ({"megacredits": 1, "plants": 1}, 0))

    def test_plants_are_not_spent_when_money_covers_it(self):
        from tfm import options
        pay, short = options.default_payment({"name": "Mine", "calculatedCost": 4}, {"paymentOptions": {"plants": True}},
                                             self.ctx(megacredits=5, plants=4))
        self.assertEqual((pay, short), ({"megacredits": 4}, 0))


class Playable(unittest.TestCase):
    def test_cards_missing_from_the_play_choices_are_marked(self):
        from tfm import options
        wf = {"type": "or", "options": [{"type": "projectCard", "cards": [{"name": "Mine"}]}, {"type": "option"}]}
        self.assertEqual(options.playable_cards(wf), {"Mine"})
        self.assertIsNone(options.playable_cards({"type": "card", "cards": [{"name": "Mine"}]}))


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
