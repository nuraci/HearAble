# T9 Plus, Intel N95 — la CPU arriva in tempo reale

*Rapporto intermedio. 2026-09-17, dalle 20:36 alle 21:15 UTC+2. T9 Plus, Intel N95, 15 GiB,
Debian 13 trixie (kernel 6.12.107), Nemotron 3.5 ASR Streaming 0.6B q8_0, CPU, it-IT,
C_tail1. Codice HearAble `v1.2.0-3-ga08d428`, NeMo-Speech.cpp `v0.1.0` @ `4f96762`,
ggml `v0.12.0-9-gc03b4e2b`.*

**Campagna completa: fasi 0–18.** Nessuna ottimizzazione applicata al codice di calcolo.
Le modifiche adottate sono tre: una riga di sysfs (il governor), i messaggi di apertura e
chiusura sul pannello del decoder, e una correzione nel ricevitore audio trovata staccando
un cavo. La manopola diagnostica dei thread è stata misurata e rimossa e non produce nessun
numero di questo verdetto.

Ogni riga è etichettata: **MEASURED** letto da uno strumento, **DERIVED** calcolato da
misure, **NOT_MEASURED** non misurato.

## Il risultato, per primo

```text
FINAL_DECISION: T9_HEARABLE_GO
```

Il T9 fa girare Nemotron **a metà del tempo reale** e regge la televisione vera: mezz'ora di
torture con quindici interventi deliberati, zero chunk persi, zero sottotitoli stantii. Il
cavo diretto funziona senza router né DHCP, la macchina si accende da sola con un magic
packet mandato dal decoder, si spegne da sola venti minuti dopo che nessuno guarda più, e
diciannove guasti su diciannove si sono ripresi senza che nessuno toccasse una tastiera.

```text
BASELINE_RTF (mediana di tre corse)       = 0.6741   (governor powersave)
BEST_UNPATCHED_RTF (governor performance) = 0.4896
LIVE_10MIN_RTF (televisione vera)         = 0.4962
TORTURE_30MIN_RTF                         = 0.4848
```

## 1 — La macchina · MEASURED

| | |
|---|---|
| CPU | Intel N95, 4 core / 4 thread, 800 MHz – 3,40 GHz |
| cache | L1d 128 KiB · L2 2 MiB · L3 6 MiB |
| RAM | 15 GiB + 15 GiB di swap |
| SIMD | sse4_1, sse4_2, avx, **avx2**, fma, f16c, bmi2, **avx_vnni** |
| rete | Wi-Fi `wlp2s0` a ~1,1 MB/s; `enp1s0` ed `enp3s0` libere |
| governor | `powersave`, driver `intel_pstate` in modalità *active* |

`PHASE0_CPU_REQUIREMENTS: PASS`

## 2 — Identità · MEASURED

Il modello coincide con la baseline congelata byte per byte: sha256
`a5c435f2…f429ae`, 741 548 352 byte. NeMo-Speech.cpp senza patch locali.

`MODEL_IDENTITY: PASS`

## 3 — Il kernel che gira davvero · MEASURED

CMake ha scelto `-msse4.2 -mf16c -mfma -mbmi2 -mavx -mavx2`, **senza** `-march=native`, che
è la baseline voluta. Nel disassemblato di `ggml_vec_dot_q8_0_q8_0`:

| | |
|---|---|
| istruzioni chiave | `vpsignb`, `vpmaddubsw`, `vpmaddwd`, `vpbroadcastd`, `vfmadd231ps` |
| registri | 11 `ymm`, 0 `zmm` |
| fallback scalare | no — `_generic` è un simbolo separato e non viene chiamato |

`AVX2: YES` · `SCALAR_FALLBACK: NO`

**Una leva non ancora tirata.** La CPU dichiara `avx_vnni`: il prodotto int8 che oggi costa
`vpmaddubsw` + `vpmaddwd` starebbe in una sola `vpdpbusd`. L'opzione ggml `GGML_AVX_VNNI` è
OFF per default. È il primo candidato della fase 8.

## 4 — Correttezza · MEASURED

| harness | PC | T9 | distanza di edit | WER |
|---|---:|---:|---:|---:|
| catena HearAble, chunk 160 ms | 71 parole | 71 parole | 0 | **0,00 %** |
| CLI `transcribe`, file intero | 71 parole | 71 parole | 0 | **0,00 %** |

Stesso sha256 del testo, `45ecdd21…`. Il risultato non cambia fra una corsa e l'altra né fra
1 e 4 thread: deterministico.

`T9_WER: 0.00%` · `EXACT_TRANSCRIPT: YES`

**Il riferimento storico non regge, e va detto.** `benchmarks/pc/smoke_cpu.json` e il
transcript del gate UNO Q hanno **72** parole e finiscono con «grafica». Nessuna delle due
macchine la produce oggi, né dalla CLI né dalla catena paced. Il confronto valido è PC
contro T9 con lo stesso strumento lo stesso giorno — ed è identico — ma il «WER 0,00 % vs
PC» della campagna UNO Q poggia su un riferimento che al momento non so rigenerare.
`NOT_MEASURED`: perché quelle corse emettessero una parola in più.

