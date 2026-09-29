# hearable-audio-relay/1 — il formato sul filo

Questo documento descrive il trasporto fra un SF8008 che sta riproducendo e il
nodo HearAble che ascolta. È scritto come contratto: chi implementa un altro
ricevitore deve poterlo fare senza leggere il codice del plugin.

Due canali separati, per la ragione data nel gate: un ricevitore lento o assente
non deve mai fermare Enigma2, quindi l'audio non passa mai dalla coda di
controllo e il controllo non attende mai l'audio.

| | CONTROL | MEDIA |
|---|---|---|
| direzione | ricevitore → box (richiesta) | box → ricevitore (push) |
| trasporto | HTTP GET su porta 8770 | TCP, MPEG-TS, porta scelta dal ricevitore |
| chi si connette | il ricevitore | il box |
| cadenza | ogni 500 ms | continua, al ritmo di riproduzione |
| dentro Enigma2 | sì (plugin) | no (processo ffmpeg separato) |

## CONTROL

`GET http://<box>:8770/control?receiver=<ip>:<porta>&want_lead_ms=<voluto>&lead_s=<misura>`

La richiesta è anche l'annuncio: il ricevitore dice dove vuole ricevere, quanto
anticipo vuole, e quanto è risultato avanti rispetto allo spettatore. I due
numeri sull'anticipo sono diversi e non vanno confusi: `want_lead_ms` è la
richiesta, `lead_s` è la misura. Un ricevitore che smette di
chiedere sparisce dopo `RECEIVER_TTL_S` (8 s) e il relay si ferma da solo.
`receiver` è URL-encoded: i due punti arrivano come `%3A` e vanno decodificati.

La risposta è JSON, campi stabili:

| campo | significato |
|---|---|
| `protocol` | `hearable-audio-relay/1` |
| `source_type` | `file`, `stream`, `dvb` o `unknown`, dedotto dal riferimento del servizio |
| `service_reference` | riferimento Enigma2, es. `4097:0:0:...:/media/hdd/x.mkv` |
| `media` | percorso sul box |
| `source_epoch` | intero crescente; cambia a ogni discontinuità |
| `state` | `idle`, `waiting_for_receiver`, `streaming`, `paused` |
| `selected_audio_track` / `selected_audio_language` / `audio_track_count` | traccia scelta dallo spettatore |
| `position_s` / `length_s` | posizione del decoder e durata, secondi |
| `sampled_wall_epoch` | quando quella posizione è stata letta |
| `relay_started_at_media_s` | da dove il relay corrente ha iniziato a leggere |
| `relay_lead_ms` | anticipo in vigore, in millisecondi: quello chiesto dal ricevitore, o 1500 se nessuno ha chiesto |
| `relay_head_start_s` | margine fisso, 5.0 |
| `relay_spawns` / `relay_exits` / `last_reason` | diagnostica |
| `wire` | `{container, codec, transcoded, timestamps}` |

`position_s` viene da `getPlayPosition()` del servizio Enigma2, in unità da
90 kHz convertite in secondi. Sta **1099 ms avanti** all'immagine effettivamente
sullo schermo (misurato, dispersione 91 ms su una decina di campioni): è la
posizione del decoder, non quella del televisore, ed è esattamente ciò che serve
qui, perché il punto dell'architettura è prendere l'audio prima della
presentazione.

## MEDIA

Il box esegue un secondo ffmpeg, fuori da Enigma2:

```
ffmpeg -v error -nostdin -re -copyts -ss <position + lead + head_start>
       -i <media> -map 0:a:<track> -c copy -f mpegts tcp://<ricevitore>
```

- **Nessuna transcodifica.** `-c copy`: l'AAC selezionato viaggia com'è, 191–211
  kbit/s misurati. Il SF8008 non paga codifica.
- **Nessun video.** Un solo `-map` sulla traccia audio scelta.
- **`-re`**: legge al ritmo di riproduzione. Un consumatore fermo riempie il
  buffer TCP e ferma questo ffmpeg, non il decoder.
- **`-copyts`**: i PTS sono quelli veri del file sorgente. Questo è il punto
  chiave del protocollo — vedi sotto.
- Il box si connette *verso* il ricevitore. Con `listen=1` il relay sceglieva la
  posizione di partenza fino a 30 s prima che qualcuno si collegasse, e
  l'audio arrivava etichettato con un istante che non esisteva più.

### I timestamp sono l'unica autorità

Il ricevitore ricava la posizione nel media **solo** dai PTS del primo pacchetto
TS. Non dal control plane, non dal proprio orologio. Una versione precedente
sovrascriveva il PTS con l'inizio che il control plane diceva di aver chiesto, e
ha etichettato audio del secondo 504 come secondo 91,4: da lì in poi ogni
sottotitolo sarebbe finito nel punto sbagliato del film. Il control plane dice
*cosa* sta succedendo; il flusso dice *dove*.

