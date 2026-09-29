# I sottotitoli come si vedono, e il player da chiavetta

*2026-09-19. HearAble 1.4.0, SF8008 su openATV 7.5.1, T9 Plus come appliance sul cavo
diretto. Campagna di leggibilità e chiusura del player da filesystem.*

Niente è stato toccato nel motore: ASR, C_tail1, chunk, right context, quantizzazione e
timeshift sono esattamente quelli di ieri. Tutto quello che cambia sta nell'ultimo strato,
quello che disegna.

## Il verdetto, per primo

```text
FINAL_UX_GATE: PASS
```

La scelta dell'aspetto è dello spettatore e l'ha fatta davanti al televisore:

```text
font_scale         1.20
line_gap           0.25
background         acceso, opacità 0.85
padding            0
```

## Com'era prima di toccarlo

| | |
|---|---|
| carattere | `Regular`, 33 px |
| riga | 1178 × 54 px, banda 1178 × 108 |
| posizione | 51,562 su un OSD di **1280×720** (non 1920×1080: l'OSD non è il video) |
| sfondo | `#00000000`, nero **pieno** |
| interlinea | implicita nel rapporto riga/carattere, 1,613 |

**La semantica dell'alpha, verificata e non assunta**, perché il mandato lo chiede e perché
è controintuitiva: il byte alto è **trasparenza**, non opacità. `0x00` è opaco e `0xff` è
invisibile. La conversione da un'opacità che una persona riconosce sta scritta in un punto
solo, `_alpha()`, e il resto del codice parla di opacità come si parla fra persone.

## Che cosa è configurabile adesso

`/etc/enigma2/hearable_look.json`, con limiti e ritorno ai valori di fabbrica se il file
dice qualcosa di strano:

| chiave | intervallo | che cos'è |
|---|---|---|
| `font_scale` | 0,8 – 1,6 | la dimensione del carattere |
| `line_gap` | 0,25 – 1,0 | l'aria fra le due righe, in frazioni del carattere |
| `background_enabled` | vero/falso | la banda nera |
| `background_opacity` | 0,0 – 1,0 | quanto è coprente |
| `padding` | 0,0 – 0,5 | margine attorno al testo dentro la banda |

Si cambiano dal control endpoint e **il pannello si ricostruisce senza riavviare enigma2**.
Non è una comodità da sviluppatore: confrontare due dimensioni di carattere aspettando un
riavvio fra l'una e l'altra non è un confronto, perché quando arriva la seconda nessuno
ricorda abbastanza bene la prima per preferirla.

## Le varianti, e quali reggono

| | carattere | sfondo | esito |
|---|---|---|---|
| A | 100% | pieno | valida |
| B | 110% | assente | **testo illeggibile su scene chiare** |
| C | 120% | assente | **testo illeggibile su scene chiare** |
| D | 120% | 45% | valida |
| E | 120% | 65% | valida |
| F | 120% | 85% | valida |
| G | 130% | 65% | valida |

`TECHNICALLY_VALID_VARIANTS: 5`. Le due senza sfondo non sono un difetto del codice: il
bianco su un cartone animato luminoso semplicemente non si legge, e la fotografia della
variante C lo mostra meglio di qualunque frase.

## Quaranta caratteri, la domanda del gate

```text
40_CHARS_AT_120_PERCENT: PASS
```

Due righe da **quaranta caratteri esatti**, costruite con le lettere più larghe e con i
discendenti che decidono se una dimensione ci sta:

```text
WWMM quell'ampio miglioramento degli xqW
AVVOLGIMENTO: perché già giù, prqjgyMWWW
```

Nessun taglio orizzontale né verticale, nessuna parola spezzata — **neanche al 130%**. La
larghezza utile è 1178 px su 1280, e il fondo della banda resta a 670/720: i 50 px di
margine inferiore non si sono mossi, perché la banda cresce verso l'alto.

## La scelta, un passo alla volta

Lo spettatore ha guardato e deciso, e l'agente non ha espresso preferenze:

1. 100% → «provane una più grande»
2. 110% → «ancora più grande»
3. 120% → «in generale va bene, **ma la distanza fra le righe è troppa**»

Quella richiesta ha prodotto il parametro che non era previsto dal mandato. L'aria fra le
righe era implicita: ogni riga stava in una scatola alta 1,613 volte il carattere, e il
vuoto era la somma di due mezze scatole. Diventata esplicita, si è dimezzata (0,30, 12 px)
e poi stretta ancora (0,25, 10 px), con il limite inferiore messo dove i discendenti
cominciano a toccarsi.

Poi lo sfondo: 85% → 65% → **ritorno a 85%**.

## I messaggi di lifecycle, con il carattere nuovo

| | |
|---|---|
| `UI_START_MESSAGE` | PASS |
| `UI_STOP_MESSAGE` | PASS — `HearAble 1.4.0 / Arrivederci` |
| `UI_CREDIT_ONLY_LIFECYCLE` | PASS — 39 campioni durante il parlato, zero con credito o versione |
| `UI_NO_STALE_AFTER_STOP` | PASS |

`NO_VISIBLE_BACKGROUND_RESIDUE: PASS` — dopo il clear la zona della banda ha **0,0%** di
pixel neri, misurato sul fotogramma e non a occhio.

## Il player da chiavetta

Tutte le prove sul T9, sul cavo privato, con due film veri.

| prova | ripresa | stati stantii |
|---|---:|---:|
| play → primo sottotitolo | 36,1 s | 0 |
| ripresa dalla pausa | 1,2 s | 0 |
| salto avanti | 19,1 s | 0 |
| salto indietro | **64,9 s** | 0 |
| cambio file | 49,9 s | 0 |
| F4 OFF → ON | 5,4 s | 0 |

`FILE_STALE_SUBTITLES: 0` sull'intera sessione. Dopo ogni discontinuità il testo che compare
appartiene alla nuova posizione, e questo non è dedotto dal fatto che il pannello sia
cambiato: il tempo di ripresa viene contato solo da quando l'epoch della sorgente è
avanzato.

`FILE_AUDIO_TRACK_CHANGE: NOT_TESTABLE` — nessuno dei film sulla chiavetta ha più di una
traccia audio. Sul DVB invece era testabile e passa (Rai 1 HD ne dichiara quattro).

Il salto indietro è il più lento di tutti, 65 secondi: il relay deve riposizionarsi su una
timeline che va all'indietro e il riconoscitore ricostruire il contesto. Lento, non
sbagliato.

## La pausa e lo spegnimento

Misurato prima di decidere, come chiede il mandato: **la pausa chiude la sessione audio**.
Il relay va in `paused`, i socket scendono a zero, in trenta secondi non arriva un solo
stato nuovo e il pannello resta fermo.

Conseguenza: il conto alla rovescia dello spegnimento parte anche durante una pausa
normale, e una pausa più lunga di venti minuti spegne il T9.

**Decisione esplicita dello spettatore: va bene così.** È coerente con «nessuno sta
guardando», e al ritorno F4 risveglia la macchina in una trentina di secondi. Niente è
stato cambiato nel watchdog, che resta basato sulla sessione audio e mai sul parlato.

## Le regressioni

| | |
|---|---|
| `DVB_REGRESSION` (dopo la grafica) | **PASS**, 12 controlli su 12 |
| `POST_FILE_DVB_REGRESSION` | **PASS**, 12 controlli su 12 |

Tuner libero, zero servizi persi, zero stantii nuovi, zero errori di trasporto.

## Quattro difetti, tutti nei miei strumenti

Nessuno nel sistema; tutti nel modo di misurarlo, e tutti avrebbero prodotto un rapporto
sbagliato.

1. **Il saluto contato come sottotitolo.** «Primo sottotitolo dopo 1,8 s» era il tempo di
   dipingere `HearAble · Avvio...`. Il valore vero è 36,1 s.
2. **Il tempo di ripresa dopo un salto misurato a 0,0 s**, perché il pannello stava ancora
   finendo la frase di prima. Ora si aspetta il nuovo epoch: 19,1 s.
3. **Una lettura fallita dello stato tornava come zero**, e la differenza fra un totale
   reale e quello zero ha trasformato «non è successo niente» in «2165 sottotitoli
   stantii». Adesso una lettura mancante si chiama `NON_MISURATO`.
4. **Lo strumento di confronto marcava le sue righe con epoch 9000**, e alla sua
   disconnessione la guardia sul box ha rifiutato 2165 stati veri finché un hello non l'ha
   azzerata. È l'origine di quel numero, ed è colpa dello strumento.

## Il blocco finale

```text
HEARABLE_VERSION: 1.4.0

CURRENT_FONT: Regular
BASE_FONT_SIZE: 33 px (100%)
TESTED_FONT_SCALES: 100, 110, 120, 130
FONT_100_RESULT: valida
FONT_110_RESULT: valida
FONT_120_RESULT: valida — scelta
FONT_130_RESULT: valida (esplorativa, 40 caratteri entrano)
40_CHARS_AT_120_PERCENT: PASS
PHYSICAL_WIDTH_LIMIT: 1178 px su un OSD di 1280
SAFE_AREA_RESULT: PASS, 50 px sotto la banda, invariati

BACKGROUND_IMPLEMENTATION: colore di sfondo del widget, non un rettangolo separato
BACKGROUND_ALPHA_SEMANTICS: il byte alto e' trasparenza (0x00 opaco, 0xff invisibile)
TESTED_BACKGROUND_LEVELS: 0, 45, 65, 85, 100
BACKGROUND_PADDING: 0

TECHNICALLY_VALID_VARIANTS: 5
USER_VISUAL_SELECTION: COMPLETED
FINAL_FONT_SCALE: 1.20
FINAL_LINE_GAP: 0.25
FINAL_BACKGROUND: acceso
FINAL_BACKGROUND_OPACITY: 0.85
FINAL_PADDING: 0

UI_START_MESSAGE: PASS
UI_STOP_MESSAGE: PASS
UI_GREETING_SURVIVES_THE_WAIT: PASS
UI_NO_STALE_AFTER_STOP: PASS

DVB_REGRESSION: PASS

FILE_PLAY: PASS (36.1 s)
FILE_PAUSE: PASS (chiude la sessione; deciso di lasciarlo cosi')
FILE_RESUME: PASS (1.2 s)
FILE_SEEK_FORWARD: PASS (19.1 s)
FILE_SEEK_BACKWARD: PASS (64.9 s)
FILE_CHANGE: PASS (49.9 s)
FILE_AUDIO_TRACK_CHANGE: NOT_TESTABLE (nessun film con piu' di una traccia)
FILE_F4_OFF_ON: PASS (5.4 s)
FILE_STALE_SUBTITLES: 0

POST_FILE_DVB_REGRESSION: PASS

ASR_CHANGED: NO
C_TAIL1_CHANGED: NO
CHUNK_CHANGED: NO
RIGHT_CONTEXT_CHANGED: NO
FILESYSTEM_READ_ONLY_CHANGED: NO
TIMESHIFT_CHANGED: NO

FINAL_UX_GATE: PASS
FIRST_REMAINING_FAILURE: nessuno
NEXT_ACTION: usarlo.
```

## Quello che non è stato misurato

* **Cambio traccia audio su file**: nessuno dei film sulla chiavetta ne ha più di una.
  `NOT_TESTABLE`, non un fallimento.
* **Flicker**: nessuno osservato, ma una fotografia non può dimostrarlo. `NOT_MEASURED` in
  senso stretto.
* **Latenza bocca→schermo sul percorso T9**: resta fuori campagna, come da mandato.
