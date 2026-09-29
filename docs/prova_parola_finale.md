# Quando l'hardware torna: provare la parola finale

*29 settembre 2026. Branch `usb-mkv-gate`, HEAD `2b0e791`, tag `v1.5.1`.
Scritto prima della prova, con decoder e antenna scollegati.*

## Che cosa c'è di pronto

Sì: **la correzione è fatta, testata offline e committata.** Non è però ancora
una versione nuova, ed è voluto — vedi «Perché non ho ancora messo il tag».

```text
la correzione   2ba2a8a  "Release the held last word on silence, not on the
                          next sentence"
dove            hearable/realtime_server.py — SOLO lato mini PC
il decoder      non cambia: nessun plugin da reinstallare, nessuno schermo nero
suite           246 test verdi, di cui 4 nuovi su questo difetto
```

**Il difetto era questo.** L'ultima parola di una frase viene trattenuta di
proposito, e dovrebbe uscire dopo 450 ms di quiete. Ma «quiete» era implementata
come «non è arrivato nessun evento», mentre un riconoscitore in streaming emette
un risultato a ogni chunk **anche mentre nessuno parla**. Quegli eventi identici
ritimbravano il timer all'infinito: la scadenza non arrivava mai e la parola
aspettava la frase successiva. Ora «quiete» significa «l'ipotesi ha smesso di
cambiare».

## I due comandi, quando il T9 è acceso

```bash
tools/t9_deploy.sh              # copia i sorgenti e riavvia la catena
tools/t9_last_word_gate.py      # 180 s di diretta, poi il verdetto
```

Il T9 si spegne da solo quando nessuno guarda, quindi potrebbe non rispondere.
Non è un guasto: `t9_deploy.sh` se ne accorge e dice come svegliarlo (F4 sul
telecomando, oppure il pulsante).

### Che cosa fa il deploy, e che cosa controlla

Copia `hearable/ config/ scripts/ systemd/ tools/` — **non** il modello (707 MB,
è già lì), **non** `upstream/` (è una directory di build), **non** i risultati
dei benchmark, che li scrive il T9 e sovrascriverli distruggerebbe le prove
della corsa precedente.

Poi riavvia `hearable-t9` e **verifica invece di fidarsi**: confronta l'md5 di
`realtime_server.py` sulle due macchine, controlla che il servizio sia `active`,
e controlla che il pid sia **cambiato**. Un riavvio che dichiara successo mentre
il vecchio codice è ancora caricato è il modo di fallire che vale dieci righe di
script — ci siamo già cascati con l'installazione del plugin.

### Che cosa misura il gate, e che cosa no

Ogni parola rilasciata dal timer porta `reason: "TAIL_FLUSH_TIMEOUT"` nel suo
evento di commit, e quegli eventi finiscono nel file della corsa. Con il difetto
presente quel motivo **non compare mai**, perché la scadenza non si raggiunge.
Con la correzione compare una volta per pausa.

| verdetto | significa |
|---|---|
| `PASS` | almeno 3 rilasci per scadenza: il timer scatta durante le pause |
| `FAIL` | zero rilasci in tutta la corsa: la parola aspetta ancora la frase dopo |
| `NON_MISURATO` | troppo pochi rilasci, o nessun evento: la corsa non dice niente |

La soglia di 3 c'è perché una corsa senza pause non produce prove, e dichiarare
`PASS` su zero osservazioni è come una guardia diventa decorazione.

**Il gate misura il meccanismo, non la qualità.** Se dà `PASS` ma guardando la
televisione le frasi finiscono ancora corte, il difetto è altrove e va ripresa l'indagine — probabilmente su
CASE B (parola mai prodotta dall'ASR) o CASE C (prodotta e non mostrata), che
questa campagna **non ha escluso**.

Il giudice è stato provato offline sui tre esiti, con file di eventi costruiti
apposta. Il deploy no: la sua strada felice non è mai stata percorsa, perché
serve il T9 acceso.

## Perché non ho ancora messo il tag

La versione è ancora `1.5.1` di proposito. Alzarla a `1.5.2` obbliga a
reinstallare anche il plugin sul decoder — il test sulla coerenza delle versioni
lo pretende, e la riga di commiato mostrerebbe altrimenti un numero sbagliato.
Vuol dire **due cambiamenti nello stesso momento**, proprio mentre si prova se
uno dei due ha funzionato.

Quindi l'ordine giusto è:

```text
1. deploy della sola correzione sul mini PC
2. guardare la televisione e giudicare
3. se regge -> versione 1.5.2, tag, plugin reinstallato
```

Così il numero di versione vorrà dire «confermato sull'hardware» e non
«sperato». Se preferisci il contrario basta dirlo: è una riga.

## Che cosa guardare con gli occhi

Il gate conta gli eventi; la prova vera la fai tu. Quello che deve succedere:

* una frase **finisce completa**, compresa l'ultima parola, durante la pausa;
* la frase successiva **comincia con la propria prima parola**, non con quella
  rimasta indietro;
* niente di peggio di prima: la punteggiatura, le maiuscole, le due righe da 40
  caratteri e il look devono essere identici.

Quell'ultimo punto è la lista di regressioni del mandato forense (§17). La
correzione tocca solo la semantica di un timer, quindi non dovrebbe sfiorarle —
ma «non dovrebbe» non è una misura.

**Un rischio piccolo, dichiarato.** Rilasciando la coda prima, una parola che
l'ASR rivedesse *dopo* i 450 ms verrebbe committata e la revisione rifiutata in
blocco. Non è un comportamento nuovo — succedeva già quando il timer funzionava
— ma da ora si esercita più spesso. Se vedi una parola sbagliata che resta
sbagliata, è questo.

## Lo stato di tutto il resto

| | |
|---|---|
| decoder | plugin 1.5.1, look `font_scale 1.2 · line_gap 0.25 · opacity 0.85` |
| mini PC | spento; si sveglia con F4 in ~32 s |
| indirizzi | decoder sulla **wifi** (`192.168.1.244` il 19/9), T9 `192.168.1.30`; link privato `10.77.0.1` ↔ `10.77.0.2` |
| antenna | provvisoria, da rimettere — è il motivo per cui la prova aspetta |
| albero | pulito, tutto committato |