## 5 — Baseline realtime · MEASURED

Tre corse indipendenti, pacing a scadenze assolute, WAV di 30 s.

| corsa | RTF | p50 | p95 | max | drop | CPU | RSS | temp inizio → max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0,6732 | 108,0 | 111,9 | 478,8 | 0 | 272 % | 987 MB | 48 → 58 °C |
| 2 | 0,7672 | — | 114,1 | 1335,1 | 0 | 291 % | 987 MB | 53 → 61 °C |
| 3 | 0,6741 | 107,9 | 111,7 | 477,7 | 0 | 272 % | 987 MB | 49 → 58 °C |

**Mediana 0,6741.** Ritardo finale 0,2 ms, crescita 0,0 ms/chunk: la catena non accumula.
La corsa 2 è un fuori scala con un chunk da 1,3 s; resta nei dati, ed è il motivo per cui il
verdetto usa la mediana e non la media.

`THERMAL_STABLE: YES` — picco 61 °C contro un limite di 105. Nessun throttling.

Il campionatore di frequenze e temperature è costato lo **0,167 %** del tempo (6 sorgenti,
un solo processo). Sulla UNO Q la versione a ciclo di shell costava il 13,6 %.

## 6 — Thread: il tetto è a due · MEASURED

L'albero congelato chiede **quattro thread con un letterale**, `session.cpp:903`, e nessuna
variabile d'ambiente lo tocca. `taskset` avrebbe confinato quattro thread su meno core, che è
un'altra grandezza. Quindi: manopola diagnostica temporanea, misura, rimozione — la patch è
in `experimental_threads.patch` e l'albero è tornato intatto.

| thread | RTF medio | speedup vs 1T | efficienza | p50 | CPU |
|---:|---:|---:|---:|---:|---:|
| 1 | 1,0181 | 1,00× | 100 % | 160,4 | 99 % |
| 2 | **0,6724** | **1,514×** | 76 % | 105,9 | 135 % |
| 3 | 0,7328 | 1,389× | 46 % | 116,4 | 220 % |
| 4 | 0,6732 | 1,512× | 38 % | 107,5 | 272 % |

`BEST_THREAD_COUNT: 2`

Due risultati, e nessuno dei due era atteso. **Il quarto core non serve a niente**: 2 e 4
thread danno lo stesso RTF entro lo 0,1 %. E **tre thread vanno peggio di due**, in modo
riproducibile su entrambe le corse — un numero dispari di thread sbilancia la ripartizione
delle righe e il costo si vede.

Per confronto, sulla UNO Q lo scaling era quasi lineare (efficienza 80 % a 4 core). Lì il
limite era il calcolo; qui no.

## 7 — La memoria, che spiega il punto 6 · MEASURED

STREAM, tre array da 134 MB, 402 MB di dataset, 10 ripetizioni.

| thread | COPY | SCALE | ADD | TRIAD |
|---:|---:|---:|---:|---:|
| 1 | 12,90 | 12,96 | 15,16 | 15,16 |
| 2 | **14,86** | **14,89** | **16,75** | **16,77** |
| 3 | 14,34 | 14,47 | 15,79 | 15,93 |
| 4 | 14,45 | 14,47 | 15,94 | 15,92 |

`CPU_MEMORY_BANDWIDTH_PEAK: 16.82 GB/s` — e **satura a due thread**, esattamente dove
satura l'RTF. Il riconoscitore su questa macchina non è limitato dai core: è limitato dalla
strada verso la DRAM, e quella strada è già tutta occupata da due thread.

Contro la UNO Q (9,13 GB/s di picco): **1,84 volte meglio**. È la ragione fisica per cui una
scheda arriva a RTF 2,1 e questa a 0,67.

## 8 — Tuning gratuito · MEASURED

Quattro varianti, tre corse ciascuna, stesso WAV, stesso pacing. Lo sha256 del testo è
`45ecdd21` in tutte e dodici le corse: **WER 0 ovunque**, nessun chunk perso.

| | RTF mediano | vs A | vs B |
|---|---:|---:|---:|
| A — baseline + `powersave` | 0,6762 | — | −38,1 % |
| **B — baseline + `performance`** | **0,4896** | **+27,6 %** | — |
| C — `-march=native` + performance | 0,4830 | +28,6 % | +1,35 % |
| D — `GGML_AVX_VNNI` + performance | 0,4854 | +28,2 % | +0,86 % |

**Il guadagno non viene dal compilatore: viene dal governor.** Il riconoscitore lavora a
raffiche — circa 105 ms di calcolo ogni 160 — e `intel_pstate` in `powersave` non resta mai
occupato abbastanza da salire. Con `performance` la frequenza media sotto carico passa da
~1850 MHz a **2804 MHz** e il tempo per chunk scende da 108 a 76 ms.

### La VNNI c'è, e non serve

`-march=native` su questa CPU risolve in `alderlake` e abilita `-mavxvnni`. Nel
disassemblato del kernel Q8, a confronto:

| build | `vpdpbusd` | `vpmaddubsw` | `vpmaddwd` | `vpsignb` |
|---|---:|---:|---:|---:|
| baseline | 0 | 1 | 1 | 2 |
| native | **1** | 0 | 0 | 2 |
| baseline + VNNI | **1** | 0 | 0 | 2 |

