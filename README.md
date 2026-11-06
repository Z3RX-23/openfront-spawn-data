# OpenFront spawn data

Recolhe, **de 6 em 6 horas e sozinho** (GitHub Actions), jogos públicos do OpenFront e guarda contagens agregadas
de spawns/vitórias por zona do mapa, **um ficheiro por mapa**, servido por GitHub Pages:

```
https://<utilizador>.github.io/<repositorio>/data/<mapa>.json     ex.: .../data/northamerica.json
https://<utilizador>.github.io/<repositorio>/data/index.json       lista de mapas e nº de jogos
```

A extensão *OpenFront Spawn Heatmap* pede só o ficheiro do mapa onde estás.
Os ficheiros só têm contagens por zona, sem nomes de jogadores.

## Pôr a funcionar (uma vez)

1. No GitHub, cria um repositório **público** novo (por ex. `openfront-spawn-data`), vazio.
2. Nesta pasta (a que tem `.github/`, `collector/`, `docs/`), no terminal:
   ```bash
   git init
   git add .
   git commit -m "initial"
   git branch -M main
   git remote add origin https://github.com/<utilizador>/<repositorio>.git
   git push -u origin main
   ```
3. No repositório: **Settings → Pages → Build and deployment → Source: "Deploy from a branch"**,
   branch **main**, pasta **/docs** → Save.
4. **Actions** → *collect-spawns* → **Run workflow** (para não esperares 6 horas pela primeira corrida).
   Se a corrida falhar ao fazer push, vai a **Settings → Actions → General → Workflow permissions**
   e escolhe **Read and write permissions**.
5. Quando a corrida acabar (pode demorar mais de uma hora, são muitos pedidos com pausa de 2 s),
   abre `https://<utilizador>.github.io/<repositorio>/` e vê a lista de mapas.
6. Na extensão, no painel 🔥, cola em **URL dos dados**:
   `https://<utilizador>.github.io/<repositorio>/data`

## Como funciona

- `collector/collect.py --incremental` guarda em `state.json` até onde já processou e só trata jogos novos.
  Só processa jogos iniciados há mais de 90 min (para já terem terminado e terem vencedor).
- Cada corrida processa no máximo 2000 jogos (`--max-games`) e continua na seguinte, sem saltar nem repetir jogos.
- Os dados somam-se ao longo do tempo: quanto mais tempo passar, melhor fica o heatmap dos mapas populares.

## Notas

- O GitHub pode **desativar workflows agendados** em repositórios sem atividade (em geral ao fim de ~60 dias).
  Se parar de atualizar, vai a *Actions* e volta a ativá-lo.
- Os IPs do GitHub são partilhados, por isso a API do OpenFront pode limitar mais do que em casa.
  O script abranda sozinho; se aparecerem muitos 429 nos logs, aumenta `--delay` no `collect.yml`.
- Para limites mais altos, o OpenFront pede contacto via Discord (ver `docs/API.md` no repositório deles).

## Testes

```bash
cd collector
python -m unittest test_collect test_incremental
```
