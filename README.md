# Viannas pelo Mundo

Site privado para buscar passagens **em milhas**, com destinos nacionais e internacionais em cards, filtro das mais baratas e exportação Excel.

## De onde vêm as milhas

As três cias usam o mesmo coletor local: Chrome visível, perfil persistente com o **seu login**, preenche a home como pessoa e intercepta o JSON que o próprio site já pede.

- **LATAM Pass:** `python3 -m app.collectors.latam login`
- **TudoAzul:** `python3 -m app.collectors.azul login`
- **GOL Smiles:** `python3 -m app.collectors.smiles login`

Não há API paga, busca em dinheiro nem Seats.aero. GIG e SDU na mesma rota viram **uma busca RIO**.

## Como usar

1. Abra [http://127.0.0.1:8765](http://127.0.0.1:8765) (senha local: `viannas-2026`)
2. Faça login uma vez em cada cia (comandos acima)
3. Os cards só aparecem com milhas reais
4. Em ida e volta, o número é dos **dois trechos juntos**

O painel **não** consulta milhas a cada clique. Às **00h** e **12h** o coletor grava `data/harvest/` no site de cada cia que já tiver sessão. Esses JSON ficam no repositório **privado** [buscar-passagens-dados](https://github.com/palomavmenezes/buscar-passagens-dados):

```bash
git clone https://github.com/palomavmenezes/buscar-passagens-dados.git data/harvest
```

Os arquivos ficam assim: `latam/2026-10-11/GIG-SSA.json` (cia → data da passagem → trecho).

Pedido pontual:

```bash
python3 -m app.snapshot ask --origem RIO --para VIX --datas 2026-10-04,2026-10-18 --ida-volta
```

## Ligar o site

```bash
cd ~/passagens-aereas-viannaspelomundo
export PYTHONPATH="$PWD/.vendor"
export PLAYWRIGHT_BROWSERS_PATH="$PWD/data/playwright-browsers"
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```
