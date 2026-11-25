# Fase 2 — Secondo periodo di verifica (giugno – novembre 2026)

Data: 25/11/2026 · Dati: `var/research/fase2-holdout.json` (non versionato)

Stesso protocollo dello studio principale, stesse quattro varianti, **nessuna nuova selezione**:
la variante C era stata scelta in precedenza sui dati in-sample. Segnali disponibili tra il
23/05/2026 e il 24/11/2026; i filing di questo periodo provengono dal feed EDGAR con l'orario di
accettazione esatto (entrata simulata fino a un giorno prima rispetto agli archivi trimestrali).
Le finestre a 20 e 60 sedute degli eventi più recenti non sono ancora osservabili.

## Extra-rendimento medio per evento rispetto a SPY

| Variante | Eventi | 5 sedute (media / mediana / % positivi) | 20 sedute | 60 sedute |
|---|---|---|---|---|
| A | 346 | −0,16% / −0,19% / 49% | −0,66% / +0,10% / 51% | −2,52% / −1,45% / 44% |
| B | 147 | −0,62% / −0,51% / 44% | −0,65% / −0,46% / 48% | +0,12% / −0,18% / 50% |
| **C (scelta)** | 200 | −0,26% / −0,56% / 46% | **−0,08% / +0,19% / 52%** | −0,83% / −0,10% / 49% |
| D | 210 | −0,52% / −0,20% / 49% | −0,59% / −0,79% / 48% | −1,29% / −1,49% / 48% |

Nessun t mensile supera 0,5 in valore assoluto a 20 o 60 sedute.

## Variante C: benchmark e portafoglio

| Verifica | Risultato |
|---|---|
| 20 sedute vs SPY | −0,08% medio, mediana +0,19% |
| 20 sedute vs IWM (esplorativo) | +1,10% medio, mediana +1,01% (nel periodo precedente: −1,25%) |
| Portafoglio 1.000 USD con costi | −16,3% contro SPY −4,2%; 75 operazioni, 28% positive, media −3,6% per operazione |

## Lettura

1. **Conferma l'esito negativo della fase 2.** Il segnale non mostra un vantaggio fuori campione
   in nessuno dei due periodi; il segno del risultato contro IWM cambia da un periodo all'altro,
   cioè è rumore.
2. **La gestione del portafoglio peggiora un segnale neutro.** A livello di evento il risultato è
   circa zero, ma il portafoglio perde: con uno stop al 10% su titoli piccoli e volatili, la
   maggior parte delle operazioni viene chiusa in perdita dal rumore di prezzo.

## Ipotesi dichiarata ora, non ancora verificata

Da testare solo su dati che arriveranno **dopo** oggi (25/11/2026), per non adattarla ai
risultati già visti: segnale C con orizzonte di 60 sedute, stop solo catastrofico (−25%) e
confronto con un benchmark di pari dimensione (IWM). Criterio: extra-rendimento medio positivo con
t mensile ≥ 2 dopo almeno 6 mesi di dati nuovi.
