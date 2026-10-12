# Mockup ferroviario GNSS

Replay di tracce CSV → RabbitMQ → indicatori e Random Forest → dashboard Web STOMP.
L’obiettivo generale è in [GOAL.md](GOAL.md); il mockup usa tre servizi applicativi e un broker, senza database o gateway.

## Preparazione offline

Servono Python 3.14 e Docker Compose. Dalla radice del repository:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m tools.process_dataset
python -m tools.train_random_forests
```

I CSV sorgente in `dataset/{train,validation,test}/<rotta>/` devono essere già presenti. Per generarli manualmente, vedere [DATA_GENERATION.md](DATA_GENERATION.md). Il replay non avvia mai generazione o training.

Il preprocessing addestra Isolation Forest solo sulle tracce interamente nominali del train, salva `models/preprocessing.joblib` e rigenera `dataset_processed/`. Ogni CSV elaborato contiene timestamp, cinque indicatori `risk_*` e label originale. Non contiene un rischio finale deterministico. La configurazione opzionale `--config file.json` accetta `cfg` e `speed_to_ms`; per questi dataset le velocità sono già in m/s.

Il training salva `models/random_forest_models.joblib`: RF binaria per `final_risk`, RF multilabel per spoofing, jamming, low satellites, multipath e tunnel. Il rischio usato nel training della seconda RF è ottenuto out-of-fold con gruppi per rotta. Imputatori, colonne e soglie sono inclusi nel bundle. Validation e test non partecipano al training. Le probabilità RF non sono calibrate.

Le versioni ML sono fissate in `services/classification/requirements.txt`, condiviso con l’ambiente offline. Rigenerare entrambi i modelli quando si cambiano preprocessing o dipendenze. In alternativa si può usare direttamente l’immagine runtime per ottenere lo stesso ambiente:

```bash
docker compose build classification
docker compose run --rm --no-deps -v "$PWD:/app" -w /app classification python -m tools.process_dataset
docker compose run --rm --no-deps -v "$PWD:/app" -w /app classification python -m tools.train_random_forests
```

## Avvio

```bash
cp .env.example .env
docker compose up --build
```

- Dashboard: [localhost:8080](http://localhost:8080).
- Management RabbitMQ: [localhost:15672](http://localhost:15672), account dimostrativo `admin` / `admin_demo`.
- Web STOMP: `ws://localhost:15674/ws`.

Sono avviati `rabbitmq`, `simulation`, `classification` e `frontend`. Modelli, dataset e geometrie sono montati in sola lettura. I dati del broker restano nel volume `rabbitmq_data`. Per fermare lo stack mantenendoli: `docker compose down`.

Le credenziali di simulazione, classificazione e frontend sono distinte, configurabili in `.env` e limitate per risorsa e routing key. Il frontend può sottoscrivere solo gli aggiornamenti classificati e non può pubblicare sull’exchange. Le credenziali dimostrative ammettono lettere, numeri, `_` e `-`; quelle del browser sono visibili per definizione. La configurazione browser viene generata all’avvio del container. Usare per `WEB_STOMP_URL` un indirizzo raggiungibile dal browser. Se cambia `WEB_STOMP_PORT`, aggiornare anche l’URL.

Le definizioni vengono importate all’avvio del broker. Dopo una modifica alle credenziali ricreare il broker e i servizi applicativi con `docker compose up -d --force-recreate`.

## Scelta del file dalla dashboard

Il menu **File CSV da analizzare** elenca le tracce valide in `gnss_tracks/` e `dataset/`, escludendo geometrie e ground truth. **Analizza dall’inizio** interrompe il replay corrente e avvia una nuova sessione dal primo campione del file scelto, senza riavviare i container. La scelta è condivisa fra le dashboard collegate. Anche selezionare nuovamente lo stesso file avvia una sessione nuova.

La mappa mostra tre elementi distinti:

- **Tratteggio grigio chiaro:** geometria completa del percorso previsto, caricata dal file `geometry.csv` o `<traccia>.geometry.csv`. Non è la cronologia delle misure GPS.
- **Marcatore blu GPS:** ultima posizione del ricevitore GNSS del mezzo; viene nascosto quando manca il fix.
- **Marcatore viola Media utenti:** ultima posizione media dei telefoni a bordo, dai campi `crowd_latitude` e `crowd_longitude`, con il numero di telefoni `crowd_num_phones`. È indipendente da `estimated_*` e continua ad aggiornarsi anche senza fix del mezzo.