`VNNI_ACTUALLY_USED: YES` — verificato nell'eseguibile, non dedotto dal flag della CPU. Due
istruzioni diventano una, e il tempo cala dello 0,86 %. Non è una delusione: è la conferma
della fase 7. Il kernel non è limitato dall'aritmetica ma dalla strada verso la DRAM, e
risparmiare istruzioni su un lavoro che aspetta la memoria non produce quasi niente.

### Decisione

Regola 8.5: sotto il 3 % una variante non giustifica una build speciale permanente.

```text
BEST_UNPATCHED_RTF: 0.4896
BEST_BUILD: baseline (AVX2 esplicito, nessun flag speciale)
BEST_GOVERNOR: performance
VNNI_ACTUALLY_USED: YES (non adottata: +0.86%)
FINAL_ASR_THREAD_COUNT_FOR_LIVE_TEST: 4 (il letterale dell'albero congelato)
PHASE8_DECISION: si adotta solo il governor; build invariata
```

La variante E (`native` + VNNI) **non è stata costruita**: con C a +1,35 % e D a +0,86 %,
combinarle non può cambiare quale build va in produzione.

## 11 — Il decoder sulla LAN di casa · MEASURED

T9 su Wi-Fi, SF8008 su Ethernet, stessa rete domestica, nessun cavo dedicato. Dodici
controlli, **tutti PASS**.

| | controllo | esito |
|---:|---|---|
| 1 | plugin OFF: niente dipinto, tap rilasciato | PASS |
| 2 | plugin ON | PASS |
| 3 | il T9 si registra come ricevitore | PASS — `192.168.1.30:9010` |
| 4 | audio ricevuto e trasformato in stati | PASS |
| 5 | prima trascrizione | PASS — «Vesto super coniglio ce l'ho» |
| 6 | sottotitolo dipinto | PASS — 386 render |
| 7 | plugin OFF: schermo pulito, coda vuota | PASS |
| 8 | plugin ON: epoch nuovo | PASS — 1 → 3, zero stantii |
| 9 | zap nello stesso multiplex | PASS — PID 1150, zero stantii |
| 10 | zap su multiplex diverso | PASS — PID 303, zero stantii |
| 11 | cambio traccia audio | **NOT_TESTABLE** — il servizio ha una traccia sola |
| 12 | `kill -9` della pipeline e riconnessione | PASS — torna a `streaming` da solo |

`TUNER_FREE: YES` · `LOST_SERVICES: 0` · `STALE_SUBTITLES: 0`

**Tre dipendenze mancavano su Debian netinst** e sono ora prerequisiti dell'appliance:
`curl`, `python3-aiohttp` e soprattutto **`ffmpeg`**, che decodifica l'AAC del transport
stream in PCM. Senza di lui il tap apriva la connessione, mandava 4,5 KB e si prendeva un
*broken pipe*: il sintomo non nominava mai la causa.

## 12 — Dieci minuti di televisione vera · MEASURED

Rai News 24, parlato continuo, 628 s di audio.

| | |
|---|---:|
| **RTF live** | **0,4962** |
| chunk p50 / p95 / max | 79,0 / 82,8 / 475,6 ms (budget 160) |
| chunk persi | **0** |
| stati sottotitolo generati | 3925 |
| ridipinture del pannello | 981, con 981 ack |
| coda audio massima | 1 chunk; 2 flush, **0 chunk persi nei flush** |
| stati stantii dopo barriera | **0** |
| messaggi non validi / errori di trasporto | 0 / 0 |
| riconnessioni del sink | 1 (quella iniziale) |
| latenza di ack p50 / p95 | 41,8 / 151,6 ms |
| RSS del processo | 1,03 GB |
| CPU media | 202 % (due core su quattro) |
| frequenza media / massima | 2804 / 3242 MHz |
| temperatura inizio → massima | 48 → **61 °C** (limite 105) |

`LIVE_10MIN_DROPS: 0` · `LIVE_10MIN_BACKLOG: 0`

Un solo avviso del decoder audio in dieci minuti, al secondo 323: `non monotonically
increasing dts`, un pacchetto con timestamp ripetuto. Non ha prodotto né un chunk perso né
un buco nei sottotitoli, ed è annotato qui invece di essere ignorato.

Nota di metodo: nessun timestamp del T9 è stato sottratto da un timestamp del box. Le due
macchine non hanno un orologio comune, e la latenza da bocca a schermo **non è stata
misurata** in questa sessione: `NOT_MEASURED`, non zero.

## 13 — Mezz'ora di maltrattamenti · MEASURED

Trenta minuti di DVB reale con quindici eventi programmati, distribuiti nel tempo e
mescolati a tratti lunghi senza interventi.

| evento | quante volte | esito |
|---|---:|---|
| zap nello stesso multiplex | 3 | tutti riagganciati |
| zap su multiplex diverso | 3 | tutti riagganciati |
| plugin OFF → ON | 3 | epoch nuovo ogni volta |
| cambio traccia audio | 2 | PID 300 → 301 → 300 |
| `kill -9` della pipeline | 1 | ripresa automatica |

