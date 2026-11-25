# Fase 2 — Conclusioni sul segnale "cluster di acquisti insider"

Data: 25/11/2026 · Report automatico: [fase2-insider.md](fase2-insider.md)

## Esito

**Il criterio della fase 2 non è soddisfatto.** Nessuna delle quattro varianti mostra un
extra-rendimento robusto fuori campione. Il segnale **non** va portato al paper trading né al
denaro reale nella forma attuale.

| Verifica | Risultato (variante scelta C, 20 sedute) |
|---|---|
| In-sample vs SPY (dic 2024 – lug 2025, con embargo) | −1,29% medio, mediana −2,16%, 42% positivi |
| Out-of-sample vs SPY (ago 2025 – mag 2026) | +0,67% medio, mediana −0,41%, 47% positivi, t mensile 0,34 |
| Out-of-sample vs IWM (small cap) — esplorativo | −1,25% medio, mediana −2,01%, 42% positivi |
| Portafoglio 1.000 USD out-of-sample, con costi | −11,2% contro SPY +19,6% (120 operazioni) |

Il lieve risultato positivo contro SPY fuori campione scompare contro un benchmark di pari
dimensione (IWM): non è un vantaggio del segnale ma la differenza tra small cap e mega cap nel
periodo.

## Dati e metodo

- 258.000 Form 4 dagli archivi trimestrali SEC (ott 2024 – mar 2026) più il feed EDGAR
  (apr – mag 2026); prezzi giornalieri non rettificati di tutte le azioni USA, comprese le
  delistate (Massive), con 3.349 split applicati alla data di esecuzione.
- Segnale identico a quello di produzione, rigiocato ogni giorno alle 16:00 di New York con i
  soli dati pubblici a quell'ora; entrata all'apertura successiva.
- Protocollo fissato prima dei risultati: 4 varianti, split al 1/8/2025, selezione sulla media
  mensile in-sample a 20 sedute con embargo.
- Il portafoglio da 1.000 USD usa lo stesso dimensionamento, lo stesso motore di rischio e la
  stessa stima commissionale della produzione.

## Controlli di correttezza eseguiti

- Tre eventi ricalcolati a mano dai prezzi grezzi (DOW, LLY, OLED): coincidenti con lo studio.
- Quattro giri di revisione indipendente dell'agente `risk-guardian` su look-ahead, split,
  embargo e coerenza dei dati.
- Il confronto con IWM è **successivo** alla lettura dei risultati: è informativo, non è stato
  usato per scegliere nulla.

## Cosa non dimostra

- Non dimostra che gli acquisti degli insider siano inutili in generale: la letteratura usa
  orizzonti più lunghi (6–12 mesi) e periodi molto più ampi. Con 2 anni di prezzi (limite del
  piano gratuito) e un regime di mercato dominato dalle mega cap il campione è limitato.
- Non riguarda il segnale LLM sugli 8-K, che non è stato testato: un backtest storico sarebbe
  contaminato dalla conoscenza del modello (date anteriori al suo cutoff) ed è valido solo su
  dati successivi, cioè in forward test.

## Raccomandazione

1. **Non** passare alla fase 3 con questo segnale.
2. Completare l'acquisizione dei Form 4 fino a novembre 2026 e rieseguire lo stesso protocollo
   come secondo periodo di verifica, senza cambiare le varianti.
3. Valutare nuove ipotesi **dichiarate prima** di guardare i dati (per esempio orizzonti più
   lunghi e confronto con un benchmark di pari dimensione), sapendo che ogni nuova prova
   aumenta il rischio di trovare un risultato casuale.
4. Avviare il segnale LLM solo in modalità osservazione (forward test senza ordini), per
   accumulare un campione non contaminato.
