---
name: viannas-rotas
description: Catálogo de origens e destinos do site Viannas pelo Mundo. Use ao buscar ou coletar passagens em milhas, ao adicionar origem/destino, ou quando a Paloma pedir GIG, Itália, Espanha, Portugal, Alemanha, França, EUA, Chile, Reino Unido, Argentina, Japão, China, Nordeste, finais de semana ou trechos nacionais.
---

# Rotas Viannas pelo Mundo

Fonte da verdade: `data/routes.json`.

Não inventar preço. Só milhas. Busca pessoal, não comercial. Sem API paga (nem Gecko). Sem furar proteção das cias.

## Primeira resposta (obrigatória)

Se faltar destino, data de ida ou data de volta — e nenhum padrão abaixo completar isso — **pergunte antes de coletar**:

1. Quais destinos você quer buscar? (cidade, IATA ou região: Nordeste, Itália, …)
2. Qual a data de ida?
3. Qual a data de volta?

Origem padrão: GIG, se ela não disser outra.

Se ela quiser **GIG e SDU na mesma rota**, não faça duas buscas. Use **RIO** na LATAM, na Azul e no GOL/Smiles (`--origem RIO` ou `--origem GIG,SDU`, que vira uma busca RIO). Na coleta, grave o aeroporto real de cada voo (GIG ou SDU).

Não dispare o Chrome só com “quero passagens” ou “busca o catálogo”.

## Padrões que já dão para rodar

Quando o pedido já traz destino + datas (ou um padrão de datas), monte a lista, mostre as datas e só então colete.

| Ela diz | Entenda |
|---|---|
| `finais de semana` / `fins de semana` / `fim de semana` do mês X | **Ida sexta, volta segunda**, cada semana daquele mês |
| `GIG e SDU` para o mesmo destino | **Uma busca com origem `RIO`** na LATAM, Azul e GOL (Galeão e Santos Dumont juntos) |
| `GIG para Salvador` / `SSA` | Origem GIG, destino SSA |
| `Nordeste`, `Itália`, `Portugal`, … | Todos os aeroportos da região no catálogo |
| Datas `15/12` e `28/12` | Ida e volta explícitas |
| `só ida` | Sem trecho de volta |

Exemplo completo:

> Pesquise passagem GIG para Salvador finais de semana do mês de dezembro

Vira 4 fins de semana em dezembro/2026:

- ida sex 04/12 · volta seg 07/12
- ida sex 11/12 · volta seg 14/12
- ida sex 18/12 · volta seg 21/12
- ida sex 25/12 · volta seg 28/12

```bash
cd ~/passagens-aereas-viannaspelomundo
export PYTHONPATH="$PWD/.vendor"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/playwright-browsers"
python3 -m app.snapshot ask --origem GIG --para SSA --mes 2026-12 --finais-de-semana --dry-run
```

Sem `--dry-run` busca de verdade (máximo 6 trechos por rodada, para no primeiro 403).

Datas explícitas:

```bash
python3 -m app.snapshot ask --origem GIG --para FCO --datas 2026-10-11,2026-10-18 --ida-volta
```

`--mes` sem `--finais-de-semana` amostra dia 10 (ida) e dia 20 (volta). Não varrer os 31 dias.

Espelhar o banco: `python3 -m app.snapshot from-db`  
Varredura do catálogo: `python3 -m app.snapshot catalog` — evitar.

## Catálogo

Nacional e internacional **não são iguais** na LATAM e na Azul. Fonte: `data/routes.json` (`programs` em cada aeroporto). Origem RIO não busca GIG/SDU.

**LATAM nacional:** Nordeste REC SSA FOR NAT MCZ AJU JPA SLZ BPS · Norte BEL MAO · Centro-Oeste BSB · Sudeste VCP CNF CGH GRU VIX · Sul POA CWB FLN NVT IGU

**Azul nacional:** Nordeste REC SSA FOR NAT MCZ AJU JPA THE SLZ BPS · Norte MAO BEL PVH RBR BVB MCP STM · Centro-Oeste BSB CGB GYN CGR · Sudeste VCP CNF CGH GRU VIX · Sul POA CWB FLN NVT IGU

**Azul internacional:** EUA FLL MCO · Europa LIS OPO MAD · Caribe/América do Sul CUR MVD PDP ASU BRC MDZ

**LATAM internacional:** Europa, América do Norte, América do Sul, América Central, Ásia, Oriente Médio, África (lista completa no `routes.json`)

## Ritmo ético

- No máximo 6 trechos por rodada
- 2–3 min na tela de resultados depois de cada sucesso; só então volta à home e preenche a próxima busca
- Ir **devagar**: um clique de ida por busca, esperar a volta carregar, não disparar outra pesquisa
- Parar no primeiro 403 daquela cia (as outras seguem)
- Sem stealth, proxy, fingerprint falso, replay de BFF ou bypass de captcha

O site mostra data e horário de cada voo para ela escolher na hora de comprar. Gravar todos os voos da busca, não só o mais barato.

Ida + volta, nacional e internacional — **uma busca, duas pastas**:

- Na tela de ida, ler **todos** os voos LIGHT de uma vez (milhas, paradas, duração). Salvar em `data/harvest/latam/DATA-IDA/ORIGEM-DESTINO.json`
- Clicar **uma** ida LIGHT. Na volta já aparecem LIGHT **e** executiva — ler as duas. Salvar em `data/harvest/latam/DATA-VOLTA/DESTINO-ORIGEM.json`
- Não voltar à ida nem refazer a busca
- Por enquanto só quantidade de paradas e duração total do card; sem abrir itinerário

Login (Chrome visível, sessão dela):

```bash
python3 -m app.collectors.latam login
python3 -m app.collectors.azul login
python3 -m app.collectors.smiles login
```