| | |
|---|---:|
| **RTF** | **0,4848** |
| chunk p50 / p95 / max | 77,0 / 80,7 / 477,4 ms |
| chunk persi | **0** |
| stati sottotitolo (sessione intera) | 10 075, con 2118 ridipinture e 2097 ack |
| stati stantii dopo barriera | **0** |
| messaggi non validi, errori di trasporto, overflow | 0, 0, 0 |
| coda del pannello | sempre 0 |
| coda audio | 1 chunk; 8 flush, **0 chunk persi nei flush** |
| temperatura media per terzo | 53,6 / 52,3 / **54,1 °C** (limite 105) |
| frequenza media | 2800 MHz |

`PHASE13_TORTURE_30MIN: PASS`. Nessun degrado progressivo: il terzo finale è termicamente
indistinguibile dal primo.

**Il cambio traccia audio era testabile dopo tutto.** Nella campagna precedente l'avevo
dichiarato `NOT_TESTABLE` perché nessun servizio provato aveva più di una traccia. Rai 1 HD
ne dichiara **quattro**.

Nota sulle metriche: il `kill -9` al ventesimo minuto si porta via le metriche del primo
segmento, che muoiono con il processo — è esattamente quello che il test chiedeva di fare.
L'RTF qui sopra è quello del secondo segmento, 603 s; i contatori del plugin coprono invece
tutta la mezz'ora.

## I messaggi di apertura e chiusura · MEASURED

```text
By Nunzio Raciti          HearAble 1.3.0
HearAble · Avvio...       Arrivederci
```

Il credito saluta, la versione se ne va — così spegnere i sottotitoli è anche il modo di
chiedere al decoder quale build sta girando, senza terminale e senza sapere che esiste un
terminale. Il numero viene da `HEARABLE_VERSION` e non è scritto nella stringa: cambia da
solo a ogni rilascio, e il gate lo legge dal box invece di aspettarsi un valore fisso —
che è la stessa trappola per cui la versione era invecchiata di nascosto.

Sono **stati di interfaccia, non sottotitoli**: non passano dallo stabilizzatore né dal
formatter, e il produttore non crede di averli mandati. Il saluto non blocca niente — la
pipeline parte dietro — e il primo sottotitolo vero si riprende il pannello appena esiste;
se non arriva nessuno, dopo due minuti il messaggio si cancella invece di restare a
descrivere una cosa che non sta succedendo. L'arrivederci dura quattro secondi
configurabili, poi il pannello viene pulito.

| | |
|---|---|
| `UI_START_MESSAGE` | PASS |
| `UI_STOP_MESSAGE` | PASS |
| `UI_CREDIT_ONLY_LIFECYCLE` | PASS — campioni del pannello durante il parlato, **zero** con il credito o con la versione |
| `UI_NO_STALE_AFTER_STOP` | PASS — pannello vuoto, coda 0, nessun lifecycle residuo |

Il saluto resta **almeno quattro secondi** anche quando un sottotitolo è già pronto, come
l'arrivederci. Per dargli quei secondi non si butta via niente: gli stati aspettano nella
linea di ritardo, che tiene il più recente e scarta quelli superati, così ciò che compare
dopo è quello che si sta dicendo allora e non quello che si diceva quattro secondi prima.

### Il messaggio cancellato da chi aveva ragione

Premendo F4 con il T9 addormentato, il saluto durava otto secondi e poi il pannello restava
**nero per i venti** che la macchina impiega a svegliarsi — cioè esattamente quando
qualcosa deve dire allo spettatore che sta succedendo qualcosa.

La regola che nasconde un pannello inattivo guarda le righe del riconoscitore, e quelle
contenevano ancora l'ultimo sottotitolo della sessione precedente: il saluto non è un
sottotitolo e non le tocca. Così la regola vedeva del testo vecchio ammutolito e puliva lo
schermo, portandosi via il messaggio. **Aveva ragione sul testo e torto sul messaggio.**

Continua a dimenticare le righe vecchie — altrimenti riaffiorerebbero quando il saluto se
ne va — ma non dipinge più sopra un messaggio di lifecycle. Verificato a macchina davvero
spenta: saluto sullo schermo per tutti i trenta secondi, fino al primo sottotitolo vero.

Scrivere il test ha trovato un difetto vero: il file di stato riportava le due righe del
riconoscitore come se fossero lo schermo, quindi un messaggio di lifecycle sembrava un
pannello vuoto. Il plugin ora registra **ciò che ha dipinto**, e ogni pittura passa da un
punto solo.

## 14 — La rete privata · MEASURED

```text
Internet / LAN ── Wi-Fi ── SF8008 192.168.1.244 (default route)
                            │ eth0 10.77.0.1/24, nessun gateway, nessun DNS
                            │ cavo RJ45 diretto
                       T9 enp1s0 10.77.0.2/24
```

`T9_HEARABLE_NIC: enp1s0`, identificata per carrier e non a indovinare: prima del cavo
entrambe le porte a 0, dopo solo `enp1s0` a 1, ed `enp3s0` resta libera.

