# Roadmap e criteri di passaggio

| Fase | Stato | Contenuto | Criterio per passare alla successiva |
|---|---|---|---|
| 0. Decisioni | fatto | Conto proprio, USA, $1.000, conto personale IT, approvazione umana | — |
| 1. Piattaforma dati | fatto | EDGAR point-in-time, cambi BCE, database, audit | Ingestione giornaliera senza errori (verificata: 784 filing, 0 errori) |
| 2. Ricerca | **fatto — esito negativo** | Studio point-in-time del segnale insider ([conclusioni](research/fase2-conclusioni.md)) | **Non soddisfatto**: nessun extra-rendimento robusto fuori campione; non procedere con questo segnale |
| 3. Paper trading | da fare | Sistema completo su IBKR paper | Almeno 3 mesi; risultati coerenti col backtest; zero incidenti operativi |
| 4. Live ridotto | da fare | $1.000 reali, approvazione umana di ogni entrata | Slippage reale vicino alla stima; nessuna violazione di regole |
| 5. Automazione | da fare | `approval_mode=auto` con opt-in esplicito | Decisione presa per iscritto |

## Prossimi passi tecnici

1. **Nuove ipotesi di segnale** dichiarate prima di guardare i dati e testate solo su dati futuri
   (una è già dichiarata in [fase2-holdout.md](research/fase2-holdout.md)). Il secondo periodo di
   verifica del segnale insider è fatto: esito negativo confermato.
2. **Stop lato broker**: oggi stop e uscite a scadenza sono gestiti dal software durante la
   sessione. Prima del denaro reale, aggiungere ordini stop GTC presso IBKR, così la protezione
   vale anche se il processo è fermo.
3. **Verifica dell'adapter IBKR sul conto paper**: saldo regolato, frazionarie, commissioni
   effettive, mappatura degli stati d'ordine.
4. ~~Festività di borsa USA~~ (fatto: calendario NYSE con festività e chiusure anticipate).
5. **Riporto delle minusvalenze** e dividendi nel registro fiscale.

## Limiti noti (dichiarati, non nascosti)

- Il backtester non modella il regolamento T+1; il calendario NYSE non conosce in anticipo le
  chiusure straordinarie (vanno aggiunte quando annunciate).
- La conviction del segnale insider è una funzione euristica e va calibrata nel backtest.
- Il costo commissionale è una stima della tariffa Tiered (le fonti IBKR non coincidono sulle
  frazionarie): va confrontato con gli estratti conto paper.
- Il segnale LLM è valido in backtest solo su periodi successivi al cutoff del modello.
