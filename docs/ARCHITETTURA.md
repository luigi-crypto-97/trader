# Architettura

## Principio guida

**L'LLM propone, il codice deterministico decide, l'umano approva.** Nessun modello linguistico
e nessun agente ha accesso a strumenti che inviano ordini.

## Componenti

```
            SEC EDGAR (Form 4, 8-K)              ECB (cambi)
                    |                                 |
            data/edgar (client, parser, ingest)   data/fx
                    |                                 |
                 database (SQLite / Postgres) <-------+
                    |
   +----------------+-----------------+
   |                |                 |
signals/insider  signals/llm      mcp_server (agenti: propose_signal)
   |                |                 |
   +-------> pipeline (PROPOSED) <----+
                    |
        risk/sizing + risk/engine (puri, deterministici)
                    |
        approval (CLI / Telegram)  <-- umano
                    |
        broker (simulato | IBKR)  --> fill --> piani di uscita
                    |
        audit (catena di hash)   tax/ledger (RT, IVAFE)
```

## Scelte principali

| Tema | Scelta | Motivo |
|---|---|---|
| Orizzonte | Swing (giorni-settimane) | Sulla latenza non si compete; l'LLM serve per la profondità di lettura |
| Dati | EDGAR con timestamp di accettazione | Point-in-time: nessuna informazione dal futuro |
| Segnale base | Cluster di acquisti discrezionali di insider | Anomalia documentata, spiegabile, nessun costo LLM |
| Segnale LLM | Smistamento con Haiku 5.5, analisi con Opus 5.5 | Costo basso sul volume, qualità sull'analisi |
| Output LLM | JSON vincolato da schema + citazioni verificate alla lettera | Niente testo libero eseguito, niente allucinazioni non rilevate |
| Rischio | Motore puro, ogni controllo riportato | Testabile, spiegabile, non aggirabile da prompt |
| Esecuzione | Solo ordini limite in entrata; ciò che si approva è ciò che si invia | Nessuna sorpresa di prezzo o quantità |
| Conto | Cash, solo long, solo contante regolato | Nessuna leva; niente good-faith violation |
| Broker | Interfaccia unica; simulatore e IBKR | Cambiare broker non tocca strategia e rischio |
| Persistenza | SQLAlchemy (SQLite di default) | Zero infrastruttura per partire; Postgres quando serve |
| Audit | Catena SHA-256 | Ogni modifica a posteriori è rilevabile |

## Controlli del motore di rischio

Entrate (tutti obbligatori): kill switch spento · equity positiva · drawdown massimo (oltre la
soglia attiva il kill switch) · perdita giornaliera · restricted list · whitelist · ordini/giorno ·
solo ordini limite · passo di quantità · quotazione disponibile e recente · quotazione a due lati ·
spread massimo · prezzo minimo · deviazione del limite dal prezzo di riferimento · controvalore
minimo · incidenza commissioni · contante regolato · peso massimo della posizione · esposizione
lorda · numero di posizioni · liquidità (quota del controvalore medio giornaliero).

Uscite: quantità non superiore a quella detenuta (niente scoperto) e controllo del prezzo limite
solo se esiste una quotazione recente. Le uscite non vengono mai bloccate da kill switch, limiti di
perdita o restricted list.

## Sicurezza del lab con agenti

- Server MCP senza connessione al broker e senza strumenti d'ordine; unica scrittura:
  `propose_signal` (accoda e basta).
- La restricted list (elenco personale) non è esposta via MCP, log o git; `.claude/settings.json`
  ne nega la lettura agli agenti.
- Il testo dei filing è trattato come dato non affidabile (prompt injection) in tutti i prompt.
- Dal telefono (Telegram) si può fermare il sistema (`/kill`) ma non riavviarlo: la riattivazione
  avviene solo da CLI.