Basta un campione: 1880 byte sono sufficienti a leggere il primo PTS, e il
ricevitore ne legge 4096.

### Chi regola l'anticipo

Un solo regolatore, e sta al ricevitore. Il relay punta generosamente avanti
(`lead + 5 s`) e non si corregge mai; il ricevitore rifiuta di passare al
pipeline qualunque cosa sia più avanti di `relay_lead_ms` rispetto alla
posizione del decoder, e l'eccedenza aspetta in coda. Due regolatori, uno per
estremità, si sono combattuti: il relay smontava flussi che l'allowance aveva
appena costruito, e il ricevitore restava bloccato dietro una coda piena.

### Chi decide l'anticipo

Il ricevitore, e vive in `config/audio_source_sf8008_relay.json` come `lead_ms`.
Il relay lo riceve a ogni poll e lo usa **solo** per scegliere da dove iniziare a
leggere; a regolarlo davvero è il ricevitore, che rifiuta di passare al pipeline
qualunque cosa sia più avanti di tanto.

Cambiarlo mentre un flusso è in corso fa ripartire il relay dalla nuova
posizione — altrimenti il nuovo valore avrebbe effetto solo alla discontinuità
successiva, che può essere lontana un'ora. Una variazione sotto i 100 ms non
vale una giunzione nell'audio e viene adottata senza riavvio.

## Discontinuità

Ogni evento qui sotto alza `source_epoch`, fa ripartire ffmpeg dalla nuova
posizione e fa svuotare la coda al ricevitore. L'invariante è che **nessun
frame di un'epoca precedente venga consegnato dopo la barriera** (verificato:
zero su tutte le barriere osservate).

| evento | come viene visto | cosa fa il relay |
|---|---|---|
| anticipo cambiato | il ricevitore chiede un `want_lead_ms` diverso di almeno 100 ms | riparte dalla nuova posizione |
| pausa | la posizione smette di avanzare per ≥ 1,5 s | ferma ffmpeg, stato `paused` |
| ripresa | la posizione riprende | riparte dalla posizione corrente |
| seek | la posizione salta di oltre 4 s rispetto all'atteso | riparte, `reason=seeked` |
| cambio traccia | `selected_audio_track` cambia | riparte sulla nuova traccia |
| fine file / stop | il servizio non è più riproducibile | ferma ffmpeg, stato `idle` |
| ricevitore sparito | nessuna richiesta di control per 8 s | ferma ffmpeg |
| anticipo assurdo | il ricevitore riporta uno scarto > 12 s | riparte, al massimo 3 volte, poi lascia stare |

L'ultima riga è una correzione di questo gate: un ricevitore semplicemente lento
non si aggiusta facendo ripartire il relay, e riprovare a oltranza ha prodotto
70 riavvii in 90 secondi. Dopo tre tentativi il relay dichiara
`receiver_cannot_keep_up` e continua a trasmettere.

## Perdite, riconnessione, contropressione

- ffmpeg che esce viene fatto ripartire dopo 2 s, con backoff esponenziale fino
  a 30 s per i fallimenti immediati (un ricevitore annunciato ma non in ascolto
  ha prodotto settecento tentativi prima che qualcuno se ne accorgesse).
- Il ricevitore mantiene una coda limitata (40 chunk da 160 ms ≈ 6,4 s). Piena
  è il funzionamento normale: è il margine che regge il jitter di rete.
- Il TS non ha ritrasmissione a livello applicativo: un pacchetto perso è audio
  perso, e si vede come errore del decoder nella telemetria. Su LAN cablata non
  se ne sono osservati.

## Cosa non è definito qui

`source_type` diverso da `file`. DVB, IPTV e YouTube sono stati solo studiati,
non implementati.

`source_type` viene dedotto dal riferimento che Enigma2 dichiara corrente: il
primo campo `4097` con un percorso è un file, `4097` con un URL è uno stream,
`1` è un servizio broadcast. Era una costante `file`, il che rendeva inutile il
controllo qui sotto — andando su un canale il ricevitore avrebbe accettato audio
che il relay non sa produrre.

Il ricevitore verifica `protocol` e `source_type` a ogni lettura del control
plane e rifiuta ciò che non conosce: all'apertura con un errore esplicito, in
corsa svuotando la coda e smettendo di accettare audio invece di etichettarlo
male. Se i due campi mancano del tutto la richiesta viene accettata — un box con
un plugin più vecchio resta utilizzabile — ma un valore *diverso* non viene mai
ignorato.