Dodici controlli funzionali sul link privato, **tutti PASS**, compresi i due zap e il
`kill -9` della pipeline, con zero stati stantii.

**Test di indipendenza dal router: PASS.** Con il Wi-Fi del T9 abbassato per 87 s — una
sola riga nella tabella di routing — sono arrivati **344 stati** e sullo schermo c'era del
testo. Niente router, niente DHCP, niente DNS nel percorso critico.

**Il Wi-Fi del decoder non era un problema di coesistenza.** L'immagine dichiarava già
`auto eth0` e `auto wlan0`: la scheda era configurata per un SSID che non è quello di questa
casa, e il 4-way handshake non si chiudeva mai. Messa la rete giusta, le due interfacce
convivono e la default route resta sul Wi-Fi.

Per rientrare nel decoder quando il cavo spostato lo aveva isolato, il T9 ha fatto da server
DHCP sul solo `enp1s0` per due minuti, giusto il tempo di dargli `10.77.0.1` e renderlo
statico. Poi rimosso: **sul percorso critico non c'è DHCP**.

## 14B — Wake-on-LAN · MEASURED

| | |
|---|---|
| `WOL_ADVERTISED` | YES (`Supports Wake-on: pumbg`) |
| `WOL_FROM_S5_REAL_TEST` | **PASS** |
| `WOL_RELIABLE_3_CYCLES` | **PASS** — 32,0 / 32,1 / 32,0 s |
| `POWER_ON_METHOD` | WOL, con il pulsante come riserva |

Spegnimento vero con `systemctl poweroff` e magic packet mandato **dal decoder sul cavo
privato**, che è il percorso reale. I 32 secondi non sono la latenza del pacchetto ma il
tempo di avvio fino a quando la macchina risponde: la sveglia è immediata.

`Wake-on: g` sopravvive al riavvio perché il driver `r8169` lo riazzera e la configurazione
dell'interfaccia lo riapplica.

**F4 manda il pacchetto, e per un giorno non lo ha mandato.** La capacità era verificata ma
l'integrazione no: il rapporto diceva `INTEGRATE_IN_F4_ON` e nessuno integrava niente, così
alla prima prova reale il pannello diceva «Avvio...» a un decoder che aspettava una macchina
che nessuno aveva svegliato. Adesso il plugin manda il pacchetto all'accensione e poi ogni
20 s finché il ricevitore non compare, al massimo 15 volte; configurabile in
`/etc/enigma2/hearable_wol.json`, con `wol_sent` e `wol_attempts` nella telemetria.

Misurato da spento, con un film sulla chiavetta: pacchetto a 6 s, secondo a 18 s, macchina
viva a 24 s, **primo sottotitolo a 30 s**. Due pacchetti su quindici disponibili, poi il
relay ha smesso.

## 15 — L'appliance · MEASURED

Quattro unità, responsabilità separate:

```text
hearable-governor.service       performance all'avvio, powersave allo stop
hearable-t9.service             la pipeline; Restart=on-failure, ON=0
hearable-idle-watchdog.service  spegnimento per assenza di sessione audio
hearable-rgb.service            il ring; nessuno dipende da lui
```

Tre cicli di riavvio, `BOOT_CYCLES: PASS`, senza toccare niente dopo l'accensione:

| ciclo | torna dopo | indirizzo | governor | unità | WOL | F4 → sottotitolo |
|---|---:|---|---|---|---|---:|
| 1 | 37,7 s | 10.77.0.2/24 | performance | attive | `g` | 7,03 s |
| 2 | 32,9 s | 10.77.0.2/24 | performance | attive | `g` | 5,21 s |
| 3 | 37,8 s | 10.77.0.2/24 | performance | attive | `g` | 5,33 s |

Con il T9 già acceso, F4 → primo sottotitolo: **mediana 4,02 s** su tre prove.

## 15C — Lo spegnimento per inattività · MEASURED

Il segnale è **la sessione audio, mai il parlato**: un canale può stare in silenzio o
trasmettere musica e la macchina resta accesa. Il watchdog guarda la connessione TCP che il
tap apre sul ricevitore, e non tocca né l'ASR né il ring.

| | |
|---|---|
| `NO_FALSE_POWEROFF_ON_SILENCE` | PASS — sessione viva osservata per 141 s con timeout di prova a 120 |
| `AUDIO_SESSION_IDLE_TIMER` | PASS — parte 0,5 s dopo F4 OFF |
| `IDLE_TIMER_CANCEL_ON_AUDIO_RETURN` | PASS — annullato in 5,4 s, con 100 s ancora sul contatore |
| perdita involontaria della sessione | PASS — timer a 2,6 s, sessione tornata da sola in 18,0 s |
| `AUTO_POWEROFF_AFTER_TIMEOUT` | PASS — spenta dopo 127,7 s su 120 di timeout, risvegliata con WOL in 32,1 s |
| `IDLE_TIMER_CONFIGURABLE` | PASS — `/etc/default/hearable`, riportato a 20 minuti |

### Spegnere il decoder spegne anche il T9

Aggiunto il 18 settembre su richiesta, e la prima versione era sbagliata due volte.