**Inquadra percorso** mostra l’intera traccia; **Centra posizioni** avvicina i due marcatori. I pannelli laterali distinguono le coordinate GPS da quelle della media utenti.

Il simulatore espone un piccolo controllo HTTP interno sulla porta 8081, raggiunto tramite Nginx: `GET /api/tracks`, `GET /api/geometry?file=…` e `POST /api/select` con `{"file_id": "…"}`. Non sono aggiunti container; i campioni e i risultati ML continuano a passare da RabbitMQ. Il catalogo viene letto all’avvio del simulatore: dopo aver aggiunto CSV, riavviare `simulation` e ricaricare la pagina.

## Traccia iniziale e velocità

`.env` configura la traccia iniziale e la velocità, ad esempio:

```dotenv
CSV_PATH=/data/dataset/test/route_0006/scenario_06.csv
VEHICLE_ID=TRAIN_001
REPLAY_SPEED=1
REPLAY_LOOP=true
```

Poi applicare: `docker compose up -d --force-recreate simulation`.

`REPLAY_SPEED=1` riproduce gli intervalli originali; `2` raddoppia la velocità. A ogni ripetizione viene creato un nuovo `session_id`; `sequence` riparte da zero. Il simulatore cerca `geometry.csv` nella cartella della traccia o `<nome>.geometry.csv`. `GEOMETRY_ID` permette di indicare esplicitamente il percorso relativo a `/data`, ad esempio `gnss_tracks/track_000_mixed.geometry.csv`.

Il simulatore trasmette tutti i campi sensoriali, senza `anomaly_label` né `truth_*`. I valori mancanti diventano `null`; le velocità restano in m/s fino all’adattamento del JSON per la dashboard, che usa km/h. Il riferimento deriva da `estimated_*`, mai dalla ground truth.

## Comportamento del mockup

La classificazione carica una volta i due artefatti e conserva il prefisso della sessione corrente per ogni veicolo. Ricalcola gli indicatori sul prefisso, emette solo l’ultima riga e libera il buffer precedente al cambio di sessione. È una scelta per tracce dimostrative brevi: costo e memoria crescono durante il replay. I buffer non sopravvivono al riavvio del processo.

I primi due campioni producono `ATTENTION`, `warmup=true`, rischio e confidence non disponibili. Dal terzo si esegue l’inferenza; indicatori ancora mancanti vengono trattati dagli imputatori RF. Si assumono CSV con timestamp regolari e fix presente durante l’inizializzazione. Gap vengono segnalati nei log; campioni non validi o timestamp incompatibili con il preprocessore vanno in DLQ.

Il fix assente produce `UNRELIABLE` senza modificare il valore RF. Negli altri casi le soglie di `RISK_THRESHOLDS=0.2,0.5,0.8` separano NOMINAL, ATTENTION, DEGRADED e UNRELIABLE. `confidence_score = 100 × (1 − final_risk)` è un indice dimostrativo.

Riprendi/Sospendi aggiornamenti collegano o scollegano soltanto la dashboard: il replay continua. Analizza dall’inizio cambia invece la traccia del simulatore. Ogni browser ha una propria coda temporanea e riceve tutti gli aggiornamenti successivi alla connessione. Non viene recuperata la cronologia precedente. Il cambio di sessione azzera percorsi, grafico ed eventi. La media utenti si aggiorna anche senza fix GNSS; il segnale è mostrato in dB-Hz e tutte le anomalie multilabel sono conservate. Mappa e librerie frontend richiedono accesso Internet ai rispettivi CDN.

## Messaggistica

L’exchange topic durevole `gnss.events` usa:

| Routing key | Destinazione |
| --- | --- |
| `telemetry.raw.v1` | Coda durevole `classification.raw.v1` |
| `telemetry.classified.v1` | Una sottoscrizione Web STOMP per browser |

Entrambi i publisher usano conferme. La classificazione invia l’ACK solo dopo la pubblicazione confermata. Le sequenze già consegnate vengono ignorate; un risultato in attesa di conferma viene ritentato. Sono presenti riconnessioni automatiche. I messaggi invalidi vengono rifiutati senza requeue e finiscono in `classification.raw.dlq`, consultabile dal management.

