"""Teste end-to-end do modo --incremental contra uma API falsa (servidor HTTP local)."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import collect

HERE = os.path.dirname(os.path.abspath(__file__))
def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


DIMS = load(os.path.join(HERE, "map_dims.json"))
W = DIMS["world"]["normal"][0]


def tile(x, y):
    return y * W + x


def build_fake_games():
    """Devolve lista de (meta, record). Inclui: FFA+Team, 2 jogos com o mesmo start, sem vencedor, mapa desconhecido."""
    base = (datetime.now(timezone.utc) - timedelta(hours=5)).replace(microsecond=0)
    specs = [  # (offset_min, mode, kind)
        (0, "Free For All", "ok"), (10, "Free For All", "ok"), (20, "Team", "ok"),
        (30, "Free For All", "ok"), (30, "Free For All", "ok"),  # mesmo start
        (40, "Free For All", "nowinner"), (50, "Team", "ok"), (60, "Free For All", "badmap"),
        (70, "Free For All", "ok"), (80, "Team", "ok"),
    ]
    out = []
    for i, (off, mode, kind) in enumerate(specs):
        gid = f"G{i:03d}"
        start = base + timedelta(minutes=off, milliseconds=123)
        team = mode == "Team"
        players = [{"clientID": f"{gid}p{k}", **({"team": "Red" if k % 2 == 0 else "Blue"} if team else {})} for k in range(4)]
        info = {
            "gameID": gid,
            "config": {"gameMap": "Atlantis" if kind == "badmap" else "World", "gameMapSize": "Normal", "gameMode": mode},
            "players": players,
            "winner": None if kind == "nowinner" else (["team", "Blue", f"{gid}p1", f"{gid}p3"] if team else ["player", f"{gid}p1"]),
        }
        turns = [{"turnNumber": 1, "intents": [
            {"type": "spawn", "tile": tile(100 + 400 * k + i, 200 + 150 * k), "clientID": f"{gid}p{k}"} for k in range(4)]}]
        meta = {"game": gid, "start": collect.fmt_iso(start), "end": collect.fmt_iso(start + timedelta(minutes=20)),
                "type": "Public", "mode": mode}
        out.append((meta, {"info": info, "turns": turns}, kind))
    return out


class FakeAPI(BaseHTTPRequestHandler):
    games = []
    game_hits = Counter()
    list_hits = 0
    first_429_done = set()

    def log_message(self, *a):
        pass

    def _send(self, code, body, headers=None):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/public/games":
            FakeAPI.list_hits += 1
            start, end = collect.parse_iso(q["start"][0]), collect.parse_iso(q["end"][0])
            if end - start > timedelta(days=2):
                return self._send(400, {"error": "range"})
            lim, off = int(q.get("limit", ["50"])[0]), int(q.get("offset", ["0"])[0])
            rows = [m for m, _, _ in FakeAPI.games
                    if start <= collect.parse_iso(m["start"]) <= end and m["mode"] == q["mode"][0]]
            rows.sort(key=lambda m: (m["start"], m["game"]))
            return self._send(200, rows[off:off + lim], {"Content-Range": f"games {off}-{off + lim}/{len(rows)}"})
        if u.path.startswith("/public/game/"):
            gid = u.path.rsplit("/", 1)[1]
            rec = next((r for m, r, _ in FakeAPI.games if m["game"] == gid), None)
            if rec is None:
                return self._send(404, {})
            if gid == "G002" and gid not in FakeAPI.first_429_done:  # forca um 429 uma vez
                FakeAPI.first_429_done.add(gid)
                return self._send(429, {}, {"Retry-After": "0"})
            FakeAPI.game_hits[gid] += 1
            return self._send(200, rec)
        self._send(404, {})


class IncrementalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        FakeAPI.games = build_fake_games()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_collect(self, tmp, max_games):
        r = subprocess.run([
            sys.executable, os.path.join(HERE, "collect.py"), "--incremental", "--mode", "both",
            "--max-games", str(max_games), "--settle-minutes", "0", "--delay", "0",
            "--initial-days", "1", "--api", self.url,
            "--out-dir", os.path.join(tmp, "data"), "--state", os.path.join(tmp, "state.json")],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def test_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            # corrida 1: o limite cai ENTRE G003 e G004, que tem o mesmo start (caso delicado)
            self.run_collect(tmp, 4)
            order = sorted(FakeAPI.games, key=lambda t: (t[0]["start"], t[0]["game"]))
            self.assertEqual(sum(FakeAPI.game_hits.values()), 4)
            self.assertEqual(set(FakeAPI.game_hits), {m["game"] for m, _, _ in order[:4]})
            self.assertEqual(order[3][0]["start"], order[4][0]["start"])  # confirma que o caso e mesmo o delicado
            st = load(os.path.join(tmp, "state.json"))
            self.assertEqual(st["last_start"], order[3][0]["start"])
            self.assertEqual(st["boundary_ids"], [order[3][0]["game"]])

            # corrida 2: sem limite pratico -> processa o resto, sem repetir os da corrida 1
            self.run_collect(tmp, 100)
            self.assertEqual(sum(FakeAPI.game_hits.values()), len(order))
            self.assertTrue(all(v == 1 for v in FakeAPI.game_hits.values()), dict(FakeAPI.game_hits))

            # corrida 3: nada de novo -> nenhum jogo pedido outra vez
            before = dict(FakeAPI.game_hits)
            self.run_collect(tmp, 100)
            self.assertEqual(dict(FakeAPI.game_hits), before)

            # totais: so jogos "ok" contam (sem vencedor e mapa desconhecido sao ignorados)
            ok = [(m, r) for m, r, kind in order if kind == "ok"]
            data = load(os.path.join(tmp, "data", "world.json"))
            entry = data["maps"]["world"]
            exp_games = Counter("team" if m["mode"] == "Team" else "ffa" for m, _ in ok)
            for mode in ("ffa", "team"):
                self.assertEqual(entry["modes"][mode]["games"], exp_games[mode], mode)
                spawns = sum(c[2] for c in entry["modes"][mode]["cells"])
                wins = sum(c[3] for c in entry["modes"][mode]["cells"])
                self.assertEqual(spawns, 4 * exp_games[mode], mode)
                # FFA: 1 vencedor por jogo; Team: 2 jogadores vencedores por jogo
                self.assertEqual(wins, (2 if mode == "team" else 1) * exp_games[mode], mode)
            # expected por spawn: FFA 1/4, Team 1/2
            ffa_exp = sum(c[4] for c in entry["modes"]["ffa"]["cells"])
            team_exp = sum(c[4] for c in entry["modes"]["team"]["cells"])
            self.assertAlmostEqual(ffa_exp, 4 * exp_games["ffa"] * 0.25, places=2)
            self.assertAlmostEqual(team_exp, 4 * exp_games["team"] * 0.5, places=2)

            index = load(os.path.join(tmp, "data", "index.json"))
            self.assertEqual(index["maps"]["world"]["games"], dict(exp_games))
            self.assertNotIn("atlantis", index["maps"])

    def test_merge_map_is_additive(self):
        a = {"name": "W", "nx": 2, "ny": 1, "modes": {"ffa": {"games": 1, "cells": [[0, 0, 2, 1, 0.5]]}}}
        b = {"name": "W", "nx": 2, "ny": 1, "modes": {"ffa": {"games": 2, "cells": [[0, 0, 3, 0, 0.75], [1, 0, 1, 1, 0.25]]}}}
        m = collect.merge_map(a, b)["modes"]["ffa"]
        self.assertEqual(m["games"], 3)
        self.assertEqual(m["cells"], [[0, 0, 5, 1, 1.25], [1, 0, 1, 1, 0.25]])


if __name__ == "__main__":
    unittest.main()