Il watchdog, quando non c'è sessione, chiede al decoder che cosa sta facendo e distingue
tre casi:

| il decoder è | come si riconosce | attesa |
|---|---|---|
| in visione | risponde e non è in standby | 20 minuti |
| in **standby** (tasto del telecomando) | `instandby: true` da OpenWebif | **3 minuti** |
| **assente** (standby profondo, spina, cavo) | non risponde affatto | **3 minuti** |

Deve restare via **60 s** prima di contare, così un riavvio di enigma2 per un aggiornamento
— che lascia la macchina raggiungibile e torna in pochi secondi — non fa scattare niente.

Misurato dall'inizio alla fine con il telecomando: standby riconosciuto subito, limite
passato da 20 a 3 minuti dopo 75 s, **T9 spento a 200 s**. E il ritorno, misurato subito
dopo: decoder riacceso, F4, primo magic packet, macchina viva, **sottotitoli sullo schermo
a 30 s**.

**Due cose scoperte per strada, e sono il motivo per cui il disegno è questo.**

La prima: il gancio di chiusura del plugin non viene **mai** chiamato su openATV 7.5.1 —
provato con `init 4` e con il riavvio pulito di OpenWebif, il log si interrompe e basta.
Un saluto mandato dalla scheda non è un meccanismo su cui appoggiarsi, e il codice che lo
manda è rimasto nell'albero con scritto sopra che non serve a niente qui.

La seconda l'ha trovata lo spettatore premendo il tasto: **il telecomando mette il decoder
in standby, non lo spegne**. Risponde al ping esattamente come prima. La mia prima versione
si basava sulla raggiungibilità e non si sarebbe accorta di niente — avrei scritto in questo
rapporto che funzionava, e sarebbe stato falso.

Vale la pena registrarlo: **entrambi i difetti di questa giornata sono emersi da qualcuno
che usava il telecomando**, non dai diciannove casi della matrice, dove ero io a pilotare
tutto via rete. Una matrice dei guasti costruita da chi conosce il sistema prova i guasti
che quel sistema si aspetta.

## 16 — Diciannove guasti · MEASURED

`FAILURE_RECOVERY: PASS`, 19 casi su 19, nessun intervento manuale.

| guasto | recupero |
|---|---:|
| `kill -9` della pipeline ASR | 10,6 s |
| `kill -9` del tap sul decoder | 5,5 s |
| restart del servizio | 10,6 s |
| decoder assente (enigma2 fermato) | sessione caduta, come atteso |
| decoder di nuovo disponibile | 6,0 s dopo F4 |
| **cavo staccato** | socket rilasciato in **14,1 s** |
| **cavo reinserito** | sessione viva in **7,1 s**, 62 stati nei 10 s successivi |
| reboot del decoder con T9 acceso | 3,3 s |
| ffmpeg ucciso sul T9 | **la sessione non cade affatto** |
| controller RGB assente | 163 sottotitoli mentre non c'era |
| magic packet a macchina accesa | ignorato |

Gli altri otto casi sono misurati nei gate dedicati e citati per riferimento invece di essere
ripetuti.

### Il difetto trovato staccando un cavo

Il caso 6 **falliva**, ed è l'unico difetto vero della campagna. Staccato il cavo, il
ricevitore restava aggrappato al socket morto: TCP non sa che il peer è sparito, e
`except socket.timeout: continue` aspettava per sempre. Il ciclo di accettazione è seriale,
quindi la connessione viva non veniva mai presa in carico: il tap del decoder ha ritentato
**diciotto volte** e sul T9 sono rimasti **tre socket semiaperti** con ~80 kB non letti
ciascuno. I sottotitoli non tornavano da soli.

Correzione in `hearable/sf8008/relay_source.py`: un tap in diretta manda ~26 kB/s senza
pause, quindi **quindici secondi di silenzio non sono un passaggio tranquillo ma un
interlocutore assente**, e la connessione viene lasciata andare. Più keepalive TCP sul
socket accettato e un contatore `stream_timeouts` nella telemetria, così il fatto si legge
invece di dedurlo.

Verificata due volte: abbassando il link via software (recupero in 28 s) e poi con lo
strappo fisico, che è il caso che aveva rotto tutto — 14,1 s per lasciare il socket, 7,1 s
per tornare a trasmettere.

## 17 — Il ring · MEASURED

Protocollo verificato su due implementazioni indipendenti e poi sull'apparecchio: frame di
cinque byte `0xFA, modo, luminosità, velocità, checksum`, checksum come somma troncata a
8 bit, **10000 baud** (il valore del driver ufficiale, non i 9600 del CH340N), cinque modi
predefiniti, luminosità e velocità 1..5 invertite. `RGB_SOLID_COLORS_SUPPORTED: NO` — il
firmware non accetta colori arbitrari.

**La prima mappa ha preso zero su cinque.** Costruita sulle intuizioni di chi l'ha scritta e
messa alla prova alla cieca con uno spettatore, tre pattern su cinque sono stati letti come
l'esatto contrario dell'intenzione: i colori che giravano dicevano «in attesa» dove volevano
dire «sto partendo», e il lampeggio diceva «mi sto riagganciando» dove voleva dire «guasto».

