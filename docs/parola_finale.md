# L'ultima parola di una frase

*1 ottobre 2026. Difetto osservato sul campo, diagnosticato sul campo, e
**non risolto**. Questo documento dice che cosa è, che cosa è stato provato, e
con quali numeri ogni tentativo è stato respinto.*

## Il sintomo

> A volte manca una parola alla fine di una frase. La parola compare soltanto
> quando inizia la frase successiva, che può arrivare anche alcuni secondi dopo.

Esempio dal campo: il pannello mostrava `«ok»` mancante, e `ok` è poi comparso
insieme a `«scommetto»`, che era la frase dopo.

## Perché succede

L'ultima parola viene **trattenuta di proposito** (`COMMIT_TAIL_HOLD_WORDS: 1`):
un riconoscitore in streaming riscrive la propria coda, e mostrare una parola
che poi cambia davanti a chi legge è peggio che mostrarla più tardi.

Esiste un meccanismo per rilasciarla comunque dopo `COMMIT_TAIL_FLUSH_MS`
(450 ms) di quiete. **Non ha mai funzionato**, per due difetti in fila.

### Primo difetto: il timer non scadeva mai — corretto

Il timer misurava «non è arrivato nessun evento» invece di «l'ipotesi ha smesso
di cambiare». Un riconoscitore cache-aware emette un risultato a ogni chunk
anche in silenzio, quindi quegli eventi identici ritimbravano la scadenza
all'infinito.

Corretto in `2ba2a8a`: la quiete si misura dall'ultimo **cambio** di ipotesi.

### Secondo difetto: il rilascio veniva rifiutato — diagnosticato

Con il timer riparato, la misura in campo su mezz'ora di televisione vera:

```text
flush dovuti   203
committati       1
rifiutati      202   →  DIFFERENT_WORD: 202  (il 100%)

ultimo rifiuto: committate 1346 · ipotesi 1347 · d'accordo fino a 4
                committato 'offer'  ·  ipotesi 'offermi.'
```

`force_ingest` pretende che **tutto** il prefisso già committato coincida ancora
con l'ipotesi del riconoscitore. Alla quinta parola della sessione il modello
aveva committato `offer` — una parola ancora a metà — e poi l'ha completata in
`offermi`. Da quel momento il prefisso non coincide più, e **non si ripara
mai**: l'unica cosa che azzererebbe il committer è `is_final`, che su questo
modello **non scatta mai** (0 volte su 4510 eventi).

Una sola parola mezza decodificata ha spento il rilascio per tutta la mezz'ora,
e dopo 1346 parole la discordanza era ancora alla quarta.

## Che cosa è stato provato

### Trattenere due parole invece di una — NO

`COMMIT_TAIL_HOLD_WORDS: 2`, misurato sulle due sessioni: **1 rilascio su 213**
e **1 su 277**. Nessun effetto. La revisione del modello *fonde* due token in
uno, quindi nessun valore di questo parametro protegge.

### Rilasciare a tempo, appendendo — PROVATO SUL CAMPO, RESPINTO

Il rilascio appende invece di pretendere il prefisso intero. Sulla riproduzione
sembrava ottimo: da 1 a 157 rilasci su 213, e **zero** parole già mostrate
riscritte.

In campo lo spettatore ha detto: **«è peggiorato e non ne vale la pena»**.

La misura che spiega il verdetto, fatta dopo: contando quante parole rilasciate
il modello poi **cambia**,

| soglia | rilasci | poi cambiate |
|---:|---:|---:|
| 450 ms | 186 / 240 | **55 % / 48 %** |
| 700 ms | 83 / 150 | 49 % / 43 % |
| 1000 ms | 62 / 105 | 36 % / 40 % |
| 2000 ms | 36 / 43 | 31 % / 26 % |
| 3000 ms | 25 / 33 | 20 % / 21 % |

