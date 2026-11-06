import json
import os
import unittest

import collect

HERE = os.path.dirname(os.path.abspath(__file__))
DIMS = json.load(open(os.path.join(HERE, "map_dims.json")))
W, H = DIMS["world"]["normal"]  # 2000 x 1000


def tile(x, y, w=W):
    return y * w + x


def ffa_record(**over):
    rec = {
        "info": {
            "gameID": "TEST1234",
            "config": {"gameMap": "World", "gameMapSize": "Normal", "gameMode": "Free For All"},
            "players": [{"clientID": "AAA"}, {"clientID": "BBB"}, {"clientID": "CCC"}, {"clientID": "DDD"}],
            "winner": ["player", "BBB"],
        },
        "turns": [
            {"turnNumber": 1, "intents": [
                {"type": "spawn", "tile": tile(100, 100), "clientID": "AAA"},
                {"type": "spawn", "tile": tile(500, 300), "clientID": "BBB"},
                {"type": "spawn", "tile": tile(900, 400), "clientID": "CCC"},
            ]},
            {"turnNumber": 2, "intents": [
                {"type": "spawn", "tile": tile(1500, 800), "clientID": "BBB"},  # BBB muda de spawn
                {"type": "attack", "targetID": None, "troops": 10, "clientID": "AAA"},
            ]},
        ],
    }
    rec["info"].update(over)
    return rec


class ExtractTests(unittest.TestCase):
    def test_ffa_uses_last_spawn_and_marks_winner(self):
        rec, why = collect.extract_game(ffa_record(), DIMS)
        self.assertIsNone(why)
        self.assertEqual(rec["mode"], "ffa")
        got = {tuple(p[:2]): p[2] for p in rec["players"]}
        self.assertEqual(got[(1500, 800)], 1)  # BBB: ultimo spawn, venceu
        self.assertEqual(got[(100, 100)], 0)
        self.assertNotIn((500, 300), got)  # spawn antigo do BBB descartado
        self.assertAlmostEqual(rec["expected"], 1 / 3)  # 3 jogadores com spawn

    def test_compact_uses_smaller_width(self):
        cw, ch = DIMS["world"]["compact"]
        r = ffa_record()
        r["info"]["config"]["gameMapSize"] = "Compact"
        r["turns"] = [{"turnNumber": 1, "intents": [
            {"type": "spawn", "tile": tile(300, 200, cw), "clientID": "BBB"}]}]
        rec, why = collect.extract_game(r, DIMS)
        self.assertIsNone(why)
        self.assertEqual((rec["w"], rec["h"]), (cw, ch))
        self.assertEqual(rec["players"][0][:2], [300, 200])

    def test_team_winner_and_expected(self):
        r = ffa_record()
        r["info"]["config"]["gameMode"] = "Team"
        r["info"]["players"] = [
            {"clientID": "AAA", "team": "Red"}, {"clientID": "BBB", "team": "Blue"},
            {"clientID": "CCC", "team": "Red"}, {"clientID": "DDD", "team": "Blue"}]
        r["info"]["winner"] = ["team", "Blue", "BBB", "DDD"]
        rec, why = collect.extract_game(r, DIMS)
        self.assertEqual(rec["mode"], "team")
        self.assertAlmostEqual(rec["expected"], 0.5)  # 2 equipas
        self.assertEqual(sum(p[2] for p in rec["players"]), 1)  # so BBB tem spawn e venceu

    def test_skips(self):
        self.assertEqual(collect.extract_game(ffa_record(winner=None), DIMS)[1], "sem vencedor")
        self.assertEqual(collect.extract_game(ffa_record(statsAgreed=False), DIMS)[1], "stats nao confirmadas")
        r = ffa_record()
        r["info"]["config"]["gameMap"] = "Atlantis"
        self.assertIn("mapa desconhecido", collect.extract_game(r, DIMS)[1])
        r = ffa_record()
        r["turns"] = []
        self.assertEqual(collect.extract_game(r, DIMS)[1], "sem intents de spawn")


class AggregateTests(unittest.TestCase):
    def test_cells_sum_up(self):
        recs = [collect.extract_game(ffa_record(), DIMS)[0] for _ in range(3)]
        out = collect.aggregate(recs, DIMS)
        m = out["maps"]["world"]
        self.assertEqual(m["nx"], 100)
        self.assertEqual(m["ny"], 50)  # 2000x1000 -> 100x50
        ffa = m["modes"]["ffa"]
        self.assertEqual(ffa["games"], 3)
        total_spawns = sum(c[2] for c in ffa["cells"])
        total_wins = sum(c[3] for c in ffa["cells"])
        self.assertEqual(total_spawns, 9)  # 3 jogos x 3 jogadores
        self.assertEqual(total_wins, 3)
        # celula do spawn (1500,800) -> ix=75, iy=40
        cell = next(c for c in ffa["cells"] if c[0] == 75 and c[1] == 40)
        self.assertEqual(cell[2:4], [3, 3])
        json.dumps(out)  # serializavel


if __name__ == "__main__":
    unittest.main()
