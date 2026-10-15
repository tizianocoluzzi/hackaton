# Demo: degrado progressivo GNSS e avvisi ai treni vicini

Selezionare `gnss_tracks/demo_degrado_progressivo/demo_degrado_progressivo.csv`
nella dashboard e premere **Analizza dall’inizio**. Usare replay a velocità 1×.
Durata: 60 secondi, 61 campioni per mezzo, tre treni sincronizzati.
Se i servizi erano già avviati prima dell'aggiunta dei dati:

```sh
docker compose restart simulation alerts
```

Ricaricare la dashboard per aggiornare il catalogo.

| Tempo | Cosa mostra TRAIN_001 |
| --- | --- |
| 0–19 s | GPS coerente con sensori e utenti. Primi due campioni di warmup; dal secondo 2 rischio inferiore al 2%. Localizzazione GNSS affidabile dal secondo 12, dopo l'isteresi prevista. |
| 20–27 s | Degrado: scostamento laterale di 5,3 m, 11 satelliti, HDOP 2. Rischio medio 48,8%, prevalentemente 46%, con transitorio iniziale 62%. |
| 28–60 s | GPS errato di 300–556 m, rischio oltre 98%, stato UNRELIABLE. La localizzazione passa ai sensori; la media utenti non è disponibile. |
| 28 s | Il semiasse laterale dell'incertezza supera 90 m e comprende i due binari a ±40 m con TRAIN_002 e TRAIN_003. Entrambi diventano destinatari degli avvisi. |
| 33 s | Con il retry predefinito, gli avvisi risultano consegnati ai due treni (notifiche simulate). |

Gli altri due treni mantengono GNSS nominale e viaggiano affiancati a 5 m/s.
La rete sintetica ha tre binari paralleli allo stesso livello fisico.
Il ricevitore del primo treno continua a dichiarare fix 3D anche quando la posizione
è completamente errata: è il modello a rilevare l'inaffidabilità.

Il 50% è il rischio ML dimostrativo, non una percentuale di errore metrico.
Le percentuali sono ottenute dai modelli presenti, senza inserire punteggi nel CSV.
`verification.json` registra il replay offline causale (inferenza sul solo prefisso
 disponibile a ogni secondo), localizzazione, destinatari e ricevute simulate.
La verifica usa la pipeline esistente e le impostazioni predefinite; non modifica
codice, modelli, soglie o tracce precedenti.
