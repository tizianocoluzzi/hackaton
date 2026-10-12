SCHEDA SFIDA
MERMEC ENGINEERING SRL (MME)
Safe Mobility — Resilienza PNT/GNSS e continuità operativa

TITOLO DELLA CHALLENGE
Awareness e mitigazione di anomalie PNT/GNSS per la mobilità sicura

OBIETTIVI DELLA CHALLENGE

–  Sviluppare un concept o prototipo software capace di monitorare indicatori GNSS/PNT e, se utile, confrontarli con altre

fonti di contesto.

–  Rilevare anomalie o incoerenze nella posizione, nella velocità, nella traiettoria o nel timing, incluse possibili situazioni di

spoofing, jamming e perdita di qualità del segnale.

–  Stimare il livello di affidabilità della soluzione di navigazione e rendere evidente il passaggio da uno stato nominale a uno

stato degradato o non affidabile.

–  Generare alert, registrare gli eventi e suggerire una possibile azione di fallback o degradazione controllata, a supporto

dell’operatore o del sistema.

SINTESI DELLA CHALLENGE

I  team  dovranno  proporre  una  soluzione  che  aumenti  la  resilienza  della  localizzazione  in  scenari  di mobilità, combinando
indicatori  GNSS/PNT  con  eventuali  altre  fonti  di  contesto  o  logiche  di  coerenza.  Non  è  necessariamente  richiesta  una
soluzione hardware o specialistica a livello di radiofrequenza (RF).

I partecipanti possono concentrarsi sulla logica di rilevamento delle anomalie, sulla valutazione dell’affidabilità, sulla gestione
operativa del rischio e sulla rappresentazione chiara delle informazioni. Saranno valorizzate le proposte che rendano evidente
il passaggio da stato nominale a stato degradato e la relativa gestione operativa.

CONTESTO (BACKGROUND)

Nei sistemi di mobilità terrestre, ferroviaria, marittima o aerea, i servizi di posizionamento, navigazione e timing (PNT) basati
su  GNSS  possono  essere  essenziali  per  localizzare  correttamente  un  mezzo,  sincronizzare  sistemi  e  supportare  decisioni
operative.  La  posizione  fornita  dal  ricevitore  non  dovrebbe  però  essere  considerata  sempre  automaticamente  affidabile:
interferenze,  degradazione  del  segnale,  errori,  jamming  o  spoofing  possono  ridurne  la  qualità  o  generare  informazioni  di
localizzazione incoerenti.

La  call  Safe  Mobility richiama l’uso di dati satellitari e il ruolo della navigazione satellitare per una localizzazione accurata e
affidabile dei mezzi su infrastrutture ferroviarie e stradali. La dipendenza dei sistemi di trasporto basati su AI dallo scambio di
dati in tempo reale, inclusi quelli satellitari, rende la resilienza PNT/GNSS coerente con questo perimetro.

ESEMPI DI SCENARI NON SENSIBILI

1.  Perdita  progressiva  della  qualità  GNSS.  Un  mezzo  percorre  una  tratta  con  posizione  inizialmente  stabile;  in  seguito
diminuiscono  i  satelliti  utilizzabili,  peggiorano  gli  indicatori  di  accuratezza  e  compaiono  discontinuità  nella  traiettoria.  Il
sistema dovrebbe riconoscere il degrado, ridurre il confidence score e avvisare l’operatore.

Safe Mobility Hack 2026 — Scheda Sfida · MERMEC Engineering Srl (MME)

Pag. 1 / 2

2.  Possibile  spoofing  o  incoerenza  di  localizzazione.  La  posizione  GNSS  presenta  un  salto  improvviso  incompatibile  con
velocità,  direzione  e  dinamica  del  mezzo.  La  soluzione  dovrebbe  confrontare  i  dati  disponibili,  classificare  l’anomalia  e
proporre il passaggio allo stato di posizione non affidabile.

3. Possibile jamming o perdita del servizio. In un intervallo temporale il ricevitore perde rapidamente qualità o disponibilità
del  fix.  Il  sistema  dovrebbe  registrare  l’evento  e  attivare  una  logica  di  fallback:  ad esempio mantenere temporaneamente
l’ultima posizione valida, utilizzare una stima alternativa simulata oppure limitare alcune funzionalità.

DATI PER LA PROTOTIPAZIONE

I team potranno generare autonomamente dati sintetici, senza alcun riferimento a mezzi, tratte o informazioni operative reali.
È possibile adottare, a titolo di esempio, un semplice tracciato CSV con i seguenti campi:

timestamp, latitude, longitude, speed, heading, satellites_used, hdop, fix_status, signal_quality, reference_position_available,
anomaly_label

Le sequenze sintetiche potranno alternare tratti nominali a eventi simulati di perdita di qualità, salto anomalo della posizione,
deriva progressiva o assenza del fix. Il tracciato è esemplificativo e non impone un metodo specifico: l’obiettivo è dimostrare il
rilevamento dell’anomalia, l’attribuzione di un livello di affidabilità e la gestione del passaggio a uno stato degradato.

OUTPUT ATTESO

L’output potrà essere un mockup, un prototipo o una proof of concept (PoC) che mostri:

–  Acquisizione e analisi del flusso dati.

–  Classificazione dello stato, ad esempio nominale, attenzione, degradato e non affidabile.

–  Confidence score o indicatore equivalente dell’affidabilità.

–  Alert e logging degli eventi.

–  Proposta di fallback o supporto decisionale.

–  Dashboard o altra rappresentazione chiara del comportamento del sistema.

RISORSE DISPONIBILI

–  Brief sul contesto industriale e sul caso d’uso, con gli scenari non sensibili descritti nella scheda.

–  Indicazioni per la generazione autonoma di dati sintetici e un esempio di struttura CSV utilizzabile per la prototipazione.

–  Compatibilmente con le disponibilità organizzative durante l’hackathon, referenti MME potranno fornire chiarimenti

tecnico-funzionali e un confronto generale sulla coerenza delle soluzioni proposte.

Safe Mobility Hack 2026 — Scheda Sfida · MERMEC Engineering Srl (MME)

Pag. 2 / 2