Circa **una parola su due è sbagliata** alla soglia in uso. E alzare la soglia
non salva: a tre secondi è ancora una su cinque, e a quel punto la parola arriva
così tardi che aspettare la frase successiva non costa di più.

**Conclusione: «l'ipotesi ha smesso di cambiare» non è una prova che la parola
sia finita.** È un indizio debole, e tutta la famiglia di soluzioni basata sul
tempo eredita quella debolezza. Quella famiglia è chiusa.

### Rilasciare sulla punteggiatura — DA PROVARE

Rilasciare quando il modello ha messo un punto, un punto interrogativo o
esclamativo sulla parola trattenuta. Sulle stesse due sessioni:

| segnale | rilasci | poi cambiate |
|---|---:|---:|
| tempo, 450 ms | 186 / 240 | 55 % / 48 % |
| **punteggiatura** | **114 / 172** | **4,4 % / 0,0 %** |
| entrambi | 31 / 64 | 3,2 % / 0,0 % |

Cambia il **tipo di prova**: una pausa è un'osservazione sui tempi, un punto è
il modello che dichiara finita la frase. Ed è esattamente il caso del difetto,
perché riguarda l'ultima parola **di una frase**.

Riprodotto attraverso il bridge vero invece che con una simulazione: **84 e 172
rilasci, zero riscritture**.

## Lo stato oggi

```text
COMMIT_TAIL_RELEASE: "off" | "time" | "punctuation"
```

* **default della classe: `off`** — nessuna installazione cambia comportamento
  aggiornando;
* `time` è conservato solo perché il confronto si possa rifare, non perché
  serva;
* la chiave booleana precedente `COMMIT_TAIL_FLUSH_APPENDS` continua a essere
  letta e significa `time`, così una configurazione fatta prima non viene
  ribaltata in silenzio;
* le metriche di ogni corsa registrano quale modo le ha prodotte, così un
  numero non resta orfano della sua condizione.

Per cambiarlo servono una riga e un riavvio, senza aspettare nessuno:

```bash
# sul mini PC
nano hearable/config/led_subtitles.json     # "COMMIT_TAIL_RELEASE": "off"
sudo systemctl restart hearable-t9
```

## Quello che resta vero comunque

**In `off` il difetto c'è.** L'ultima parola di una frase aspetta la frase
successiva. È il comportamento spedito, scelto perché l'alternativa a tempo è
stata provata su televisione vera e giudicata peggiore.

**`punctuation` aiuta solo le frasi che il modello punteggia.** Dove non mette
il punto, il sintomo è identico a oggi.

**I numeri di questo documento vengono da riproduzioni** delle sessioni
registrate, e quella riproduzione coincide con le righe davvero dipinte **al
63,7 %**: sono indicazioni forti, non dimostrazioni. Il criterio di «parola
sbagliata» è a sua volta un surrogato — la parola a quell'indice risulta diversa
nell'ipotesi finale — e non cattura una parola giusta rilasciata al momento
sbagliato.

**Due cause non sono mai state escluse**: che la parola non venga mai prodotta
dal riconoscitore, o che venga prodotta e non mostrata per un motivo a valle del
committer. La diagnosi qui spiega i 202 rifiuti osservati; non dimostra che
siano l'unica causa del sintomo.

## Strumentazione

Ogni corsa scrive, nel blocco `tail_flush` delle sue metriche:

| campo | che cosa dice |
|---|---|
| `tail_flush_due` | quante volte il rilascio è stato dovuto |
| `tail_flush_committed` | quante volte ha prodotto parole |
| `tail_flush_refused` | quante volte no |
| `refusal_kinds` | perché, classificato |
| `last_refusal` | l'ultimo caso, con la parola committata e quella dell'ipotesi |
| `commit_tail_release` | quale modo ha prodotto questi numeri |

E alla **prima** occorrenza di ogni tipo di rifiuto compare una riga nel
journal: il log deve nominare un modo nuovo di fallire, non contare uno vecchio.
