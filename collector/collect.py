#!/usr/bin/env python3
"""
OpenFront spawn collector.

Puxa jogos publicos da API oficial (https://api.openfront.io/public/...),
extrai onde cada jogador fez spawn e se ganhou, e gera um heatmaps.json
que a extensao Chrome importa.

So usa a biblioteca standard (Python 3.8+).

Exemplos:
  python collect.py --days 2 --max-games 200            # recolhe e agrega
  python collect.py --aggregate-only                    # re-agrega a cache
  python collect.py --demo                              # dataset SINTETICO p/ testar o overlay
  python collect.py --incremental --mode both           # modo agendado: 1 ficheiro por mapa em ../docs/data

A API tem rate limits muito estritos (ver docs/API.md do OpenFront). O script
espera entre pedidos, respeita Retry-After e guarda tudo em cache, por isso
podes correr varias vezes e ele continua de onde ficou.
"""
import argparse
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.openfront.io"
HERE = os.path.dirname(os.path.abspath(__file__))
UA = "openfront-spawn-heatmap/0.1 (personal stats tool)"
GRID_COLS = 100  # resolucao do heatmap (colunas); as linhas seguem o aspect ratio


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http_get_json(url, delay, retries=6):
    """GET com backoff em 429/5xx. Devolve (json, headers)."""
    wait = delay
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))
                time.sleep(delay)
                return body, r.headers
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                ra = e.headers.get("Retry-After")
                pause = float(ra) if ra and ra.replace(".", "", 1).isdigit() else min(wait * 2, 120)
                print(f"  HTTP {e.code}, a esperar {pause:.0f}s (tentativa {attempt + 1}/{retries})", file=sys.stderr)
                time.sleep(pause)
                wait = pause
                continue
            if e.code == 404:
                return None, None
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  erro de rede: {e}; a esperar {wait * 2:.0f}s", file=sys.stderr)
            time.sleep(wait * 2)
            wait *= 2
    raise RuntimeError(f"Falhou apos {retries} tentativas: {url}")


