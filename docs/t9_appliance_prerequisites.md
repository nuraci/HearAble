# T9 Plus — di che cosa ha bisogno l'appliance

Debian 13 netinst è spartana di proposito, e ogni pacchetto qui sotto è stato
installato perché qualcosa si è rotto senza. L'elenco è diviso per quello che
serve davvero a far comparire i sottotitoli, quello che serve solo a compilare,
e quello che serve solo a misurare.

## REQUIRED_RUNTIME — senza questi non ci sono sottotitoli

```bash
sudo apt install -y ffmpeg python3-numpy python3-aiohttp curl
```

| pacchetto | perché |
|---|---|
| **`ffmpeg`** | decodifica l'AAC del transport stream in PCM. Senza, il tap del decoder apre la connessione, manda 4,5 KB e si prende un *broken pipe*: il sintomo non nomina mai la causa, ed è costato un giro di diagnosi |
| `python3-numpy` | i chunk audio della catena |
| `python3-aiohttp` | il server di `hearable.realtime_server` |
| `curl` | il lanciatore accende HearAble sul decoder; senza, falliva in silenzio perché la chiamata era protetta da `|| true` |

## REQUIRED_RUNTIME per la rete privata e l'accensione

```bash
sudo apt install -y ethtool
```

`ethtool` riapplica il Wake-on-LAN a ogni avvio: il driver `r8169` lo riazzera, e
senza questa riga il magic packet smette di funzionare al primo riavvio.

## REQUIRED_RUNTIME per il ring RGB (accessorio)

```bash
sudo apt install -y python3-serial
```

Se manca, `hearable-rgb` lo dice una volta nel journal e continua senza ring. I
sottotitoli non se ne accorgono: è la condizione che il mandato chiede
esplicitamente.

## REQUIRED_BUILD — solo per compilare il runtime ASR

```bash
sudo apt install -y build-essential cmake ninja-build pkg-config git
```

Su questa macchina c'erano già tutti tranne nessuno: l'immagine installata li
aveva. `libsentencepiece-dev` **non** serve: `scripts/build_t9_n95_cpu.sh`
compila sentencepiece dal commit fissato, come fa il PC.

## OPTIONAL_DIAGNOSTICS — utili a misurare, inutili a funzionare

```bash
sudo apt install -y rsync time linux-perf lm-sensors
```

| pacchetto | perché |
|---|---|
| `rsync` | trasferire sorgenti e modello dal PC |
| `time` | `/usr/bin/time -v` per RSS di picco e CPU% nelle campagne |
| `linux-perf`, `lm-sensors` | profilazione e temperature; `sensors` era già presente |

## Installato e poi rimosso

`dnsmasq` è servito **due minuti**, per rimettere in piedi il decoder quando il
cavo spostato lo aveva lasciato senza rete e il suo Wi-Fi non era ancora buono:
la sua `eth0` era in DHCP, gli è stato dato `10.77.0.1` e da lì si è potuto
configurare in statico. Poi è stato fermato e la configurazione rimossa. **Sul
percorso critico non c'è DHCP**, che è quello che il mandato chiede.

## Verifica rapida

```bash
for p in ffmpeg curl ethtool; do command -v $p >/dev/null || echo "manca $p"; done
python3 -c "import numpy, aiohttp, serial" || echo "manca un modulo python"
```
