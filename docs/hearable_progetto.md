# HearAble — sottotitoli per il televisore che hai già

*Sottotitolazione italiana in diretta · DVB-T2 · tutto in casa. Versione 1.5.1,
settembre 2026. Orchestrato da Nunzio Raciti, scritto da Claude (Opus) sotto la
sua direzione — vedi [Come è stato costruito](#10--come-è-stato-costruito).*

HearAble mette **i sottotitoli in italiano sulla televisione in diretta**, circa
un secondo dopo la voce, per uno spettatore che ci sente poco. Gira su un
ricevitore terrestre di consumo e su un mini PC senza ventola, collegati da un
cavo privato. Niente esce di casa, non c'è niente da sottoscrivere, e **il
telecomando continua a funzionare**.

| | |
|---|---:|
| Ritardo del sottotitolo rispetto alla voce | ~1,0 s |
| Fattore di tempo reale, trenta minuti di diretta | 0,4848 |
| Audio sul filo | 26 kB/s |
| Sintonizzatori consumati | 0 |

Ogni cifra di questo documento è una misura presa sull'hardware vero, oppure è
dichiarata come non misurata.

---

## 1. A che cosa serve

Le emittenti italiane sottotitolano alcuni programmi e altri no. Il telegiornale
in diretta, le finestre regionali, lo sport, i talk show, quasi tutto il
pomeriggio: arrivano senza sottotitoli, oppure con sottotitoli così in ritardo da
descrivere una frase diversa da quella sullo schermo. Per chi ci sente poco è la
differenza fra guardare la televisione e guardare delle persone che muovono la
bocca.

HearAble nasce esattamente per quel caso: **sottotitolare quello che c'è adesso,
in italiano, sul televisore stesso** — non su un telefono, non su un tablet
appoggiato allo schermo, e senza chiedere allo spettatore di imparare niente
oltre a un tasto.

Tre vincoli hanno deciso tutto quello che viene dopo.

- **Non deve rompere il televisore.** Il decoder ha un solo sintonizzatore
  terrestre. Se sottotitolare costa un sintonizzatore, o si prende un tasto del
  telecomando, o fa sparire l'immagine, lo spettatore ci perde più di quanto ci
  guadagni.
- **Deve girare in casa.** Nessun servizio di riconoscimento nel cloud, nessun
  account, nessun caricamento di quello che una persona guarda nel suo salotto.
- **Si deve poter usare premendo un tasto.** Lo spettatore preme `F4`. Tutto il
  resto — svegliare il riconoscitore, collegarsi, smontare tutto, spegnere dopo —
  è un problema del sistema.

```
┌────────────────────────────────┐
│        By Nunzio Raciti        │
│      HearAble · Avvio...       │
└────────────────────────────────┘
```

*Il saluto, come appare sul televisore. Quattro secondi, poi il primo sottotitolo
vero lo sostituisce.*

---

## 2. La prima domanda: da dove viene l'audio?

Tutto quello che sta a valle è ordinario. Tirare fuori l'audio pulito del
programma da un decoder chiuso, senza pagare pegno, è la parte che è stata
inventata.

| Strada | Esito | Perché |
|---|---|---|
| Microfono vicino al televisore | SCARTATA | Rumore della stanza, e il fischio dell'apparecchio acustico dello spettatore stesso. Daremmo al riconoscitore la versione peggiore di un segnale che dentro il box esiste perfetto. |
| Estrattore audio HDMI | SCARTATA | Hardware in più in salotto, e l'audio arriva già decodificato e ricodificato — cioè dopo il buffer di uscita del decoder, che è proprio il ritardo che stiamo cercando di non pagare. |
| Streaming Enigma2 sulla porta 8001 | SCARTATA | Funziona, e **trattiene il sintonizzatore**. Cambiando canale verso un altro multiplex il box restava senza servizio. Misurato, non supposto. |
| **Filtro PID sul demux DVB** | **SCELTA** | Leggere il PID audio direttamente dal demux del servizio *già sintonizzato*. Non costa un sintonizzatore, non ricodifica niente, e arriva prima dell'immagine. |

### Il tap sul demux

Il ricevitore monta uno stack DVB Hisilicon ed espone l'API DVB standard di Linux.
Enigma2 tiene aperti ventiquattro filtri PID su `demux0` mentre mostra un canale;
quello di HearAble è il venticinquesimo. È solo userspace — nessun modulo kernel,
nessuna immagine modificata, niente di persistente.

```c
/* tutto il meccanismo, in cinque chiamate */
open("/dev/dvb/adapter0/demux0", O_RDWR | O_NONBLOCK)
DMX_SET_BUFFER_SIZE   1 MiB
DMX_SET_PES_FILTER    pid    = <il PID audio scelto dallo spettatore>
                      input  = DMX_IN_FRONTEND
                      output = DMX_OUT_TSDEMUX_TAP
                      flags  = DMX_IMMEDIATE_START
read()                /* pacchetti MPEG-TS da 188 byte */
```

Le tre modalità di uscita sono state provate sul box vero invece che dedotte
dall'header, e la differenza conta:

| Modalità | Esito |
|---|---|
| `DMX_OUT_TS_TAP` | Si apre senza errori e consegna **zero byte** — manda al device `dvr`, non a questo descrittore |
| `DMX_OUT_TAP` (PES) | Funziona, 127,3 kbit/s, primo dato dopo 0,27 s |
| `DMX_OUT_TSDEMUX_TAP` | Funziona, 132,8 kbit/s, primo dato dopo **0,03 s** — **scelta** |

**Perché `DMX_IN_FRONTEND` è tutto il trucco.** Il filtro legge il flusso che
*entra* nel box, non quello che il decoder sta presentando. HearAble sta quindi
**davanti alle orecchie dello spettatore**, e ogni millisecondo di quel vantaggio
è un millisecondo regalato al riconoscitore. È anche il motivo per cui immagine e
sottotitoli non possono separarsi per una ragione nostra: nel percorso
dell'immagine non ci siamo proprio.

---

## 3. Il percorso, comprese le parti finite male

Cinque campagne. Due sono finite contro una porta chiusa, e sono proprio quelle
due a spiegare perché il sistema che gira oggi è fatto così.

### Baseline su PC — dimostrare il riconoscitore prima di costruirci intorno

Nemotron 3.5 ASR Streaming 0.6B, quantizzato `q8_0`, su NeMo-Speech.cpp. Su uno
Xeon da scrivania trascrive l'italiano a **RTF 0,476** con un ritmo realistico e
un tasso di errore sulle parole mostrate del **4,74 %**.

Una Radeon RX 6600 via Vulkan è più veloce in pura portata, ma **sotto ritmo** —
l'unica modalità che conti per l'audio dal vivo, perché la GPU non può
sovrapporre lavoro che non ha ancora ricevuto — il suo vantaggio crolla da 5,7×
a 2,2×. È quella singola osservazione il motivo per cui l'appliance che è andata
in produzione non ha nessuna GPU.

### Arduino UNO Q — CHIUSA

Una scheda da cinquanta euro può ospitare il riconoscitore? No, ed è valsa la
pena di misurarlo in cinque modi per esserne sicuri.

| Percorso | Esito | Numero decisivo |
|---|---|---|
| CPU, build nativa | NO-GO | RTF 2,3146 |
| Vulkan, capability e build | PASS | 57,9 MB di shader, zero errori |
| Vulkan, esecuzione | bloccato | 0,267 GFLOP/s, poi rifiuto del driver |
| GPU misurata da sola | SCARSA | 2,1 GFLOP/s, banda 3,43 GB/s |
| DSP / Hexagon / QNN | non esiste | nessun dominio CDSP nel SoC |
| CPU, fino in fondo | margine insufficiente | serve 7,33×, la fisica ne concede 2,79 |

Il numero di chiusura è quello onesto: per arrivare al tempo reale serve
**7,33×**, e la fisica del bus di memoria ne concede **2,79×**. Amdahl non ha
nemmeno avuto la sua occasione: ha chiuso prima la banda.

Due cose però sono sopravvissute, e contano: gli stessi sorgenti compilano sulla
scheda con il suo gcc, e la trascrizione è *identica bit per bit a quella del PC*
— le stesse 74 parole, **WER 0,00 %**. Architettura diversa, compilatore diverso,
kernel SIMD diversi, e non si muove una lettera.

### La correzione dei 64 millisecondi

La catena tratteneva i sottotitoli per 1,5 s, perché una misura fatta da uno
screenshot diceva che il tap correva **2,4 s** davanti all'immagine. Chiedendolo
al decoder direttamente — `AUDIO_GET_PTS` contro il PTS del pacchetto che il
filtro PID consegna *nello stesso istante* — vengono fuori **64 ms**, con uno
scarto di 32 ms su cento campioni.

Il ritardo di pubblicazione è andato a zero e ci è rimasto. L'orecchio dello
spettatore lo aveva detto prima dello strumento.

### T9 Plus, Intel N95 — l'appliance che è andata in produzione

Un mini PC senza ventola a metà del tempo reale, su un cavo privato, svegliato dal
decoder e spento da sé. `T9_HEARABLE_GO`. Mezz'ora di torture deliberate con
quindici interventi: zero chunk persi, zero sottotitoli stantii. Diciannove guasti
iniettati, diciannove riprese senza che nessuno toccasse una tastiera.

### Ritardo A/V — CHIUSO

Si poteva ritardare il televisore invece di affrettare i sottotitoli? L'idea:
trattenere l'immagine di circa un secondo perché i sottotitoli la raggiungano,
mentre HearAble continua a leggere il segnale live.

**Il meccanismo funziona.** Con il timeshift attivo il tap resta a 0,072 s dal
live mentre il decoder presenta un'immagine in buffer 51,36 s indietro, e il
ritardo si può impostare dal plugin posizionandosi sul bordo live meno la
quantità voluta.

È stato chiuso lo stesso, da una misura presa su due minuti invece che su dieci
secondi:

| Quando | Ritardo |
|---|---:|
| Subito dopo la seek (chiesti 2000 ms) | 0,577 s |
| ~30 s dopo | 0,225 s |
| ~1 min dopo | **0,065 s** — il bordo live |

**Il ritardo si svuota da solo fino al live.** La tabella precedente, che lo dava
«stabile a 48 ms», aveva misurato il *raggiungimento* del ritardo, non la sua
*tenuta*: ogni riga era una finestra di dieci secondi.

### L'abitudine che ha prodotto quasi tutte le scoperte

Ogni difetto che conta, in questo progetto, è stato trovato facendo la cosa vera e
non la sua imitazione comoda. Staccare il cavo ha trovato un socket morto che
`ip link down` non riproduceva. Premere il telecomando ha trovato due difetti che
una matrice da diciannove casi aveva dichiarato verdi. Togliere la corrente ha
risposto a una domanda sul Wake-on-LAN a cui mesi di spegnimenti via software non
potevano rispondere. Le misure valgono quanto la cosa su cui sono prese.

---

## 4. Come sta insieme

Due macchine, un cavo privato, due piccoli protocolli che vanno in direzioni
opposte.

```
   DVB-T2
     │
     ▼
┌──────────────────────────────┐          ┌──────────────────────────────┐
│  DECODER · SF8008            │          │  MINI PC · INTEL N95         │
│                              │          │                              │
│  frontend1 ──▶ demux0        │          │  decodifica AAC + resample   │
│                  │           │          │            │                 │
│                  ▼           │          │            ▼                 │
│              tap sul PID     │          │  Nemotron 0.6B q8_0          │
│              (+64 ms)        │          │  RTF 0,4848                  │
│                  │           │          │            │                 │
│                  ▼           │          │            ▼                 │
│               relay ─────────┼──────────┼─▶ stabilizzatore → due righe │
│              (ffmpeg)        │ 26 kB/s  │            │                 │
│                              │  AAC     │            │                 │
│  pannello OSD ◀──────────────┼──────────┼────────────┘                 │
│  (due righe) │               │ subtitle │   WebSocket                  │
└──────────────┼───────────────┘  _state  └──────────────────────────────┘
               ▼
          televisore            cavo privato · 10.77.0.0/24
                                niente router, niente DHCP, niente gateway
```

L'audio esce dal decoder compresso e non torna mai indietro; rientrano soltanto
due righe di testo. Il televisore è alimentato dal decoder esattamente come è
sempre stato: HearAble disegna *sopra* l'immagine e non sta mai nel suo percorso.

La divisione è voluta. Il decoder è un apparecchio chiuso con un kernel 4.4 e non
ha spazio per un modello di riconoscimento; il mini PC ha la potenza ma non sa
disegnare sul televisore. Ciascuno fa quindi l'unica cosa che può: il box
preleva e dipinge, il mini PC ascolta e riconosce.

---

## 5. Il lato decoder

Un plugin Enigma2, circa duemila righe, che fa quattro mestieri: preleva, rilancia
l'audio, dipinge, e sveglia l'altra macchina.

| | |
|---|---|
| Hardware | Octagon SF8008 V3 Supreme Combo (Hisilicon), un sintonizzatore DVB-T2/C su `frontend1`, un DVB-S2X su `frontend0` |
| Immagine | openATV 7.5.1, Linux 4.4.35, Enigma2 |
| Plugin | `/usr/lib/enigma2/python/Plugins/Extensions/HearAbleOSD`, caricato a `WHERE_SESSIONSTART` |
| In ascolto su | `:8770` — `/hearable` (WebSocket, sottotitoli in ingresso) e `/control` (HTTP, controllo del relay) |
| Stato persistente | `/etc/enigma2/hearable_look.json`, `hearable_sync.json`, `hearable_wol.json` |

### Disegnare su un televisore

Il pannello è una skin Enigma2 costruita a runtime, e quasi ogni sua riga
racchiude un difetto trovato su uno schermo vero.

**Il byte alto è la trasparenza.** I colori di Enigma2 sono `#AARRGGBB` dove `AA`
è la *trasparenza*, non l'opacità. Il bianco scritto `#ffffffff` è la definizione
che la skin dà di «invisibile»: i glifi venivano disegnati trasparenti e
prendevano il colore di quello che avevano dietro — verdi sopra la barra verde,
blu sopra quella blu. Il bianco è `#00ffffff`.

**La finestra è la banda, non lo schermo.** Una finestra a schermo intero e
trasparente sembra identica e si comporta in modo diverso: si porta via il
telecomando. Il volume continuava a funzionare, perché è gestito globalmente, ma
`MENU` non faceva niente — il televisore sembrava rotto. Ora la finestra è grande
esattamente quanto ciò che disegna.

Il pannello è due righe di testo bianco centrato su una banda nera all'**85 % di
opacità**, ciascuna alta circa il 7,5 % dello schermo, attaccate, appoggiate al
7 % dal bordo inferiore per restare dentro l'area sicura a qualsiasi dimensione.
Quella geometria viene dalla pratica televisiva, non da quello che ci stava: si
legge come un blocco solo dall'altra parte della stanza. La tela dell'OSD è
**1280 × 720** anche su un servizio HD, ed è una trappola che va detta: disporre
la banda in coordinate 1080p la manda sotto il bordo dello schermo.

#### Che cosa può cambiare lo spettatore — `/etc/enigma2/hearable_look.json`

| Chiave | Intervallo | In uso | Effetto |
|---|---:|---:|---|
| `font_scale` | 0,8 – 1,6 | 1,2 | Dimensione del carattere, con la scatola dimensionata *dopo* il testo, così le discendenti non perdono la coda |
| `line_gap` | 0,25 – 1,0 | 0,25 | Distanza fra le due righe. Dimezzata due volte su indicazione dello spettatore |
| `background_opacity` | 0,0 – 1,0 | 0,85 | Opacità della banda. Sotto ~0,8 il bianco sparisce dentro un'immagine chiara |
| `background_enabled` | bool | true | Banda accesa o spenta del tutto |
| `padding` | 0,0 – 0,5 | 0,0 | Spazio in più dentro ogni riga |

Una modifica si applica subito: il dialogo viene distrutto e ricreato, invece di
richiedere un riavvio di Enigma2 che farebbe sparire l'immagine.

### Il telecomando

Tre tasti, e nient'altro tolto a Enigma2.

| Codice | Tasto | Che cosa fa |
|---:|---|---|
| 62 | `F4` | HearAble acceso / spento. All'accensione manda anche il magic packet che sveglia il mini PC |
| 61 | `F3` | Ritardo di pubblicazione in su, 100 ms per pressione |
| 60 | `F2` | Ritardo di pubblicazione in giù |

**HearAble parte spento a ogni avvio.** Lo stato non è persistito di proposito: i
sottotitoli sono una cosa che si chiede, e un box che torna da un black-out già
disegnando sullo schermo è un box che ha deciso al posto dello spettatore.

```
┌────────────────────────────────┐
│        HearAble 1.5.1          │
│          Arrivederci           │
└────────────────────────────────┘
```

*Allo spegnimento. La versione compare qui perché è l'unico posto dove uno
spettatore senza terminale può leggerla.*

### Il relay: mandare l'audio senza mandare il programma

Un progetto precedente mandava tutto il contenitore — video compreso — e chiedeva
alla macchina ricevente di tenere una copia identica bit per bit del file, così
da ricostruire la posizione del decoder confrontando un fotogramma preso dal suo
schermo. È insostenibile su una macchina piccola, e inutile per la diretta.

Il relay manda invece **solo la traccia audio scelta, ancora compressa**: 26 kB/s
di MPEG-TS prodotti da un processo `ffmpeg` fuori da Enigma2, così un ricevitore
lento o assente non può mai bloccare l'interfaccia.

- **È il relay a connettersi verso di noi.** Il mini PC sta in ascolto e si
  annuncia nel suo poll di controllo. È questo che permette al flusso di partire
  esattamente da dove si trova lo spettatore, ed è il motivo per cui un
  ricevitore assente non costa niente al decoder: nessun processo avviato,
  nessuna porta tenuta aperta.
- **Il flusso dice da dove comincia.** `-c copy` sa posizionarsi solo su un
  confine di contenitore, quindi il relay parte circa 0,65 s *prima* della
  posizione chiesta. I primi pacchetti di ogni epoca vengono sondati per il loro
  timestamp e l'eccesso viene scartato lato ricevitore: entrambe le parti
  conoscono un timestamp vero, quindi la differenza si butta via invece di
  correggerla con un anello di retroazione.
- **Un ricevitore che smette di chiedere sparisce** dopo un TTL di 8 s, e il
  relay si ferma da solo.

### Svegliare l'altra macchina

Premere `F4` manda un magic packet Wake-on-LAN sul link privato — in broadcast su
`10.77.0.255`, porte 9 e 7, ripetuto fino a quindici volte ogni venti secondi, che
dà una finestra di cinque minuti. Tempo di risveglio misurato da spento via
software: **32,0 s**, tre cicli su tre.

Per un po' il pacchetto era dimostrato in un banco di prova e il plugin non lo
mandava mai. Il primo uso vero se ne è accorto subito. Ora fa parte
dell'accensione di HearAble, e anche il contrario è gestito: spegnere il decoder
manda un datagramma di commiato, così il mini PC non aspetta la scadenza del suo
timer di inattività.

---

## 6. Il lato mini PC

Un box N95 senza ventola in cui nessuno entra: si sveglia quando glielo si chiede,
riconosce, e si spegne da solo quando il televisore smette di parlare.

| | |
|---|---|
| Hardware | T9 Plus, Intel N95 — 4 core / 4 thread, 800 MHz – 3,40 GHz, L1d 128 KiB · L2 2 MiB · L3 6 MiB, 15 GiB di RAM |
| SIMD | `sse4_2`, `avx`, `avx2`, `fma`, `f16c`, `bmi2`, `avx_vnni` |
| Sistema | Debian 13 trixie, Linux 6.12.107 |
| Modello | Nemotron 3.5 ASR Streaming 0.6B, `q8_0`, 741 548 352 byte, sha256 `a5c435f2…f429ae` |
| Runtime | NeMo-Speech.cpp a `4f96762`, ggml, solo CPU — nella macchina non c'è GPU e non ne serve |
| Rete | `enp1s0` = `10.77.0.2` statico, cavo privato verso il decoder. Wi-Fi solo per la manutenzione |

### Che cosa l'ha resa davvero veloce

Nessuna riga di codice di calcolo è stata ottimizzata per questa macchina. La
velocità è venuta da una riga di sysfs.

| Condizione | RTF |
|---|---:|
| Baseline, governor `powersave` | 0,6741 |
| Governor `performance` | 0,4896 |
| Dieci minuti di televisione vera | 0,4962 |
| Trenta minuti di torture deliberate | 0,4848 |

*Più basso è più veloce; 1,0 è il limite della sostenibilità.*

**Il governor vale il 27,6 % del fattore di tempo reale.** Il riconoscitore
lavora a raffiche — circa 105 ms di calcolo ogni 160 — e `intel_pstate` in
`powersave` non vede mai una raffica abbastanza lunga da salire di frequenza.
Cambiare il governor vale più di `-march=native` e AVX-VNNI messi insieme, *di un
fattore venti*. L'istruzione AVX-VNNI che la CPU dichiara ridurrebbe a una sola
`vpdpbusd` il prodotto int8 che oggi costa `vpmaddubsw` + `vpmaddwd`: resta una
leva non tirata, e piccola.

### Le quattro unit che ne fanno un apparecchio

| Unit | Mestiere | Da sapere |
|---|---|---|
| `hearable-governor` | Mette tutti i core su `performance` all'avvio, e li rimette su `powersave` allo stop | È una dipendenza della catena, non un accessorio |
| `hearable-t9` | La catena di riconoscimento | `Restart=always`, mai `on-failure` |
| `hearable-idle-watchdog` | Spegne la macchina quando nessuno guarda | Gira come root, indipendente dal riconoscitore |
| `hearable-rgb` | L'anello di stato sul case | Se manca `pyserial` lo dice una volta e continua senza |

**Due dettagli di systemd che sono costati inattività vera.**
`StartLimitIntervalSec` sotto `[Service]` viene ignorato in silenzio: il limite
di riavvii semplicemente non esiste. Va in `[Unit]`. E `Restart=on-failure` è
sbagliato per un apparecchio: uno strumento di misura ha fermato la catena con
`SIGINT`, systemd ha visto il codice di uscita 0, e il servizio è rimasto morto
ventitré minuti senza che nessuno se ne accorgesse. Un'uscita pulita è comunque
un apparecchio che ha smesso di fare sottotitoli.

### Spegnersi da solo

Il watchdog fa due domande e per agire gli servono entrambe le risposte: c'è
qualcosa attaccato alla porta audio (letto da `/proc/net/tcp` sulla porta 9010), e
il decoder sta ancora guardando (il suo `/api/powerstate`, con ripiego sul ping)?
Senza sessione audio fa partire un timer — venti minuti di default, tre quando il
decoder è andato in standby, entrambi in `/etc/default/hearable` — e spegne la
macchina. Una sessione che torna lo annulla.

La conseguenza pratica è che lo spettatore al mini PC non pensa mai. Compare
quando si preme `F4` e sparisce qualche minuto dopo che il televisore è tornato
silenzioso.

### L'anello di stato

Il case ha un anello RGB su un ponte seriale CH340. Il protocollo è una trama da
cinque byte — `0xFA`, modo, luminosità, velocità, checksum — a 10 000 baud, con
luminosità e velocità invertite rispetto a quello che verrebbe da pensare.

**La mappa dei colori ha preso 0 su 5.** La prima corrispondenza fra stati e
colori era stata dedotta dalla documentazione del controller e provata alla cieca
con la persona che l'anello lo guarda davvero. Era sbagliata su tutti e cinque
gli stati. Quella in uso è la mappa letta dall'hardware dallo spettatore: fa 6 su
6. La documentazione descrive le intenzioni, gli occhi descrivono l'anello.

### Sopravvivere alla rete

L'unico difetto vero trovato dalla campagna di torture da mezz'ora era nel
ricevitore audio, e solo un cavo staccato fisicamente lo ha fatto emergere: un
timeout di socket veniva ingoiato con `continue`, quindi una connessione morta
restava in mano per sempre — diciotto respawn, tre socket semiaperti, 80 kB non
letti. Ora il ricevitore rinuncia dopo 15 s di silenzio e imposta `SO_KEEPALIVE`
con 10 s di inattività, 5 s di intervallo e 3 sonde.

---

## 6b. Sottotitola anche quello che riproduci dal box

La diretta è il caso difficile, ed è quello di cui ha parlato tutto questo
documento. Ma la stessa catena sottotitola **un film riprodotto dallo storage del
ricevitore** — una chiavetta USB, un disco interno — senza nessun componente in
più e senza un secondo percorso di codice.

Non succede niente di speciale. Il relay è un secondo lettore di quello che il
decoder sta riproducendo: per la diretta legge il demux, per un file legge il
file, e in entrambi i casi manda avanti solo la traccia audio scelta, compressa,
da un punto più avanti rispetto allo spettatore. Il ricevitore vede
`source_type: file` invece di `dvb` nella risposta di controllo, e tira dritto.

### Misurato

Otto controlli su un film vero sullo storage del box, con il telecomando che
guidava pausa, salti, cambio di file e `F4`:

| Controllo | Esito | |
|---|---|---:|
| Primo sottotitolo dopo l'avvio | PASS | 36,1 s |
| Pausa | PASS | pannello fermo, relay `paused`, **0** nuovi stantii |
| Ripresa | PASS | 1,2 s |
| Salto avanti | PASS | 19,1 s |
| Salto indietro | PASS | 64,9 s |
| Cambio film | PASS | nuova epoca 41 → 42, **0** stantii dal file precedente |
| `F4` spento e riacceso | PASS | 5,4 s |
| Cambio traccia audio | `NOT_TESTABLE` | il film aveva una traccia sola |

`FILE_STALE_SUBTITLES: 0` su tutta la corsa: niente di precedente a una pausa, a
un salto o a un cambio di file è mai stato dipinto dopo. Una regressione
precedente, alla v1.2.0, aveva già chiuso lo stesso percorso con pausa, ripresa,
salto e cambio traccia tutti passati.

### Due cose volutamente diverse

**I tasti di sincronia sono rifiutati su una sorgente file.** `F2` e `F3`
regolano il ritardo di pubblicazione, che esiste per compensare quanto il tap
corre davanti all'immagine. Su un file quell'anticipo non esiste, quindi i tasti
non fanno niente e lo dicono, invece di applicare in silenzio una correzione
priva di significato.

**Il primo sottotitolo tarda di più.** 36,1 s contro i circa 4 della diretta,
perché far partire un film significa il relay che nasce, il flusso che comincia
da un confine di contenitore, e il riconoscitore che non ha ancora sentito
niente. Una volta avviato si comporta allo stesso modo.

### Che cosa non è stato provato

Le misure sono state prese con il film sullo storage USB del box
(`/media/hdd`). Un NVMe o un disco interno è lo stesso percorso per HearAble —
legge quello che Enigma2 sta riproducendo, e lo storage non lo tocca mai — ma
questo **non è stato misurato**, e il documento non lo afferma.


## 7. I due protocolli

Piccoli di proposito, separati di proposito, e ciascuno con una regola che decide
tutto il resto.

### Sottotitoli — `hearable-enigma2/1`

Un WebSocket persistente che porta un oggetto JSON per frame di testo. Il
produttore si connette verso l'esterno, il renderer sta in ascolto. Niente TLS e
niente autenticazione: il confine di fiducia è il cavo privato.

| Messaggio | Direzione | Scopo |
|---|---|---|
| `hello` | entrambi | Ruolo, progetto, versione del protocollo |
| `capabilities` | entrambi | Numero di righe, lunghezza, supporto agli ACK |
| `subtitle_state` | produttore → renderer | Le due righe da mostrare |
| `clear_subtitles` | produttore → renderer | Pulisci lo schermo adesso |
| `source_changed` | produttore → renderer | Il canale è cambiato: comincia una nuova epoca |
| `heartbeat` | produttore → renderer | Segno di vita mentre non si dice niente |
| `render_ack` | renderer → produttore | Questo numero di sequenza è arrivato sullo schermo |

**Non mandare mai la trascrizione cumulativa.** Un `subtitle_state` porta le due
righe da mostrare e nient'altro; il validatore rifiuta esplicitamente una
trascrizione progressiva, perché mandarla renderebbe traffico e memoria
quadratici nella durata della sessione.

**E il produttore non aspetta mai.** Gli ACK sono osservativi: un renderer lento
costa freschezza del sottotitolo, mai contropressione sulla catena di
riconoscimento. C'è esattamente uno stato in attesa, e uno più nuovo lo
sostituisce.

### Audio — `hearable-audio-relay/1`

Due canali in direzioni opposte, perché un ricevitore lento non possa mai
bloccare Enigma2.

| | CONTROL | MEDIA |
|---|---|---|
| Direzione | ricevitore → box | box → ricevitore |
| Trasporto | HTTP GET su `:8770` | TCP, MPEG-TS, porta scelta dal ricevitore |
| Chi si connette | il ricevitore | il box |
| Cadenza | ogni 500 ms | continua, al ritmo di riproduzione |
| Dentro Enigma2 | sì, il plugin | no, un `ffmpeg` separato |

La richiesta di controllo è anche l'annuncio e il battito: il ricevitore dice dove
vuole l'audio, quanto anticipo vuole, e quanto è risultato avanti rispetto allo
spettatore. Questi ultimi due sono numeri diversi e non vanno confusi: uno è una
richiesta, l'altro è una misura.

---

## 8. Dove va quel secondo

Il sottotitolo arriva circa un secondo dopo la parola pronunciata. Quasi tutto è
il riconoscitore che decide, ed è una scelta, non un incidente.

Misurato da capo a fondo: **p50 1,072 s** fra l'audio consumato e lo stato di
sottotitolo emesso (n = 1461, p05 0,893 s). Ricostruito dalle impostazioni in uso:

| Voce | Costo | Che cosa compra |
|---|---:|---|
| `chunk_ms: 160` | ~80 ms in media | L'audio arriva al riconoscitore a blocchi |
| `rnnt_right_context: 1` | ~160 ms | Il modello guarda avanti di un blocco prima di decidere |
| Calcolo ASR | 74 ms | Nemotron sul processore, p50 |
| Conferme dello stabilizzatore | 320–480 ms | Una parola diventa «stabile» solo dopo N passaggi concordi |
| Trattenuta della coda | ≤450 ms | L'ultima parola è trattenuta finché non ne arriva un'altra |
| Tetto di ridisegno | ≤166 ms | Sei ridisegni al secondo, così le correzioni non lampeggiano |
| Ritardo di pubblicazione | 0 ms | Niente — ed è proprio quella la correzione |

Più di metà di quel secondo è spesa a **non** mostrare qualcosa che potrebbe
essere sbagliato. Lo stabilizzatore aspetta che una parola sia confermata, e la
coda non confermata non si mostra affatto. Un sottotitolo più veloce che si
riscrive davanti a chi legge è peggio di uno più lento che sta fermo — e per un
lettore che dal testo dipende, invece di darci un'occhiata, è molto peggio.

**Uno strumento contro un occhio, sciolto.** Spostare il sottotitolo di 3 s era
nettamente visibile in tre domini di orologio indipendenti — 3018,9 ms sul
monotonic, 3008,0 ms sul PTS, +3,31 s su un marcatore ripreso — e completamente
invisibile sullo schermo. Il motivo è che il pannello si ridisegna solo ogni
0,55 s e il rumore di arrivo del riconoscitore è largo 3,75 s da p05 a p95: il
rumore è più largo dello spostamento. Il verdetto è stato registrato come metodo
di misura non valido, e non è stata cambiata una riga per inseguirlo.

---

## 9. Che cosa non è finito, e che cosa non è mai stato misurato

Scritto come lacuna invece che omesso in silenzio, perché una misura mancante che
sembra un risultato è la cosa più costosa di questo progetto.

| Voce | Stato | Dettaglio |
|---|---|---|
| Prova sul campo | IN CORSO | Il sistema è in uso quotidiano. Resta un punto aperto, ancora da caratterizzare. |
| Ritardi A/V grandi | NON MISURATO | 1,37 s e 7,56 s sono stati osservati per dieci secondi ciascuno. Se un ritardo grande regga per ore non si sa — e il ramo è chiuso comunque. |
| Timeshift in RAM | INCONCLUSIVO | Il buffer è comparso in `tmpfs` e le scritture su flash non si sono fermate. Non un risultato negativo: una domanda senza risposta. |
| AVX-VNNI | DISPONIBILE | La CPU la dichiara e la build non la usa. Attesa piccola, accanto al governor. |
| Indirizzo predefinito negli strumenti | PER SCELTA | Circa 33 strumenti puntano ancora al vecchio indirizzo del decoder. Non sostituito di proposito: un indirizzo cablato sarebbe di nuovo sbagliato al prossimo cambio. |
| Riavvio completo all'installazione | OSSERVATO | Installare il plugin riavvia tutto il box, non il solo Enigma2 come dichiara il comando. Visto, non indagato. |

### Che cosa invece ha ricevuto risposta

- **Il Wake-on-LAN sopravvive a un taglio di corrente.** Tutto quello che si
  sapeva sul risveglio del mini PC era misurato da spento via software, con
  l'alimentatore che teneva viva la scheda di rete. Staccata fisicamente la
  corrente a entrambe le macchine, il mini PC si è acceso lo stesso 6,4 minuti
  dopo il decoder — quando è stato premuto `F4` — con gli orologi delle due
  macchine allineati entro un secondo e il pulsante di accensione mai toccato.
- **Il tap sul demux non costa un sintonizzatore.** Trenta zap fra multiplex
  diversi, trenta successi, nessun servizio perso.
- **La catena sopravvive a essere attaccata.** Diciannove guasti iniettati,
  diciannove riprese senza intervento; trenta minuti di disturbo deliberato con
  zero chunk persi e zero sottotitoli stantii.

### L'avvio a freddo ha trovato un difetto, che è il motivo per cui si fa

Il plugin dichiarava 78 minuti di vita su un decoder acceso da 12. Il box corregge
il proprio orologio da parete *dopo* l'avvio di Enigma2 — 88 minuti di orologio
consumati in due minuti reali, misurati direttamente — quindi ogni durata si
portava dietro il salto. L'uptime era l'estremità innocua: lo stesso orologio
reggeva anche i quattro secondi che il saluto deve restare sullo schermo, e la
regola che toglie un sottotitolo vecchio. Ora tutte le durate stanno
sull'orologio monotonic, con un test che vieta di misurarne una sul wall clock.

---

## 10. Come è stato costruito

HearAble è stato **orchestrato da Nunzio Raciti e scritto da Claude** (il modello
Opus di Anthropic) sotto la sua direzione. Lui ha posto l'obiettivo e i vincoli,
ha preso ogni decisione di progetto, possiede l'hardware e ha fatto le prove che
contavano di più. Claude è stato l'esecutore materiale: il codice, i test, gli
strumenti di misura e questi rapporti.

La divisione si vede nei risultati. Diversi difetti che contano non li ha trovati
un test, ma la persona che usava la cosa vera:

| Trovato da | Che cosa ha trovato |
|---|---|
| I tasti del telecomando premuti | Due difetti che una matrice automatica da diciannove casi aveva dichiarato verdi |
| Il cavo staccato fisicamente | Un socket morto che `ip link down` non riproduceva |
| La corrente tolta dalla presa | Che il Wake-on-LAN ci sopravvive — domanda a cui nessuno spegnimento via software poteva rispondere |
| L'anello di stato guardato a occhio | Una mappa dei colori che faceva 0 su 5 contro la documentazione da cui era stata dedotta |
| Il televisore ascoltato | Che i sottotitoli erano in ritardo, prima che lo strumento fosse d'accordo |

### Niente è andato avanti su un'opinione

Ogni fase è passata da test e misure. È la regola di lavoro del progetto, non una
descrizione aggiunta dopo.

**246 test automatici**, tutti eseguibili senza un decoder in stanza: il
protocollo sul filo, il renderer lato ricevitore con il rifiuto per epoca e
sequenza, lo stabilizzatore, le regole di commit, i caricatori di
configurazione, la disciplina degli orologi, e la coerenza della versione fra
pacchetto e plugin.

**Gate costruiti apposta** per tutto ciò che richiede hardware vero — il gate
LAN, la sessione dal vivo, il ciclo di vita dell'interfaccia, i cicli di avvio,
il Wake-on-LAN, il timer di inattività, il lettore di file, una matrice di guasti
da diciannove casi. Ognuno produce un verdetto e un blocco leggibile a macchina,
non un log che qualcuno deve interpretare.

**Rapporti di campagna** sotto `benchmark/results/`, con ogni riga etichettata
**MEASURED**, **DERIVED** o **NOT_MEASURED**, così una lacuna non può mai essere
letta come un risultato.

**Verdetti a cui è permesso dire di no.** Due indagini sono state chiuse dai
numeri che le hanno chiuse: alla UNO Q serve 7,33× e la banda di memoria ne
concede 2,79×; il ritardo A/V si raggiunge ma si svuota fino al live in circa due
minuti. Un gate che produce troppe poche prove dichiara `NON_MISURATO` invece di
passare sul nulla.

**E quando una misura non era d'accordo con lo strumento, ha perso lo
strumento.** Si credeva che il tap corresse 2,4 s davanti all'immagine, e la
catena era stata costruita per compensarlo. Chiedendolo al decoder direttamente
— due orologi letti nello stesso istante, pacchetto in mano — sono venuti fuori
64 ms. Sbagliato di un fattore quaranta, e sbagliato nella direzione che
peggiorava il prodotto.

---

*HearAble 1.5.1 — sottotitolazione italiana in tempo reale per DVB-T2.
Orchestrato da Nunzio Raciti, scritto da Claude (Opus) sotto la sua direzione.
Octagon SF8008 V3 Supreme Combo · T9 Plus Intel N95 · Nemotron 3.5 ASR Streaming
0.6B su NeMo-Speech.cpp.*