# ----------------------------------------------------------------------------
# Extracao
# ----------------------------------------------------------------------------
def norm_map_key(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def winner_ids(winner):
    """Winner = ['player', id, ...] | ['team', nome, id, ...] | ['nation', nome, id, ...]."""
    if not winner or not isinstance(winner, list) or len(winner) < 2:
        return set()
    kind = winner[0]
    ids = winner[1:] if kind == "player" else winner[2:]
    return {str(i) for i in ids}


def extract_game(record, dims):
    """
    Converte um GameRecord da API num registo compacto:
      {id, map, size, mode, w, h, players: [[x, y, won(0/1)], ...], expected}
    Devolve (registo | None, motivo_de_skip | None).
    """
    # a API devolve {info:{...}, turns:[...]}; tolera tambem um registo "plano"
    info = record.get("info") or record
    cfg = info.get("config") or {}
    gid = info.get("gameID") or record.get("gameID") or "?"

    if info.get("statsAgreed") is False:
        return None, "stats nao confirmadas"
    winner = info.get("winner")
    if not winner:
        return None, "sem vencedor"

    mkey = norm_map_key(cfg.get("gameMap", ""))
    if mkey not in dims:
        return None, f"mapa desconhecido ({cfg.get('gameMap')})"
    size = "compact" if str(cfg.get("gameMapSize", "")).lower() == "compact" else "normal"
    w, h = dims[mkey][size]

    # ultimo intent de spawn por jogador (podem mudar de sitio na fase de spawn)
    spawn_tile = {}
    for turn in record.get("turns") or []:
        for it in turn.get("intents") or []:
            if it.get("type") == "spawn":
                spawn_tile[str(it.get("clientID"))] = int(it["tile"])
    if not spawn_tile:
        return None, "sem intents de spawn"

    win_set = winner_ids(winner)
    mode_cfg = str(cfg.get("gameMode", "")).lower()
    teams = {}
    for p in info.get("players") or []:
        cid = str(p.get("clientID"))
        if cid in spawn_tile and p.get("team"):
            teams[cid] = p["team"]
    is_team = ("team" in mode_cfg) or bool(teams)
    mode = "team" if is_team else "ffa"

    n_spawned = len(spawn_tile)
    n_teams = len(set(teams.values())) if is_team else 0
    expected = 1.0 / (n_teams if n_teams >= 2 else n_spawned)

    players = []
    for cid, tile in spawn_tile.items():
        x, y = tile % w, tile // w
        if y >= h:
            return None, "tile fora do mapa (dimensoes erradas?)"
        players.append([x, y, 1 if cid in win_set else 0])

    return {
        "id": gid, "map": mkey, "size": size, "mode": mode,
        "w": w, "h": h, "players": players, "expected": expected,
    }, None


# ----------------------------------------------------------------------------
# Agregacao
# ----------------------------------------------------------------------------
def aggregate(records, dims):
    maps = {}
    for r in records:
        m = maps.setdefault(r["map"], {
            "name": dims[r["map"]]["name"],
            "nx": GRID_COLS,
            "ny": max(1, round(GRID_COLS * dims[r["map"]]["normal"][1] / dims[r["map"]]["normal"][0])),
            "modes": {},
        })
        mode = m["modes"].setdefault(r["mode"], {"games": 0, "_cells": {}})
        mode["games"] += 1
        for x, y, won in r["players"]:
            ix = min(m["nx"] - 1, int(x / r["w"] * m["nx"]))
            iy = min(m["ny"] - 1, int(y / r["h"] * m["ny"]))
            c = mode["_cells"].setdefault((ix, iy), [0, 0, 0.0])
            c[0] += 1
            c[1] += won
            c[2] += r["expected"]
    for m in maps.values():
        for mode in m["modes"].values():
            cells = mode.pop("_cells")
            mode["cells"] = [[ix, iy, s, w, round(e, 4)] for (ix, iy), (s, w, e) in sorted(cells.items())]
    return {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "demo": False,
        "maps": maps,
    }


# ----------------------------------------------------------------------------
# Demo (dados SINTETICOS, so para testar o overlay)
# ----------------------------------------------------------------------------
def make_demo(dims):
    rnd = random.Random(7)
    maps = {}
    for key in ("world", "europe", "northamerica"):
        if key not in dims:
            continue
        nx = GRID_COLS
        ny = max(1, round(GRID_COLS * dims[key]["normal"][1] / dims[key]["normal"][0]))
        blobs = [(rnd.random(), rnd.random(), rnd.uniform(0.05, 0.12), rnd.uniform(0.6, 2.4)) for _ in range(6)]
        cells = []
        for iy in range(ny):
            for ix in range(nx):
                fx, fy = (ix + .5) / nx, (iy + .5) / ny
                lift = 1.0
                for bx, by, br, bl in blobs:
                    d2 = ((fx - bx) ** 2 + (fy - by) ** 2) / (br * br)
                    lift += (bl - 1.0) * math.exp(-d2)
                spawns = rnd.randint(0, 6)
                if spawns == 0:
                    continue
                exp = spawns / 8.0
                cells.append([ix, iy, spawns, round(exp * max(lift, 0.1)), round(exp, 4)])
        maps[key] = {"name": dims[key]["name"], "nx": nx, "ny": ny,
                     "modes": {"ffa": {"games": 0, "cells": cells}}}
    return {"version": 1, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "demo": True, "maps": maps}


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
# Modo incremental (para correr agendado, p.ex. GitHub Actions)
# ----------------------------------------------------------------------------
def parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def fmt_iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def list_window(api, start, end, api_mode, delay):
    """Metadados de todos os jogos publicos iniciados em [start, end] (paginado, 1000 por pedido)."""
    games, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "start": fmt_iso(start), "end": fmt_iso(end),
            "type": "Public", "mode": api_mode, "limit": 1000, "offset": offset,
        })
        page, _ = http_get_json(f"{api}/public/games?{q}", delay)
        if not page:
            break
        games.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    return games


def load_json(path, default):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def merge_map(old, new):
    """Soma as contagens de 'new' sobre 'old' (ambos no formato de uma entrada maps[key])."""
    if not old:
        return new
    if (old["nx"], old["ny"]) != (new["nx"], new["ny"]):
        raise ValueError("grelha diferente entre ficheiro existente e novos dados")
    for mode, nm in new["modes"].items():
        om = old["modes"].setdefault(mode, {"games": 0, "cells": []})
        om["games"] += nm["games"]
        acc = {(c[0], c[1]): [c[2], c[3], c[4]] for c in om["cells"]}
        for ix, iy, s, w, e in nm["cells"]:
            a = acc.setdefault((ix, iy), [0, 0, 0.0])
            a[0] += s
            a[1] += w
            a[2] += e
        om["cells"] = [[ix, iy, s, w, round(e, 4)] for (ix, iy), (s, w, e) in sorted(acc.items())]
    return old


