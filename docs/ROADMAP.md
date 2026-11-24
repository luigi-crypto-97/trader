# Roadmap e criteri di passaggio

| Fase | Stato | Contenuto | Criterio per passare alla successiva |
|---|---|---|---|
| 0. Decisioni | fatto | Conto proprio, USA, $1.000, conto personale IT, approvazione umana | — |
| 1. Piattaforma dati | fatto | EDGAR point-in-time, cambi BCE, database, audit | Ingestione giornaliera senza errori (verificata: 784 filing, 0 errori) |
| 2. Ricerca | **da fare** | Storico prezzi, backtest del segnale insider fuori campione | Rendimento fuori campione superiore a SPY al netto dei costi, con drawdown accettabile |
| 3. Paper trading | da fare | Sistema completo su IBKR paper | Almeno 3 mesi; risultati coerenti col backtest; zero incidenti operativi |
| 4. Live ridotto | da fare | $1.000 reali, approvazione umana di ogni entrata | Slippage reale vicino alla stima; nessuna violazione di regole |
| 5. Automazione | da fare | `approval_mode=auto` con opt-in esplicito | Decisione presa per iscritto |

## Prossimi passi tecnici

1. **Storico prezzi per il backtest**: scaricare le barre giornaliere (IBKR `reqHistoricalData`
   o un fornitore dati) in `data/prices/<TICKER>.csv` e lo storico Form 4 di più anni
   (`trader ingest day` su un intervallo di date).
2. **Stop lato broker**: oggi stop e uscite a scadenza sono gestiti dal software durante la
   sessione. Prima del denaro reale, aggiungere ordini stop GTC presso IBKR, così la protezione
   vale anche se il processo è fermo.
3. **Verifica dell'adapter IBKR sul conto paper**: saldo regolato, frazionarie, commissioni
   effettive, mappatura degli stati d'ordine.
4. **Festività di borsa USA** nel calendario di sessione e di regolamento.
5. **Riporto delle minusvalenze** e dividendi nel registro fiscale.

## Limiti noti (dichiarati, non nascosti)

- Il simulatore e il backtester non modellano le festività USA; il backtester non modella il T+1.
- La conviction del segnale insider è una funzione euristica e va calibrata nel backtest.
- Il costo commissionale è una stima della tariffa Tiered (le fonti IBKR non coincidono sulle
  frazionarie): va confrontato con gli estratti conto paper.
- Il segnale LLM è valido in backtest solo su periodi successivi al cutoff del modello.