La mappa che è nell'albero è **quella dello spettatore**, assegnata a partire dalle sue
letture invece che scelta:

| stato | pattern | letto come |
|---|---|---|
| BOOTING | arcobaleno | «sta partendo» |
| READY | colori che girano | «accesa e in attesa» |
| STREAMING | modo `auto` | «sta lavorando» |
| RECONNECTING | lampeggio secco | «si sta riagganciando» |
| ERROR | respiro rapido e luminoso | «c'è un problema» |
| IDLE_COUNTDOWN | l'attesa che si affievolisce col contatore | «si sta spegnendo» |

`RGB_SELF_EXPLANATORY_GATE: PASS alla seconda mappa`. Il conto alla rovescia non si
distingue dall'attesa per una differenza costante — che il mandato sconsiglia — ma perché
**cambia nel tempo**: sbiadisce mentre il timer scende.

Limite dichiarato: le letture vengono da un solo spettatore, che è anche l'utente
dell'apparecchio. Sono osservazioni alla cieca, non un campione.

## 18 — Filesystem in sola lettura · DERIVED

`READ_ONLY_FS_FEASIBLE: YES, con riserve` — **non applicato**, come chiede il mandato.

L'unico scrittore vero a regime è il lanciatore, che salva metriche ed eventi a ogni
sessione: va spostato su tmpfs o spento in produzione. Il modello (708 MB) e i sorgenti non
cambiano mai e starebbero in sola lettura senza costi. Il journal persistente è il vero
compromesso: `Storage=volatile` risolve le scritture ma perde la cronologia proprio del
guasto che ha causato un riavvio.

**Raccomandazione: non convertire adesso.** `ProtectSystem=strict` sulle tre unità dà quasi
tutto il beneficio senza rendere la root in sola lettura e senza complicare gli
aggiornamenti. La root in sola lettura ha senso quando l'apparecchio è stabile da settimane;
oggi è il primo giorno che esiste.

## Il blocco finale

```text
PLATFORM: T9 Plus mini PC
CPU: Intel N95, 4C/4T, 800-3400 MHz
OS: Debian GNU/Linux 13 (trixie), kernel 6.12.107
RAM: 15 GiB + 15 GiB swap

HEARABLE_VERSION: 1.3.0 (dichiarata dal box, dal T9 e da ogni file di metriche)
HEARABLE_COMMIT: v1.3.0 + le correzioni della sera del 18
NEMO_SPEECH_COMMIT: 4f9676226f667d14608487df744f375db87127f8 (v0.1.0)
GGML_COMMIT: c03b4e2bcece5134827881af90242086daf75be5 (v0.12.0-9)
MODEL: nemotron-3.5-asr-streaming-0.6b.q8_0.gguf
MODEL_SHA256: a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae

BEST_BUILD: baseline (AVX2 esplicito, nessun flag speciale)
BEST_GOVERNOR: performance
FINAL_ASR_THREAD_COUNT: 4 (letterale dell'albero congelato; 2 basterebbero)
BEST_UNPATCHED_RTF: 0.4896

LIVE_10MIN_RTF: 0.4962
LIVE_10MIN_DROPS: 0
LIVE_10MIN_BACKLOG: 0

TORTURE_30MIN_RTF: 0.4848
TORTURE_30MIN_DROPS: 0
TORTURE_30MIN_BACKLOG: 0
TORTURE_30MIN_MAX_QUEUE: 0 (pannello) / 1 chunk (audio)
TORTURE_30MIN_MAX_TEMP: 60 C

STALE_SUBTITLES: 0
LOST_SERVICES: 0
TUNER_FREE: YES

UI_START_MESSAGE: PASS ("By Nunzio Raciti" / "HearAble · Avvio...", almeno 4 s)
UI_STOP_MESSAGE: PASS ("HearAble <versione>" / "Arrivederci", 4 s)
UI_CREDIT_ONLY_LIFECYCLE: PASS (ne' il credito ne' la versione durante il parlato)
UI_NO_STALE_AFTER_STOP: PASS
UI_GREETING_SURVIVES_THE_WAIT: PASS (30 s a macchina spenta, senza pannello nero)

DIRECT_RJ45: PASS
T9_HEARABLE_NIC: enp1s0 (MAC oscurato in questa copia pubblica)
SF8008_PRIVATE_IP: 10.77.0.1
T9_PRIVATE_IP: 10.77.0.2
SF8008_DEFAULT_ROUTE_STILL_WIFI: YES
ROUTER_INDEPENDENCE_TEST: PASS

WOL_ADVERTISED: YES
WOL_FROM_S5_REAL_TEST: PASS
WOL_DECISION: INTEGRATE_IN_F4_ON
POWER_ON_METHOD: WOL (pulsante fisico come riserva)

SYSTEMD_AUTOSTART: PASS
GOVERNOR_PERSISTENT: PASS
BOOT_CYCLES_PASS: 3/3
F4_ON_LIFECYCLE: PASS (mediana 4.02 s a T9 gia' acceso)
AUDIO_SESSION_IDLE_TIMER: PASS
IDLE_TIMER_CONFIGURABLE: YES (/etc/default/hearable, 20 minuti)
IDLE_TIMER_CANCEL_ON_AUDIO_RETURN: PASS
NO_FALSE_POWEROFF_ON_SILENCE: PASS
AUTO_POWEROFF_AFTER_TIMEOUT: PASS
DECODER_OFF_POWERS_OFF_THE_T9: PASS (standby o assenza -> 3 minuti; misurato 200 s)
DECODER_ON_PLUS_F4_WAKES_IT: PASS (30 s dal tasto al primo sottotitolo)
FAILURE_RECOVERY: PASS (19 casi su 19)

RGB_CONTROLLER_DISCOVERY: PASS
RGB_PROTOCOL_VERIFIED: PASS (frame a 5 byte, 10000 baud, verificato a occhio)
RGB_CONTROL: PASS
RGB_INTEGRATION: PASS alla seconda mappa (la prima ha preso 0 su 5)

READ_ONLY_FS_FEASIBLE: YES con riserve, non applicato

CPU_MEMORY_BANDWIDTH_PEAK: 16.82 GB/s (satura a 2 thread)
THERMAL_STABLE: YES
CPU_DECISION: T9_CPU_STRONG_GO

FINAL_DECISION: T9_HEARABLE_GO
FIRST_REMAINING_FAILURE: nessuno
NEXT_ACTION: usarlo. Restano annotati due limiti noti, entrambi prudenti: il watchdog impiega 15 s ad accorgersi di un cavo staccato, e 60 s a contare uno standby.
```