def write_outputs(out_dir, new_agg):
    """Funde 'new_agg' nos ficheiros por mapa (<out_dir>/<mapa>.json) e reconstroi o index.json."""
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, entry in new_agg["maps"].items():
        path = os.path.join(out_dir, key + ".json")
        old = load_json(path, {}).get("maps", {}).get(key)
        merged = merge_map(old, entry)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "generated": stamp, "demo": False, "maps": {key: merged}},
                      f, separators=(",", ":"))
    index = {}
    for name in sorted(os.listdir(out_dir)):
        if not name.endswith(".json") or name == "index.json":
            continue
        m = load_json(os.path.join(out_dir, name), {}).get("maps", {})
        for key, entry in m.items():
            index[key] = {"name": entry["name"], "games": {k: v["games"] for k, v in entry["modes"].items()}}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({"version": 1, "generated": stamp, "maps": index}, f, separators=(",", ":"), indent=None)


def run_incremental(args, dims):
    """
    Processa os jogos novos desde o ultimo estado. So avanca o estado ate ao ultimo jogo
    realmente processado (por limite de --max-games ou erro), por isso nunca salta jogos.
    Os jogos so entram depois de --settle-minutes, para ja terem terminado e ter vencedor.
    """
    state = load_json(args.state, {})
    now = datetime.now(timezone.utc)
    horizon = now - timedelta(minutes=args.settle_minutes)
    start = parse_iso(state["last_start"]) if state.get("last_start") else now - timedelta(days=args.initial_days)
    done = set(state.get("boundary_ids", []))
    end = min(start + timedelta(days=2) - timedelta(seconds=1), horizon)
    if end <= start:
        print("Nada para processar (janela vazia).")
        return

    api_modes = {"ffa": ["Free For All"], "team": ["Team"], "both": ["Free For All", "Team"]}[args.mode]
    games = []
    for m in api_modes:
        games.extend(list_window(args.api, start, end, m, args.delay))
    games = [g for g in games if g.get("game") and g["game"] not in done]
    games.sort(key=lambda g: (g.get("start", ""), g["game"]))
    print(f"Janela {fmt_iso(start)} .. {fmt_iso(end)}: {len(games)} jogos por processar")

    recs, skipped, processed, last = [], {}, 0, None
    capped = False
    for g in games:
        if processed >= args.max_games:
            capped = True
            break
        try:
            rec_json, _ = http_get_json(f"{args.api}/public/game/{g['game']}", args.delay)
        except RuntimeError as e:
            print(f"  a parar: {e}", file=sys.stderr)
            capped = True
            break
        processed += 1
        last = g
        if not rec_json:
            skipped["404"] = skipped.get("404", 0) + 1
            continue
        rec, why = extract_game(rec_json, dims)
        if rec is None:
            skipped[why] = skipped.get(why, 0) + 1
        else:
            recs.append(rec)
        if processed % 25 == 0:
            print(f"  {processed} processados")

    if recs:
        write_outputs(args.out_dir, aggregate(recs, dims))

    if capped and last is not None:
        new_start = parse_iso(last["start"])
        boundary = set(done) if new_start == start else set()
        boundary |= {g["game"] for g in games[:processed] if parse_iso(g["start"]) == new_start}
    elif capped:  # parou antes de processar qualquer jogo: mantem o estado
        new_start, boundary = start, done
    else:
        new_start = end
        boundary = {g["game"] for g in games if parse_iso(g["start"]) == end}
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump({"last_start": fmt_iso(new_start), "boundary_ids": sorted(boundary),
                   "updated": fmt_iso(now)}, f, indent=1)
    print(f"Processados {processed} ({len(recs)} validos). Ignorados: {skipped or 'nenhum'}. "
          f"Estado -> {fmt_iso(new_start)}{' (limite atingido, continua na proxima corrida)' if capped else ''}")


