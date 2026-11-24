# Runbook operativo

## 1. Prerequisiti

- Python 3.12+ e [uv](https://docs.astral.sh/uv/).
- `uv sync --all-extras` (installa anche `ib_async` per IBKR).
- `.env` creato da `.env.example`; `config/restricted_list.txt` creato dall'esempio.
- `TRADER_SEC_USER_AGENT` con nome e contatto: la SEC lo richiede per l'accesso automatico
  (limite di 10 richieste/secondo; il client si ferma a 5).

## 2. Interactive Brokers (fase paper)

1. Conto IBKR Ireland, IBKR Pro con tariffa **Tiered**, tipo **cash**, azioni frazionarie attive,
   stato dati **non professional**, W-8BEN compilato. Conversione EUR→USD una tantum.
2. Installa **IB Gateway** (stabile) sulla macchina che esegue il sistema; accedi con
   l'utente **paper**.
3. In IB Gateway: *Configure → Settings → API*: abilita le API socket, porta **4002** (paper),
   accetta solo connessioni da 127.0.0.1, disattiva *Read-Only API* solo quando sei pronto a
   inviare ordini paper.
4. `.env`: `TRADER_BROKER=ibkr`, `TRADER_IBKR_PORT=4002`, `TRADER_IBKR_ACCOUNT=<DU...>`.
5. `uv run trader doctor` deve mostrare `ibkr_connection: ok`.
6. Dati di mercato: senza abbonamento usa `TRADER_IBKR_MARKET_DATA_TYPE=3` (ritardati). Con dati
   ritardati il controllo di freschezza (15 minuti) potrebbe respingere le entrate: è voluto.
7. IB Gateway richiede un nuovo login periodico con autenticazione a due fattori (indicativamente
   settimanale): pianificalo.

Il passaggio al conto reale cambia solo porta (4001) e account, dopo aver superato i criteri
della fase 3 in [ROADMAP.md](ROADMAP.md).

## 3. Telegram (approvazione da telefono)

1. Crea il bot con @BotFather e copia il token in `TRADER_TELEGRAM_BOT_TOKEN`.
2. Scrivi un messaggio al bot, poi ricava il tuo `chat_id` con
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (campo `message.chat.id`) e impostalo in
   `TRADER_TELEGRAM_CHAT_ID`.
3. Comandi: `/status`, `/pending`, `/kill`. I pulsanti Approva/Rifiuta arrivano con ogni segnale.
   Il bot risponde solo alla tua chat.

## 4. Esecuzione continua

```bash
uv run trader run --interval 300
```

Ogni ciclo: comandi Telegram → acquisizione EDGAR (feed intraday + indice del giorno precedente
una volta al giorno) → generazione segnali → revisione e rischio → esecuzione degli approvati →
uscite → sincronizzazione dei fill. Su un server usa un servizio systemd con `Restart=on-failure`
e lo stesso utente che esegue IB Gateway.

## 5. Analisi LLM degli 8-K (facoltativa)

`.env`: `TRADER_LLM_ENABLED=true` e `ANTHROPIC_API_KEY`. Lo smistamento usa
`claude-haiku-5-5` sulle prime 20.000 battute; l'analisi completa usa `claude-opus-5-5` con
fallback lato server in caso di rifiuto. I filing oltre 400.000 battute vengono saltati e
registrati nell'audit, mai troncati. Ogni chiamata registra l'uso di token nell'audit.

## 6. Agenti Claude Code (lab)

Apri il repository con Claude Code: il server MCP `trader` (`.mcp.json`) e gli agenti in
`.claude/agents/` sono pronti. Esempi:

- "usa filings-analyst per esaminare gli 8-K di oggi e i cluster insider"
- "usa risk-guardian per rivedere le modifiche correnti"
- "usa ops-monitor per un controllo dello stato"

## 7. Emergenze

| Situazione | Azione |
|---|---|
| Comportamento anomalo | `/kill` da Telegram oppure `uv run trader kill on --reason "..."` |
| Posizione da chiudere a mano | Chiudila in IBKR; al ciclo successivo il piano di uscita viene rimosso |
| Audit non integro | `uv run trader audit verify` → fermati e indaga prima di riprendere |
| Riavvio | `uv run trader kill off --reason "..."` solo dopo aver capito la causa |

## 8. Fisco (bozza)

`uv run trader tax 2026` produce plusvalenze e minusvalenze in euro (cambio BCE del giorno,
costo medio ponderato, commissioni incluse) e una stima IVAFE con i periodi di detenzione per il
quadro RW. Da verificare con il commercialista. Non gestisce: riporto delle minusvalenze degli
anni precedenti, dividendi (quadro RM), operazioni societarie.
