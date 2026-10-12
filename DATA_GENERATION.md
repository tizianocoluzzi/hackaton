# Generazione sintetica ferroviaria

`tools/generation.py` genera una sequenza GNSS, dati aggregati dei telefoni e lo stato stimato indipendente. Non implementa un filtro Kalman reale: campiona un modello statistico della sua uscita ipotetica. Nessuna misura GNSS alterata alimenta la stima alternativa. Non vengono esportati accelerometro, giroscopio o velocità delle ruote.

## Uso

```bash
python -m tools.generation --seed 42 --write-events --output-dir gnss_tracks
python -m tools.generate_dataset --seed 42 --routes-per-split 5 --output-dir dataset
python -m tools.evaluate_dataset dataset --split validation
python -m tools.evaluate_dataset dataset --split test
```

Ogni esecuzione di `tools/generation.py` scrive il CSV temporale, `.geometry.csv` e `.config.json`; `--write-events` aggiunge gli eventi. La geometria contiene distanza lungo percorso (m), latitudine/longitudine (gradi), direzione (gradi orari da nord), curvatura (1/m). È la stessa geometria usata per la dinamica, senza rumore: è un riferimento ideale sintetico, non una misura. Per simulare una mappa rilevata servirebbe un modello di errore cartografico distinto.

## Stato alternativo e rumore

Campi `estimated_latitude`, `estimated_longitude`, `estimated_speed`, `estimated_heading` e varianze `estimated_pos_var_m2` (per ciascun asse ENU), `estimated_speed_var_ms2`, `estimated_heading_var_deg2`. Le varianze restano in unità SI anche usando `--speed-unit kmh`; entrambe le velocità esportate cambiano unità. La velocità stimata è una componente longitudinale con segno: piccoli valori negativi sono possibili da fermo.

La posizione aggiunge due processi Gauss-Markov indipendenti e una deriva random walk. La varianza per asse cresce come `state_pos_sigma_m² + t * state_drift_m_sqrt_s²`. Velocità e direzione hanno rumore temporalmente correlato. Queste sono varianze marginali del modello sintetico, non covarianze calcolate da un filtro implementato; le covarianze incrociate del modello sono zero. Nessuna correzione tramite stazioni/balise viene applicata: `reference_position_available` resta solo un indicatore. Su tratte lunghe la deriva può diventare rilevante.

GNSS e telefoni conservano i loro modelli di rumore, disponibilità e anomalie. I telefoni sono mantenuti; normalmente solo il tunnel li influenza. Non si tratta di dati reali calibrati: validare su misure reali prima di dedurre prestazioni operative.

## Configurazione

`--help` elenca i parametri CLI. `--config config.json`, supportato da entrambi i generatori, permette di impostare tutti i campi di `TrackConfig` con i nomi Python presenti in `tools/generation.py`. Nel generatore singolo il JSON prevale sui corrispondenti parametri CLI; tipo del percorso viene scelto con `--track-type`. Nel dataset le forme vengono alternate automaticamente tra i cinque tipi. Esempio:

```json
{
  "duration_s": 600,
  "rate_hz": 2,
  "pos_noise_m": 2.5,
  "speed_noise_ms": 0.1,
  "cn0_noise": 1.5,
  "phone_sigma_m": 8,
  "state_pos_sigma_m": 3,
  "state_speed_sigma_ms": 0.2,
  "state_heading_sigma_deg": 1.5,
  "state_corr_s": 30,
  "state_drift_m_sqrt_s": 0.2,
  "geometry_step_m": 1
}
```

Nel dataset `--event-params eventi.json` accetta un oggetto come `{"spoofing":{"mode":"jump","magnitude":100},"multipath":{"sigma":12}}`.

I parametri degli eventi sono modificabili nel generatore singolo con `--anomaly 'spoofing:60:30:mode=jump,magnitude=100'`; vedere le funzioni `apply_*` per tutti i parametri disponibili. I parametri del rumore di sorgente sono in `TrackConfig`; rimangono costanti interne dei modelli preesistenti, quindi il modello non espone ancora ogni coefficiente numerico come configurazione. Il flag storico `--no-imu` ora disabilita lo stato stimato; `enable_imu` mantiene lo stesso significato nel JSON.

## Dataset e prevenzione del leakage

Per ogni percorso: 1 scenario nominale, 5 errori isolati, 10 coppie di errori sovrapposti. Comprende spoofing, jamming, low_satellites, multipath, tunnel. Non include anomalie di timing. Posizioni temporali casuali riproducibili; `--event-duration-s`, `--margin-s`, `--overlap-fraction` controllano durata, margini e sovrapposizione. La durata deve essere sufficiente a osservare gli eventi alla frequenza scelta.

La struttura divide i percorsi in train/validation/test prima di produrre le varianti. Tutte le varianti di un percorso condividono geometria e stato alternativo. Non dividere casualmente righe o finestre di queste sequenze tra partizioni. Il manifest registra configurazioni, seed, eventi, split e numero di campioni. I file `.truth.csv` separano la verità dai dati di input; `anomaly_label` nel CSV temporale è il target e va escluso dalle caratteristiche, come nomi dei file, seed ed eventi del manifest. Le etichette con `+` rappresentano target multilabel.

## Valutazione senza modello ML

`tools/evaluate_dataset.py` confronta il GNSS con lo stato alternativo tramite distanza locale ENU e controlla fix, HDOP e satelliti. È una baseline binaria configurabile, non una classificazione dei cinque errori e non un modello addestrato. Restituisce TP/FP/TN/FN, precision, recall, F1 e conteggi per tipo (gli eventi sovrapposti contribuiscono a entrambi i tipi). Se il denominatore è zero la metrica è `null`. Richiede lo stato stimato abilitato.

Regolare le soglie sulla validation, poi fissarle per il test. Esempio: `--position-threshold-m 25 --hdop-threshold 5 --min-satellites 4`. Le metriche sono per campione, non per evento; non misurano latenza di rilevamento o affidabilità operativa. Gli scenari sono bilanciati per numero di sequenze, non necessariamente per campioni di classe. Nessun modello finale viene addestrato.