def load_cache(path):
    seen, recs = set(), []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                o = json.loads(line)
                seen.add(o["id"])
                if o.get("rec"):
                    recs.append(o["rec"])
    return seen, recs


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=2, help="quantos dias para tras (default 2)")
    ap.add_argument("--mode", choices=["ffa", "team", "both"], default="ffa")
    ap.add_argument("--max-games", type=int, default=200, help="maximo de jogos novos a descarregar")
    ap.add_argument("--delay", type=float, default=2.0, help="segundos entre pedidos (default 2)")
    ap.add_argument("--cache", default=os.path.join(HERE, "games_cache.jsonl"))
    ap.add_argument("--out", default=os.path.join(HERE, "heatmaps.json"))
    ap.add_argument("--aggregate-only", action="store_true")
    ap.add_argument("--demo", action="store_true", help="gera dados sinteticos (NAO sao reais)")
    ap.add_argument("--api", default=API, help="base da API (default %(default)s)")
    ap.add_argument("--incremental", action="store_true",
                    help="modo agendado: processa so jogos novos e escreve um ficheiro por mapa em --out-dir")
    ap.add_argument("--out-dir", default=os.path.join(HERE, "..", "docs", "data"))
    ap.add_argument("--state", default=os.path.join(HERE, "..", "state.json"))
    ap.add_argument("--settle-minutes", type=int, default=90, help="so processa jogos iniciados ha mais de N min")
    ap.add_argument("--initial-days", type=float, default=2, help="na 1a corrida, quantos dias para tras")
    args = ap.parse_args()

    with open(os.path.join(HERE, "map_dims.json"), encoding="utf-8") as f:
        dims = json.load(f)

    if args.incremental:
        run_incremental(args, dims)
        return

    if args.demo:
        out = make_demo(dims)
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(out, f)
        print(f"Demo SINTETICO escrito em {args.out}")
        return

    seen, recs = load_cache(args.cache)
    print(f"Cache: {len(seen)} jogos ja vistos, {len(recs)} utilizaveis")

    if not args.aggregate_only:
        modes = {"ffa": ["Free For All"], "team": ["Team"], "both": ["Free For All", "Team"]}[args.mode]
        now = datetime.now(timezone.utc).replace(microsecond=0)
        new = 0
        skipped = {}
        with open(args.cache, "a", encoding="utf-8") as cache:
            for api_mode in modes:
                # janelas de 2 dias (limite da API), da mais recente para tras
                windows = []
                end = now
                remaining = args.days
                while remaining > 0:
                    span = min(2, remaining)
                    windows.append((end - timedelta(days=span), end))
                    end -= timedelta(days=span)
                    remaining -= span
                for start, end in windows:
                    offset = 0
                    while new < args.max_games:
                        q = urllib.parse.urlencode({
                            "start": start.isoformat().replace("+00:00", "Z"),
                            "end": end.isoformat().replace("+00:00", "Z"),
                            "type": "Public", "mode": api_mode, "limit": 100, "offset": offset,
                        })
                        page, _ = http_get_json(f"{args.api}/public/games?{q}", args.delay)
                        if not page:
                            break
                        print(f"[{api_mode}] {start:%m-%d %H:%M}..{end:%m-%d %H:%M} offset {offset}: {len(page)} jogos")
                        for g in page:
                            gid = g.get("game")
                            if not gid or gid in seen or new >= args.max_games:
                                continue
                            rec_json, _ = http_get_json(f"{args.api}/public/game/{gid}", args.delay)
                            seen.add(gid)
                            new += 1
                            if not rec_json:
                                cache.write(json.dumps({"id": gid, "rec": None}) + "\n")
                                continue
                            rec, why = extract_game(rec_json, dims)
                            if rec is None:
                                skipped[why] = skipped.get(why, 0) + 1
                            else:
                                recs.append(rec)
                            cache.write(json.dumps({"id": gid, "rec": rec}) + "\n")
                            cache.flush()
                            if new % 10 == 0:
                                print(f"  {new} jogos novos descarregados")
                        if len(page) < 100:
                            break
                        offset += 100
        print(f"Novos: {new}. Ignorados: {skipped or 'nenhum'}")

    out = aggregate(recs, dims)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f)
    total = sum(m["modes"][k]["games"] for m in out["maps"].values() for k in m["modes"])
    print(f"{len(out['maps'])} mapas, {total} jogos agregados -> {args.out}")


if __name__ == "__main__":
    main()
