# trader

Sistema di trading **per conto proprio** su azioni USA (conto personale, residente in Italia):

- acquisisce i filing **SEC EDGAR** (Form 4 degli insider, 8-K) con timestamp *point-in-time*;
- genera segnali da regole quantitative (cluster di acquisti insider) e da **analisi LLM** degli 8-K;
- passa ogni idea per un **motore di rischio deterministico** e per l'**approvazione umana**
  (CLI o Telegram) prima di inviare ordini al broker (simulato o **Interactive Brokers**);
- gestisce stop-loss e uscite a scadenza, tiene un **registro di audit** a catena di hash e una
  **bozza fiscale italiana** (quadro RT con cambi BCE, stima IVAFE/RW);
- espone un **server MCP** e quattro **agenti Claude Code** per la ricerca.

> Software sperimentale. Il denaro investito può essere perso del tutto. Nessun output del sistema
> è consulenza finanziaria o fiscale. La parte fiscale è una bozza da far verificare al commercialista.

## Avvio rapido (broker simulato)

```bash
uv sync --all-extras
cp .env.example .env            # imposta almeno TRADER_SEC_USER_AGENT
cp config/restricted_list.example.txt config/restricted_list.txt
uv run trader doctor            # verifica configurazione
uv run trader ingest day 2026-11-23
uv run trader signals generate --no-llm
uv run trader sim quote GNL 8.20 --adv 40000000   # quotazione di prova nel simulatore
uv run trader cycle --no-ingest --no-generate     # dimensionamento + rischio -> in attesa di approvazione
uv run trader signals list --status PENDING_APPROVAL
uv run trader approve <ID>
uv run trader cycle --no-ingest --no-generate     # esecuzione
uv run trader status
```

Il ciclo opera solo durante la sessione regolare NYSE (09:30–16:00 New York).

## Flusso di un segnale

```
Fonte (insider / LLM / agente MCP)
  -> PROPOSED -> dimensionamento -> motore di rischio (21 controlli)
  -> PENDING_APPROVAL -> [umano: Approva / Rifiuta] -> APPROVED
  -> ricontrollo rischio con dati freschi -> ordine limite -> fill
  -> piano di uscita (stop, scadenza) -> uscita automatica (sempre consentita)
```

## Comandi principali

| Comando | Cosa fa |
|---|---|
| `trader doctor` | Controlla configurazione e connessioni |
| `trader ingest latest` / `ingest day YYYY-MM-DD` | Acquisizione EDGAR (feed intraday / indice giornaliero) |
| `trader signals generate [--llm]` | Genera e accoda i segnali |
| `trader signals list/show` | Coda dei segnali |
| `trader approve ID` / `reject ID` | Decisione umana |
| `trader cycle` | Un ciclo completo |
| `trader run --interval 300` | Esecuzione continua con bot Telegram |
| `trader status` | Equity, posizioni, piani di uscita, kill switch |
| `trader kill on/off` | Kill switch (blocca le entrate, non le uscite) |
| `trader audit verify` | Verifica dell'integrità del registro |
| `trader tax 2026` | Bozza fiscale (RT, IVAFE/RW) |
| `trader backtest --prices DIR START END` | Backtest del segnale insider |

## Documentazione

- [docs/ARCHITETTURA.md](docs/ARCHITETTURA.md): componenti, scelte e invarianti di sicurezza
- [docs/RUNBOOK.md](docs/RUNBOOK.md): messa in esercizio (IBKR paper, Telegram, server, LLM)
- [docs/ROADMAP.md](docs/ROADMAP.md): fasi, criteri di passaggio, limiti noti
- [CLAUDE.md](CLAUDE.md): regole per Claude Code e per gli agenti del lab

## Sviluppo

```bash
uv run pytest                                  # test (live: TRADER_LIVE_TESTS=1)
uv run ruff check src tests && uv run ruff format --check src tests
uv run mypy                                    # strict
```