Un servizio futuro può aggiungere una propria coda con binding all’exchange. Le sottoscrizioni browser usano `/exchange/gnss.events/telemetry.classified.v1`, secondo la [documentazione STOMP RabbitMQ](https://www.rabbitmq.com/docs/stomp).

## Struttura e verifica

- `services/`: simulazione, classificazione e frontend Nginx.
- `shared/`: preprocessing, inferenza, contratti e connessione AMQP.
- `tools/`: generazione, valutazione, preprocessing e training offline. Avviare con `python -m tools.<nome>` dalla radice.
- `infrastructure/rabbitmq/`: plugin, utenti, permessi, exchange, coda e DLQ.
- `models/` e `dataset_processed/`: artefatti rigenerabili, ignorati da Git.
- `notebooks/gnss_reliability.ipynb`: analisi tramite gli stessi moduli condivisi. Installare separatamente `requirements-notebook.txt`; gli strumenti cartografici preesistenti usano `requirements-maps.txt`.

```bash
python -m unittest discover -s tests -v
docker compose config --quiet
docker compose logs -f simulation classification
```

Con lo stack attivo, verificare anche due sottoscrizioni Web STOMP e la DLQ:

```bash
pip install -r requirements-test.txt
python tests/smoke_compose.py
python tests/smoke_selection.py
```

Il primo smoke test pubblica tre campioni con un veicolo dedicato e lascia un messaggio di prova non valido nella DLQ. Il secondo cambia due tracce via HTTP, verifica geometria, nuova sessione, media utenti e risultati ML via Web STOMP, quindi riavvia il file precedentemente selezionato.

Prima del refactor, le due cartelle dei dati elaborati avevano 144 percorsi comuni con contenuti differenti. La vecchia `dataset_processed/` e i sorgenti modificati sono conservati in `.refactor-backup/before-refactor/`; `processed_dataset/` resta intatta e fuori dal flusso attivo. Le altre risorse esistenti restano fuori dalle immagini runtime.

## Localizzazione incerta e allarmi ferroviari

Selezionare **Demo ferroviaria · scambio e tre mezzi** (`gnss_tracks/demo_switch.csv`)
nel selettore e premere **Analizza**. Il replay dura 181 secondi virtuali:
TRAIN_001 perde il fix dal secondo 25 al 104, la deriva allarga l’ellisse sui due
rami dello scambio, TRAIN_002 ha un transito previsto entro cinque minuti e
TRAIN_003 è previsto oltre l’orizzonte. Il cavalcavia a livello 1 è distinto dai
binari a livello 0. I tre mezzi condividono sessione e timestamp. Il selettore
mezzo cambia il dettaglio; gli allarmi e le occupazioni rimangono visibili per
l’intera sessione. Le occupazioni del binario percorso possono restare attive:
la chiusura dello scambio non significa che l’intero treno sia sparito dalla rete.

`tools/generate_alert_demo.py` rigenera il fixture e il catalogo
`infrastructure/network/demo.json`. Il catalogo contiene identificativi,
polilinee complete, collegamenti, tipo e livello fisico. Le alternative vengono
cercate nell’intero catalogo, senza filtrarle sulla rotta assegnata. Per CSV diversi
da questa demo il catalogo non è associato: viene segnalata la selezione
incompleta e richiesta verifica alla sala di controllo, senza inventare una rete.

### Fonti e area plausibile

Verificato `tools/generation.py:simulate_estimated_state`: `estimated_*` è un
posterior sintetico dei sensori, calcolato separatamente dalla simulazione degli
utenti e senza incorporarne le misure. Il replay dei CSV generati aggiunge
`estimated_includes_crowd=false`, `estimated_timestamp` e `crowd_timestamp` sul
clock della riga; eventuali timestamp espliciti vengono preservati. Un produttore
esterno deve dichiarare realmente questi metadati: assenza di freschezza o
provenienza nota rende la fonte inutilizzabile. La varianza di posizione esportata
include già la deriva del generatore: non viene sommata una seconda volta.

La fusione avviene nel piano metrico locale, con pesi inversi alla varianza
conservativa. Usa `crowd_pos_std_m`, `crowd_accuracy_m`, numero di telefoni,
freschezza, `estimated_speed_var_ms2` e `estimated_heading_var_deg2`.
L’errore iniziale dell’ultimo GNSS affidabile è conservato come minimo della
varianza sensoriale. Velocità, direzione e tempo senza riferimento affidabile
propagano assi longitudinali e laterali distinti. Le fonti discordanti producono
ellissi separate, senza marcatore intermedio. Un fix anomalo non è affidabile;
il ritorno richiede misure coerenti consecutive e controlli di movimento/fonti.
Il confidence score ML rimane dimostrativo, non è una probabilità di contenimento.
Gli assi visualizzati sono estensioni euristiche pari a tre deviazioni stimate,
**non** intervalli calibrati. Applicazione locale ferroviaria; nessuna gestione
aeronautica o garanzia di sicurezza operativa.

### Persistenza, consegne e configurazione

`alerts` consuma `telemetry.classified.v1` dalla coda durevole
`alerts.classified.v1` e pubblica `alerts.snapshot.v1`. Ha credenziali e permessi
separati. SQLite nel volume `alerts_data` conserva occupazioni, identificativi,
stati di notifica e outbox transazionale attraverso riavvii e riconnessioni.
La pubblicazione ha conferma broker, la telemetria viene confermata solo dopo
salvataggio e pubblicazione; messaggi non validi vanno in `alerts.dlq`.
Il browser ripristina lo snapshot da `/api/alerts?session=...` e può confermare
la presa in carico della sala tramite `/api/alerts/ack`. La presa in carico non
risolve il rischio. Queste API, come il selettore replay, sono per la demo locale.

Il destinatario simulato restituisce una ricevuta esplicita solo dopo il numero
configurato di fallimenti; la conferma AMQP non equivale a «Avviso ricevuto».
Il primo tentativo fallisce per default, il retry avviene dopo cinque secondi di
replay. Non vengono inviate notifiche a mezzi o operatori reali. Le notifiche
restano etichettate **Notifica simulata** anche dopo consegna e presa in carico.
I destinatari sono aggiornati anche quando cambia la telemetria degli altri mezzi.
Le occupazioni hanno fine ignota finché non escluse: un arrivo oltre l’orizzonte
è escluso dalla selezione attuale, non dichiarato sicuro per sempre.

| Parametro | Default | Uso |
|---|---:|---|
| `GNSS_RECOVERY_S` | 10 | Tempo di misure coerenti prima del ritorno GNSS |
| `SOURCE_MAX_AGE_S` | 15 | Età massima di sensori e utenti |
| `MAX_UNCERTAINTY_M` | 2000 | Semiasse oltre cui dichiarare localizzazione non determinabile |
| `ALERT_HORIZON_S` | 300 | Orizzonte di selezione degli arrivi |
| `CLEARANCE_STABILITY_S` | 10 | Stabilità necessaria per liberare una zona |
| `CLEARANCE_MARGIN_M` | 10 | Margine metrico addizionale per escludere occupazione |
| `SIMULATED_DELIVERY_FAILURES` | 1 | Invii simulati falliti prima della ricevuta |

La soglia di rappresentabilità non taglia la geometria usata dagli allarmi.
Senza nuove informazioni affidabili le occupazioni non scadono. La prima versione
è deliberatamente conservativa: libera una zona solo con GNSS recuperato e
separazione stabile; non deduce esclusione dalla sola stima inerziale. In assenza
di livello considera tutte le infrastrutture geometricamente compatibili e
segnala dati incompleti. Le connessioni sono catalogate per una futura propagazione
su corridoi; ora le ellissi coinvolgono anche rami alternativi. Il catalogo non è
una mappa ferroviaria certificata. Gli stati storici sono conservati senza TTL;
per uso prolungato serviranno archiviazione e politica di retention. Il classificatore
mantiene lo stato in memoria durante le riconnessioni; un suo riavvio richiede
nuovo warmup, mentre le occupazioni restano persistenti nel servizio allarmi.

Verifiche: `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'`.
I casi `test_localization_alerts.py` coprono fix anomalo, perdita/recupero,
freschezza/provenienza mancanti, disaccordo, deriva, intersezione degli interni
segmento, cavalcavia, due rami, permanenza e chiusura stabile, orizzonte temporale,
duplicati, mancata consegna, ricevuta/presa in carico, sessioni e outbox dopo riavvio.
