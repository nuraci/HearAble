# Regressione FILE — il percorso da chiavetta dopo lo scheduler di pubblicazione

*2026-09-17T13:11:20Z. Octagon SF8008 V3 Supreme Combo, openATV 7.5.1 (kernel 4.4.35),
riproduzione da `/media/hdd/hearable_gate_1080p.mkv`, Nemotron 3.5 ASR Streaming 0.6B q8_0,
CPU, it-IT, C_tail1. Codice: v1.2.0, plugin sul box identico ai sorgenti per md5.*

Il gate `subtitle_sync_runtime_fkeys` aveva lasciato `FILE REGRESSION: NOT RUN`, non per un
problema tecnico ma perché prendeva possesso del televisore mentre lo spettatore lo stava
usando. Questa è quella corsa, fatta ora che il monitor è libero. **Senza antenna**: il
percorso FILE non ne ha bisogno, ed è per questo che si poteva fare oggi.

La domanda era una sola: la linea di ritardo introdotta per il DVB ha rotto qualcosa nel
percorso da file?

## Il verdetto, per primo

```text
FILE REGRESSION: PASS
FILE RELAY SMOKE: PASS
PAUSE/RESUME: PASS
SEEK: PASS
TRACK CHANGE: PASS
SYNC KEYS ON FILE SOURCE: REFUSED (comportamento voluto)
FILE SUBTITLE RUNTIME SYNC: NOT YET QUALIFIED (invariato)
```

## Come è stata fatta

300 s di catena reale con il file che suona sul decoder, e sette azioni a orari fissi
mandate al ricevitore dal telecomando virtuale. Niente di simulato: stessa build, stesso
modello, stesso stabilizzatore, stesso trasporto, stesso renderer della produzione.

| t | azione | che cosa mette alla prova |
|---:|---|---|
| 70 s | pausa | il lettore si accorge che l'immagine è ferma |
| 95 s | ripresa | riaggancio dopo 25 s di fermo |
| 140 s | salto avanti 60 s | riposizionamento per frame match |
| 190 s | traccia audio 0 → 1 (it → en) | cambio sorgente a metà corsa |
| 240 s | traccia audio 1 → 0 | ritorno |
| 265 s | F3 | i tasti di sincronia con sorgente FILE |
| 275 s | F3 | seconda pressione, per escludere il caso singolo |

## Che cosa ha fatto la catena  ·  MEASURED

| | questa corsa | baseline `real_e2e` |
|---|---:|---:|
| RTF di calcolo | **0,4661** | 0,4711 |
| chunk p95 | **77,26 ms** | 78,78 ms |
| chunk p99 | 91,09 ms | 94,53 ms |
| chunk max | 905,9 ms | 772,0 ms |
| chunk persi | **0** | 0 |
| RAM di picco | 1,142 GB | 1,143 GB |
| audio elaborato | 266,4 s in 305,2 s di corsa | 150,4 s in 152,7 s |

La baseline è più corta e non subiva sette azioni, quindi il confronto vale per quello che
è: la catena regge, non perde niente, e il tempo per chunk non è peggiorato.

## Che cosa ha fatto lo schermo  ·  MEASURED

| grandezza | valore |
|---|---:|
| stati generati dal produttore | 1341 |
| stati arrivati al renderer | 1340 (uno in volo alla chiusura) |
| ridipinture del pannello | 359 |
| ack di render | 353 |
| stati superati prima di essere dipinti | 30 |
| messaggi non validi | 0 |
| stati respinti per epoch vecchio | **0** |
| stati respinti per seq vecchio | **0** |
| errori di trasporto | 0 |
| latenza di ack p50 / p95 / max | 25,7 / 39,3 / 1575,6 ms |

Lo scheduler, con `publish_delay_ms = 0` come vuole il percorso FILE: coda 0, profondità
massima mai raggiunta, `dropped_overflow` **0**, 30 coalescenze — lo stesso numero degli
stati superati visto dall'altro capo, che è la coerenza che ci si aspetta fra i due
contatori.

Le 359 ridipinture per 1341 stati non sono stati persi: il pannello riscrive solo quando le
due righe cambiano davvero, ed è la stessa cadenza già misurata sul DVB.

## Le cinque discontinuità  ·  MEASURED

Ognuna delle cinque azioni sul ricevitore è stata rilevata dal lettore, che si è
riposizionato per frame match e ha aperto un nuovo epoch. L'epoch della sorgente sul box è
passato da 6 a 11: cinque azioni, cinque epoch.

| evento | rilevato a | lettore riavviato a | ritardo di riaggancio |
|---|---:|---:|---:|
| pausa | 52,5 s | — | confermata dopo 2,78 s di immagine ferma |
| ripresa | 74,6 s | 79,2 s | 4,6 s |
| salto (54,6 s di balzo) | 120,1 s | 122,6 s | 2,5 s |
| traccia 0 → 1 | 170,0 s | 172,5 s | 2,5 s |
| traccia 1 → 0 | 220,3 s | 222,8 s | 2,5 s |

Zero stati respinti per epoch vecchio significa che nessun sottotitolo della scena
precedente è sopravvissuto a una discontinuità — che è la cosa che le barriere dovevano
garantire e l'unico modo in cui lo scheduler nuovo avrebbe potuto rompere FILE.

Nota di lettura: fra 190 e 240 s l'audio era la traccia inglese e il riconoscitore girava
in `it-IT`. Il testo di quel tratto è privo di senso ed è corretto che lo sia; la prova
riguardava il riaggancio, non l'accuratezza.

## I tasti di sincronia con una sorgente FILE  ·  MEASURED

Questa parte non era nel mandato originale e l'ho aggiunta perché è il punto in cui il gate
precedente aveva trovato un difetto vero: 1500 ms scritti in `file_delay_ms` su disco senza
che nessuno li avesse misurati.

Con il file in riproduzione la chiave attiva diventa `file_delay_ms`, e `adjust_sync` si
rifiuta di toccarla, mostrando `HearAble · sync FILE — non ancora qualificato`.

| | prima | dopo due pressioni di F3 |
|---|---:|---:|
| tasti visti dal plugin | 62 | 69 |
| tasti consumati da HearAble | 60 | **62** |
| `file_delay_ms` | 0 | **0** |
| scritture su `/etc/enigma2/hearable_sync.json` | 21 | **21** |

Il tasto viene consumato — quindi non finisce a un'altra applicazione — ma non muove
niente e non scrive niente. `lead_ms` resta un anticipo di lettura e mantiene la sua
semantica: `FILE SUBTITLE RUNTIME SYNC` resta `NOT YET QUALIFIED`, come deve.

## Quello che non è stato misurato

* **Accuratezza**: nessun WER. Il fixture viene riprodotto da posizioni diverse a ogni
  corsa e metà del tratto centrale è in inglese, quindi un confronto con il riferimento
  non sarebbe onesto. `NOT_MEASURED`.
* **Regressione Desktop**: sempre `NOT RUN`. Apre una finestra sul PC dello spettatore.
* **Un secondo riaggancio dopo un salto all'indietro**: lo script provava solo un salto in
  avanti. `NOT_MEASURED`.

## Una cosa da tenere d'occhio

La latenza di ack ha un massimo di 1575,6 ms contro un p95 di 39,3, e il sink segna una
riconnessione (`reconnects: 1`, `connections_accepted: 2` dall'altro capo). Un singolo
episodio su 353 ack, senza errori di trasporto né di protocollo e senza stati persi: non è
un difetto dimostrato, ma è l'unico numero della corsa che non somigli a tutti gli altri, e
va riguardato alla prossima occasione.
