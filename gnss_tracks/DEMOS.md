# Demo con flotte e incroci

Selezionare nella dashboard:

- `gnss_tracks/demo_grid_6_trains/demo_grid_6_trains.csv`: 6 treni, 9 incroci a raso, 197 campioni a 1 Hz.
- `gnss_tracks/demo_grid_8_trains/demo_grid_8_trains.csv`: 8 treni, 16 incroci a raso, 245 campioni a 1 Hz.

Ogni scenario ha treni su linee orizzontali e verticali, con versi alternati, incroci distanti 240 m e velocità di 5 m/s (18 km/h). Tutti i treni condividono sessione e orologio. Le intersezioni hanno una zona di ingombro di 30 m; le tracce di geometria separate mantengono il riferimento corretto per ciascun treno.

Anomalie del primo treno, in secondi dall'inizio:

| Intervallo | Anomalia iniettata | Effetto |
|---|---|---|
| 30–69 | tunnel | Fix assente, 0 satelliti, crowd assente, varianza sensori crescente |
| 80–109 | spoofing | Posizione GNSS spostata di 180 m verso est |
| 120–144 | multipath | Oscillazione GNSS verso est/ovest con ampiezza 70 m |
| 150–174 | low_satellites | 3 satelliti, HDOP 8 |

Per il treno di indice i (partendo da zero), gli intervalli sono ritardati di 3 × i secondi. Le etichette nel CSV descrivono le anomalie iniettate, non garantiscono la stessa diagnosi del modello. Il replay rimuove le etichette prima dell'inferenza.

I file `.scenario.json` descrivono flotta, percorsi e transiti; i `.vehicle.csv` sono tracce ausiliarie escluse dal selettore. Le reti sono in `infrastructure/network/*.network.json`. Nessun modello viene modificato o riaddestrato.

Rigenerazione: `.venv/bin/python -m tools.generate_fleet_demos`.

Dopo una modifica al supporto del replay: `docker compose up -d --build simulation alerts`.

## Bivio · incertezza e occupazioni

Selezionare `gnss_tracks/scenario_bivio.csv`, avviare l'analisi e premere
**Centra posizioni** per mantenere il confronto in vista. La traccia contiene
solo TRAIN_001: gli altri treni sono destinatari fittizi, senza coordinate.
I dati sono un estratto invariato della traccia `track_000_mixed.csv`.

- 0–19 s: avvicinamento prima dello spoofing.
- 20 s: inizio dell'episodio di spoofing originale.
- 45 s: l'ellisse coinvolge il bivio.
- 46 s: viene coinvolto il binario parallelo a 25 m.
- 48 s: vengono coinvolti attraversamento e diramazione.
- Dal recupero GNSS: risoluzione dopo evidenza stabile di separazione.

Il binario esterno a 450 m e il cavalcavia di livello 1 restano esclusi.
Gli avvisi riportano destinatari virtuali TRAIN_B2, TRAIN_P3 e TRAIN_C4;
la consegna è simulata dal servizio allarmi locale, senza contatti esterni.
Anche la traccia completa originale usa questa stessa rete.
Rigenerazione: `python -m tools.generate_track_network`.
