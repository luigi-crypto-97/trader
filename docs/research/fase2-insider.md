# Fase 2 — Studio sugli acquisti in cluster degli insider

Periodo segnali: 2024-12-02 → 2026-05-22 · split in-sample/out-of-sample: 2025-08-01 · benchmark: SPY
Filtri: prezzo ≥ 5 USD, controvalore medio 20 sedute ≥ 1000000 USD, un evento per titolo ogni 30 giorni.
Regola di selezione (fissata prima dei risultati): max in-sample month-clustered mean excess return at 20 sessions.
**Variante scelta: C**

## Extra-rendimento medio per evento rispetto a SPY

| Variante | Campione | Eventi | 5 sedute (media / mediana / % positivi / t mensile) | 20 sedute (media / mediana / % positivi / t mensile) | 60 sedute (media / mediana / % positivi / t mensile) |
|---|---|---|---|---|---|
| A | IS | 369 | -0.51% / -0.50% / 45% / -0.40 | -1.75% / -2.07% / 40% / -1.61 | -1.83% / -4.30% / 40% / -1.59 |
| A | OOS | 569 | +0.63% / +0.39% / 54% / 1.72 | +1.66% / +0.11% / 50% / 1.38 | -0.82% / -3.26% / 41% / -0.40 |
| B | IS | 179 | -0.46% / -0.56% / 43% / -0.61 | -1.63% / -2.03% / 40% / -1.95 | -1.65% / -4.76% / 39% / -1.05 |
| B | OOS | 282 | +0.87% / +0.60% / 56% / 2.19 | +1.34% / -0.44% / 48% / 1.04 | -0.80% / -2.57% / 45% / 0.08 |
| C | IS | 199 | -0.36% / -0.44% / 45% / -0.26 | -1.29% / -2.16% / 42% / 0.38 | -1.73% / -5.20% / 33% / 0.06 |
| C | OOS | 310 | +0.44% / +0.31% / 53% / 1.00 | +0.67% / -0.41% / 47% / 0.34 | -2.81% / -5.77% / 38% / -0.86 |
| D | IS | 230 | -1.15% / -1.24% / 39% / -2.03 | -2.34% / -2.14% / 39% / -2.18 | -3.96% / -5.65% / 33% / -1.94 |
| D | OOS | 373 | +0.67% / +0.29% / 53% / 2.54 | +1.68% / +0.38% / 51% / 1.77 | -0.07% / -2.25% / 45% / 0.16 |

Varianti:
- **A**: 2+ insider, totale >= 100k USD (parametri di produzione) (segnali grezzi 19580, scartati {'illiquid': 236, 'no_price_at_entry': 35, 'below_min_price': 5})
- **B**: 3+ insider, totale >= 100k USD (segnali grezzi 9588, scartati {'illiquid': 123, 'no_price_at_entry': 17, 'below_min_price': 1})
- **C**: 2+ insider, totale >= 500k USD (segnali grezzi 10413, scartati {'illiquid': 113, 'no_price_at_entry': 15, 'below_min_price': 2})
- **D**: 2+ insider, totale >= 100k USD, almeno un CEO/CFO/Presidente/Chair (segnali grezzi 12627, scartati {'illiquid': 148, 'no_price_at_entry': 20, 'below_min_price': 4})

## Portafoglio simulato da 1.000 USD (variante C, stesso dimensionamento e motore di rischio della produzione)

| Campione | Operazioni | Rendimento | SPY | Differenza | Max drawdown | Sharpe | % positive |
|---|---|---|---|---|---|---|---|
| IS | 48 | -15.97% | +9.02% | -24.99% | -17.17% | -1.99 | 27% |
| OOS | 120 | -11.15% | +19.55% | -30.70% | -15.01% | -0.86 | 40% |

## Lettura

Out-of-sample l'extra-rendimento a 20 sedute è positivo (+0.67%) ma non statisticamente robusto (t mensile 0.34): indizio, non prova.

Limiti dichiarati:

- costi e slippage sono inclusi solo nel portafoglio simulato, non nello studio per evento;
- prezzi non rettificati con split applicati alla data di esecuzione (nessun look-ahead sui filtri di prezzo); dividendi esclusi sia per i titoli sia per SPY;
- per i filing degli archivi trimestrali SEC la disponibilità è fissata alle 23:59 ET della data di deposito (prudenziale: entrata al massimo un giorno dopo il reale); dai filing acquisiti dal feed EDGAR si usa l'orario di accettazione esatto;
- in-sample con embargo: un evento conta a un orizzonte solo se l'uscita cade prima dello split;
- un cambio di ticker durante la detenzione chiude la finestra all'ultima chiusura disponibile;
- finestra di circa 2 anni; il t mensile tratta i mesi come indipendenti.