## La versione, che aveva smesso di essere vera

`hearable/__init__.py` diceva `1.0.3` mentre i tag erano arrivati a `v1.2.0`. Quella stringa
finisce in ogni file di metriche: **dieci artefatti** di due campagne dichiarano una
versione che non esisteva da settimane, e nessuno se n'era accorto perché niente guardava.

Adesso c'è una sola fonte — `hearable/__init__.py`, oggi **1.3.0** — e tre cose che la
controllano: un test che fallisce se la copia del plugin diverge o se l'albero resta
indietro rispetto al tag più recente; l'installatore che si rifiuta di copiare un plugin
con una versione diversa dall'albero; e lo stesso installatore che si rifiuta di dichiarare
riuscita un'installazione se poi il box ne dichiara un'altra. Provate entrambe: con le
versioni disallineate l'installatore esce con codice 2 e dice quali file allineare.

`PROTOCOL_VERSION` resta separato a 1: è il contratto sul filo, e deve muoversi quando
cambiano i messaggi, non a ogni rilascio.

Non è derivata da `git describe` di proposito. Sul decoder git non esiste, e una versione
che si risolve solo dentro un checkout manca proprio dove la domanda viene posta — ed è per
questo che lo spegnimento la scrive sul televisore.

## Quello che non è stato misurato

* **Il percorso da chiavetta dal T9**: verificato il 18 settembre alla prima prova reale —
  il relay riconosce la sorgente come `file` e i sottotitoli seguono. Le prove strutturate
  (pausa, ripresa, salto, cambio traccia) restano quelle del 17 settembre fatte dal PC:
  `NOT_RUN` dal T9.
* **Latenza da bocca a schermo sul percorso T9**: le due macchine non hanno un orologio
  comune e non è stata misurata. `NOT_MEASURED`, che non vuol dire zero.
* **Spegnimento fisico del decoder** (staccare la corrente): il caso 4 della matrice ferma
  enigma2, che è quello che il T9 osserva, ma non è la stessa cosa. `NOT_RUN`.
* **Il ring giudicato da più di una persona**: le letture vengono da un solo spettatore.
* **Filesystem in sola lettura**: valutato, non applicato, con una raccomandazione contraria
  per adesso.

## Due limiti noti, misurati

**Il cavo staccato.** Il watchdog riconosce la fine della sessione dalla connessione TCP, e
una connessione a cui si stacca il cavo non muore subito. Con la correzione del ricevitore
il socket viene lasciato andare dopo 15 s, quindi il conto alla rovescia parte con quel
ritardo invece che immediatamente.

**Lo standby.** Il decoder in standby deve restare tale 60 s prima di contare, perché un
riavvio di enigma2 per un aggiornamento lascia la macchina raggiungibile e torna in pochi
secondi. Un minuto in più su una attesa di tre.

Tutti e due sbagliano nel verso prudente: la macchina resta accesa più del dovuto, non si
spegne per errore. Sono scritti qui invece di essere scoperti fra sei mesi.

## Come è finita la giornata

Il sistema che esce da questi due giorni si usa così, e ogni riga è misurata:

```text
spegni il decoder          il T9 si spegne in poco più di tre minuti
riaccendi e premi F4       messaggio di avvio, magic packet, sottotitoli in 30 s
premi F4 di nuovo          HearAble 1.3.0 / Arrivederci, poi il conto alla rovescia
```

**Entrambi i difetti dell'ultima ora sono stati trovati da qualcuno con un telecomando in
mano**, non dai diciannove casi della matrice. Vale la pena ricordarlo alla prossima
campagna: chi costruisce un sistema prova i guasti che quel sistema si aspetta.
