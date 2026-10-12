"use strict";

// ==========================================
// CONFIGURAZIONE
// ==========================================

const CONFIG = {
    ...window.RUNTIME_CONFIG,
    destination: "/exchange/gnss.events/telemetry.classified.v1",
    maxEvents: 100
};

const COLORS = {
    red: "#781e3a",
    green: "#25c78a",
    yellow: "#ffbd45",
    danger: "#ff5268",
    text: "#29252a",
    muted: "#6d6870",
    grid: "#eae7e9"
};

const $ = id => document.getElementById(id);

let stompClient = null;
let lastStatus = null;
let lastAnomaly = null;

let events = [];
let activeSession = null;
let lastSequence = -1;
let activeFile = null;
let pendingSession = null;
let requestedRoute = null;
let latestPositions = [];
let followPositions = false;
const lastGpsPositions = new Map();
const geometryCache = new Map();
const vehicleStreams = new Map();
const retiredSessions = new Set();
let selectedVehicle = null;
let alertRevision = -1;
let alertSignature = null;
let alertedVehicles = new Set();


const startPoint = {
    latitude: 41.9028,
    longitude: 12.4964
};

// ==========================================
// MAPPA
// ==========================================

const map = L.map("map").setView(
    [startPoint.latitude, startPoint.longitude],
    13
);

L.tileLayer(
    "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    {
        maxZoom: 19,
        attribution: "&copy; OpenStreetMap contributors"
    }
).addTo(map);

// La geometria completa è indipendente dalle misure GPS e dalla media utenti.
const routeOutline = L.polyline([], {
    color: "#5f6771", weight: 6, opacity: 0.65, dashArray: "10, 9"
}).addTo(map);
const plannedRoute = L.polyline([], {
    color: "#d9dde3", weight: 3, opacity: 1, dashArray: "10, 9"
}).addTo(map);
const vehicleMarker = L.circleMarker([0, 0], {
    radius: 11, color: "#ffffff", weight: 2, fillColor: "#1689ef", fillOpacity: 1,
    className: "gps-marker"
}).bindTooltip("GPS", { permanent: true, direction: "top", className: "gps-label" });
const crowdMarker = L.circleMarker([0, 0], {
    radius: 7, color: "#ffffff", weight: 2, fillColor: "#aa4bea", fillOpacity: 1,
    className: "crowd-marker"
}).bindTooltip("Media utenti", { permanent: true, direction: "bottom", className: "crowd-label" });

function validPosition(position) {
    return position && Number.isFinite(position.latitude) && Number.isFinite(position.longitude)
        && Math.abs(position.latitude) <= 90 && Math.abs(position.longitude) <= 180;
}

function showMarker(marker, position, valid) {
    if (!valid) {
        if (map.hasLayer(marker)) map.removeLayer(marker);
        return;
    }
    marker.setLatLng([position.latitude, position.longitude]);
    if (!map.hasLayer(marker)) marker.addTo(map);
}

function setFollowPositions(enabled) {
    followPositions = enabled;
    $("positions-view-btn").setAttribute("aria-pressed", String(enabled));
    $("positions-view-btn").textContent = enabled ? "Interrompi inseguimento" : "Centra posizioni";
    if (enabled) framePositions();
}

function framePositions() {
    if (latestPositions.length) map.fitBounds(L.latLngBounds(latestPositions), {
        padding: [60, 60], maxZoom: 17, animate: false
    });
}

// Only user gestures stop following; automatic fitBounds must not disable it.
map.on("dragstart", () => setFollowPositions(false));
map.getContainer().addEventListener("wheel", () => setFollowPositions(false), { passive: true });
map.getContainer().addEventListener("dblclick", () => setFollowPositions(false));
map.getContainer().addEventListener("touchstart", event => {
    if (event.touches.length > 1) setFollowPositions(false);
}, { passive: true });
map.getContainer().addEventListener("keydown", event => {
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "+", "-", "="].includes(event.key)) setFollowPositions(false);
});
map.zoomControl.getContainer().addEventListener("click", () => setFollowPositions(false));

function frameRoute() {
    if (plannedRoute.getLatLngs().length) {
        map.fitBounds(plannedRoute.getBounds(), { padding: [28, 28], maxZoom: 15 });
    }
}

async function showRoute(file) {
    requestedRoute = file;
    plannedRoute.setLatLngs([]);
    routeOutline.setLatLngs([]);
    $("route-status").textContent = "Caricamento del percorso previsto…";
    try {
        if (!geometryCache.has(file)) {
            const response = await fetch("/api/geometry?file=" + encodeURIComponent(file));
            if (!response.ok) throw new Error("Geometria non disponibile");
            geometryCache.set(file, (await response.json()).coordinates);
        }
        if (requestedRoute !== file) return;
        const points = geometryCache.get(file);
        plannedRoute.setLatLngs(points);
        routeOutline.setLatLngs(points);
        if (followPositions) framePositions();
        else frameRoute();
        $("route-status").textContent = "Percorso previsto dalla geometria del file. I marcatori mostrano le ultime posizioni ricevute.";
    } catch (error) {
        if (requestedRoute === file) $("route-status").textContent = error.message;
    }
}

const uncertaintyLayer = L.layerGroup().addTo(map);
const networkLayer = L.layerGroup().addTo(map);
const occupiedLayer = L.layerGroup().addTo(map);
const fleetLayer = L.layerGroup().addTo(map);
const estimatedMarker = L.circleMarker([0,0], {
    radius: 9, color: '#ffbd45', fillColor: '#ffbd45', fillOpacity: 1
}).bindTooltip('Posizione stimata', { permanent: true, direction: 'left' });

function updateMap(data) {
    const loc = data.localization;
    uncertaintyLayer.clearLayers();
    showMarker(estimatedMarker, null, false);
    if (loc) {
        for (const shape of loc.areas) L.polygon(shape.coordinates, {
            color: '#ff5268', weight: 1, fillColor: '#ff5268', fillOpacity: .18
        }).bindTooltip('Area d’incertezza stimata').addTo(uncertaintyLayer);
        showMarker(estimatedMarker, loc.position, loc.state !== 'GNSS_RELIABLE' && validPosition(loc.position));
        $('localization-status').textContent = loc.message;
        $('localization-detail').textContent = `Fonti: ${loc.sources.join(', ') || 'nessuna'} · Ultimo GNSS affidabile: ${loc.last_reliable_age_s == null ? 'mai osservato' : displayNumber(loc.last_reliable_age_s) + ' s fa'} · ` +
            loc.areas.map(a => `${displayNumber(2*a.semi_along_m)} × ${displayNumber(2*a.semi_lateral_m)} m · ${displayNumber(a.heading_deg)}°`).join(' / ');
    }

    const currentGps = validPosition(data.position);
    const gpsReliable = currentGps && data.gnss.fix_status === true && (loc?.gnss_usable ?? false);
    const savedGps = lastGpsPositions.get(data.vehicle_id);
    const gpsPosition = currentGps ? data.position : savedGps?.position;
    const gpsVisible = validPosition(gpsPosition);
    const crowd = data.crowd_position;
    const crowdValid = validPosition(crowd) && crowd.num_phones > 0 && loc?.quality?.users?.usable;
    const gpsColor = gpsReliable ? "#1689ef" : "#c22e45";
    vehicleMarker.setStyle({ fillColor: gpsColor, dashArray: currentGps ? null : "4 3" });
    let gpsLabel = "GPS non disponibile";
    if (gpsVisible) {
        if (gpsReliable) gpsLabel = "GPS affidabile";
        else if (currentGps) gpsLabel = "GPS non affidabile";
        else {
            const age = (Date.parse(data.timestamp) - Date.parse(savedGps.timestamp)) / 1000;
            gpsLabel = "Ultima posizione GPS" + (Number.isFinite(age) && age >= 0 ? ` · ${Math.round(age)} s fa` : " · precedente");
        }
    }
    const tooltip = document.createElement("span");
    tooltip.textContent = gpsLabel;
    vehicleMarker.unbindTooltip().bindTooltip(tooltip, {
        permanent: true, direction: "top", className: gpsReliable ? "gps-label" : "gps-label gps-unreliable"
    });
    showMarker(vehicleMarker, gpsPosition, gpsVisible);
    showMarker(crowdMarker, crowd, crowdValid);
    if (gpsVisible) vehicleMarker.bringToFront();
    latestPositions = [];
    if (gpsVisible) latestPositions.push([gpsPosition.latitude, gpsPosition.longitude]);
    if (crowdValid) latestPositions.push([crowd.latitude, crowd.longitude]);
    if (validPosition(loc?.position)) latestPositions.push([loc.position.latitude, loc.position.longitude]);
    $("gps-map-status").textContent = gpsLabel;
    $("gps-legend-symbol").style.backgroundColor = gpsColor;
    if (followPositions) framePositions();
    $("crowd-map-status").textContent = crowdValid
        ? `Media utenti · ${displayNumber(crowd.num_phones)} telefoni` : "Media utenti non disponibile";
    $("crowd-latitude").textContent = crowdValid ? displayNumber(crowd.latitude, 6) : "--";
    $("crowd-longitude").textContent = crowdValid ? displayNumber(crowd.longitude, 6) : "--";
    $("crowd-phones").textContent = displayNumber(crowd?.num_phones);
}

function resetSession() {
    events = [];
    lastStatus = null;
    lastAnomaly = null;
    lastSequence = -1;
    latestPositions = [];
    showMarker(vehicleMarker, null, false);
    showMarker(crowdMarker, null, false);
    vehicleStreams.clear();
    lastGpsPositions.clear();
    selectedVehicle = null;
    alertRevision = -1;
    alertSignature = null;
    $('vehicle-select').replaceChildren();
    $('alerts-list').replaceChildren();
    $('alerts-status').textContent = 'In attesa del servizio allarmi';
    $('localization-status').textContent = 'In attesa della localizzazione';
    $('localization-detail').textContent = '';
    uncertaintyLayer.clearLayers();
    occupiedLayer.clearLayers();
    networkLayer.clearLayers();
    alertedVehicles.clear();
    fleetLayer.clearLayers();
    showMarker(estimatedMarker, null, false);
    $("event-log").replaceChildren();
}

async function loadCatalog() {
    const response = await fetch("/api/tracks");
    if (!response.ok) throw new Error("Impossibile caricare l’elenco dei CSV. Riprova con Analizza.");
    const catalog = await response.json();
    const select = $("track-select");
    select.replaceChildren();
    for (const folder of ["gnss_tracks", "dataset"]) {
        const group = document.createElement("optgroup");
        group.label = folder === "gnss_tracks" ? "Tracce GNSS" : "Dataset · split / rotta / scenario";
        for (const track of catalog.tracks.filter(track => track.id.startsWith(folder + "/"))) {
            const option = document.createElement("option");
            option.value = track.id;
            option.textContent = track.label;
            group.appendChild(option);
        }
        select.appendChild(group);
    }
    activeFile = catalog.active.file_id;
    select.value = activeFile;
    $("active-file").textContent = activeFile;
    $("selection-status").textContent = `${catalog.tracks.length} file disponibili. Analizza avvia il file dall’inizio per tutte le dashboard.`;
    await showRoute(activeFile);
    await restoreAlerts(catalog.active.session_id);
}

async function selectTrack() {
    $("analyze-btn").disabled = true;
    try {
        if (!$("track-select").value) await loadCatalog();
        const file = $("track-select").value;
        const response = await fetch("/api/select", {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ file_id: file })
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Avvio della traccia non riuscito");
        pendingSession = result.session_id;
        if (activeSession) retiredSessions.add(activeSession);
        activeSession = null;
        activeFile = file;
        resetSession();
        $("active-file").textContent = file;
        $("selection-status").textContent = "Nuova analisi avviata. In attesa dei campioni…";
        $("system-status").textContent = "IN ATTESA";
        $("system-status").className = "status attention";
        $("status-description").textContent = "In attesa della nuova traccia";
        for (const id of ["confidence", "satellites", "hdop", "signal", "speed", "heading", "latitude", "longitude", "fix", "crowd-latitude", "crowd-longitude", "crowd-phones"]) {
            $(id).textContent = "--";
        }
        $("confidence-bar").style.width = "0%";
        $("anomaly").textContent = "In attesa di analisi";
        $("anomaly").className = "anomaly-box";
        $("anomaly-message").textContent = "Raccolta dei primi campioni";
        $("last-update").textContent = "Ultimo aggiornamento: --";
        $("gps-map-status").textContent = "GPS in attesa";
        $("crowd-map-status").textContent = "Media utenti in attesa";
        showRoute(file);
        connectRabbitMQ();
    } catch (error) {
        $("selection-status").textContent = error.message;
    } finally {
        $("analyze-btn").disabled = false;
    }
}

// ==========================================
// STATO E TELEMETRIA
// ==========================================

const statusClasses = {
    NOMINAL: "nominal",
    ATTENTION: "attention",
    DEGRADED: "degraded",
    UNRELIABLE: "unreliable"
};

const statusLabels = {
    NOMINAL: "NOMINALE",
    ATTENTION: "ATTENZIONE",
    DEGRADED: "DEGRADATO",
    UNRELIABLE: "NON AFFIDABILE"
};

function displayNumber(value, decimals = 0) {
    if (!Number.isFinite(value)) return "--";
    return value.toFixed(decimals);
}

function updateDashboard(data) {
    if (!data ||
        !data.gnss || !data.analysis) {
        console.warn("JSON incompleto", data);
        return;
    }

    if (retiredSessions.has(data.session_id)) return;
    if (pendingSession && data.session_id !== pendingSession) return;
    // Campioni senza file sorgente non appartengono al replay selezionabile.
    if (!data.source_file) return;
    pendingSession = null;
    if (data.source_file !== activeFile) {
        activeFile = data.source_file;
        $("track-select").value = activeFile;
        $("active-file").textContent = activeFile;
        showRoute(activeFile);
    }
    $("selection-status").textContent = "Analisi in corso · campione " + (data.sequence + 1) + ". La selezione è condivisa tra le dashboard.";
    const session = data.session_id;
    if (session !== activeSession) {
        if (activeSession) retiredSessions.add(activeSession);
        resetSession();
        activeSession = session;
    }
    const previous = vehicleStreams.get(data.vehicle_id);
    if (previous && data.sequence <= previous.sequence) return;
    vehicleStreams.set(data.vehicle_id, data);
    // Store every accepted vehicle sample, including vehicles not currently selected.
    if (validPosition(data.position)) lastGpsPositions.set(data.vehicle_id, {
        position: { latitude: data.position.latitude, longitude: data.position.longitude },
        timestamp: data.timestamp
    });
    if (!previous) {
        const option = document.createElement('option');
        option.value = data.vehicle_id; option.textContent = data.vehicle_id;
        $('vehicle-select').appendChild(option);
    }
    selectedVehicle ||= data.vehicle_id;
    renderFleet();
    if (selectedVehicle !== data.vehicle_id) return;
    renderVehicle(data);
}

function renderVehicle(data) {
    const analysis = data.analysis;
    const position = data.position || {};
    const gnss = data.gnss;

    const status = statusClasses[analysis.status]
        ? analysis.status
        : "UNRELIABLE";

    const statusElement = $("system-status");

    statusElement.textContent = statusLabels[status];
    statusElement.className =
        "status " + statusClasses[status];

    $("status-description").textContent =
        analysis.message || "Nessuna descrizione";

    const score = analysis.confidence_score;

    $("confidence").textContent =
        Number.isFinite(score)
            ? displayNumber(score) + "/100"
            : "--";

    $("confidence-bar").style.width =
        Number.isFinite(score)
            ? Math.max(0, Math.min(100, score)) + "%"
            : "0%";

    $("confidence-bar").style.background =
        score >= 80 ? COLORS.green :
        score >= 50 ? COLORS.yellow : COLORS.danger;

    $("satellites").textContent =
        gnss.satellites_used ?? "--";

    $("hdop").textContent =
        displayNumber(gnss.hdop, 2);

    $("signal").textContent =
        Number.isFinite(gnss.signal_quality)
            ? displayNumber(gnss.signal_quality, 1) + " dB-Hz"
            : "--";

    $("speed").textContent =
        displayNumber(position.speed, 1) + " km/h";

    $("heading").textContent =
        displayNumber(position.heading, 0) + "°";

    $("latitude").textContent =
        displayNumber(position.latitude, 6);

    $("longitude").textContent =
        displayNumber(position.longitude, 6);

    $("fix").textContent =
        gnss.fix_status === true ? "VALIDO" :
        gnss.fix_status === false ? "ASSENTE" : "--";

    $("vehicle-id").textContent =
        data.vehicle_id || "VEICOLO";

    const anomaly = analysis.anomaly_types?.length
        ? analysis.anomaly_types.join(" + ").toUpperCase() : "NONE";

    $("anomaly").textContent =
        anomaly === "NONE"
            ? "Nessuna anomalia"
            : anomaly.replaceAll("_", " ");

    $("anomaly").className =
        anomaly === "NONE"
            ? "anomaly-box"
            : "anomaly-box warning";

    $("anomaly-message").textContent =
        analysis.message || "--";


    $("last-update").textContent =
        "Ultimo aggiornamento: " +
        new Date(data.timestamp).toLocaleString("it-IT");

    updateMap(data);

    if (status !== lastStatus ||
        anomaly !== lastAnomaly) {

        addEvent(
            data.timestamp,
            status,
            analysis.message || anomaly
        );

        lastStatus = status;
        lastAnomaly = anomaly;
    }
}

// ==========================================
// EVENT LOG
// ==========================================

function addEvent(timestamp, status, message) {
    const event = {
        timestamp,
        status,
        message
    };

    events.unshift(event);

    if (events.length > CONFIG.maxEvents) {
        events.pop();
    }

    const tbody = $("event-log");
    tbody.replaceChildren();

    events.forEach(item => {
        const tr = document.createElement("tr");

        const values = [
            new Date(item.timestamp).toLocaleTimeString("it-IT"),
            statusLabels[item.status] || item.status,
            item.message
        ];

        values.forEach(value => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
        });

        tbody.appendChild(tr);
    });
}

function exportEvents() {
    const escapeCSV = value =>
        '"' + String(value ?? "").replaceAll('"', '""') + '"';

    const rows = [
        ["timestamp", "status", "message"],
        ...events.map(e => [
            e.timestamp, e.status, e.message
        ])
    ];

    const csv = rows.map(row =>
        row.map(escapeCSV).join(",")
    ).join("\r\n");

    const blob = new Blob([csv], {
        type: "text/csv;charset=utf-8"
    });

    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");

    link.href = url;
    link.download = "mermec_events.csv";
    link.click();

    setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ==========================================
// RABBITMQ WEB STOMP
// ==========================================

function connectRabbitMQ() {
    if (stompClient) return;

    $("connection-status").textContent = "CONNESSIONE...";
    $("connection-dot").className = "dot";

    stompClient = new StompJs.Client({
        brokerURL: CONFIG.rabbitmqURL,

        connectHeaders: {
            login: CONFIG.username,
            passcode: CONFIG.password,
            host: "/"
        },

        reconnectDelay: 5000,
        heartbeatIncoming: 10000,
        heartbeatOutgoing: 10000,

        onConnect: () => {
            $("connection-status").textContent =
                "CONNESSO";

            $("connection-dot").className =
                "dot connected";

            stompClient.subscribe('/exchange/gnss.events/alerts.snapshot.v1', message => {
                try { updateAlerts(JSON.parse(message.body)); }
                catch (error) { console.error('Allarmi non validi', error); }
            }, { ack: 'auto', durable: 'false', 'auto-delete': 'true', exclusive: 'true' });
            restoreAlerts(activeSession || pendingSession);
            stompClient.subscribe(
                CONFIG.destination,
                message => {
                    try {
                        const data = JSON.parse(message.body);
                        updateDashboard(data);
                    } catch (error) {
                        console.error(
                            "Errore parsing JSON:", error
                        );
                    }
                },
                { ack: "auto", durable: "false", "auto-delete": "true", exclusive: "true" }
            );
        },

        onWebSocketClose: () => {
            $("connection-status").textContent =
                "DISCONNESSO";

            $("connection-dot").className =
                "dot disconnected";
        },

        onStompError: frame => {
            console.error(
                "Errore STOMP:",
                frame.headers.message
            );
        }
    });

    stompClient.activate();
}

async function disconnectRabbitMQ() {
    if (stompClient) {
        const client = stompClient;
        stompClient = null;
        await client.deactivate();
    }
}

async function restoreAlerts(session) {
    if (!session) return;
    try {
        const response = await fetch('/api/alerts?session=' + encodeURIComponent(session));
        if (!response.ok) return;
        const snapshot = await response.json();
        if (pendingSession && snapshot.session_id !== pendingSession) return;
        if (activeSession && snapshot.session_id !== activeSession) return;
        for (const data of snapshot.vehicles) updateDashboard(data);
        updateAlerts(snapshot);
    } catch (error) { console.warn('Ripristino allarmi:', error); }
}

function renderFleet() {
    fleetLayer.clearLayers();
    for (const data of vehicleStreams.values()) {
        const pos = data.localization?.position;
        if (validPosition(pos)) {
            const tooltip = document.createElement('span'); tooltip.textContent = data.vehicle_id + (alertedVehicles.has(data.vehicle_id) ? ' · destinatario allerta simulata' : '');
            L.circleMarker([pos.latitude,pos.longitude], {radius: 6, color: alertedVehicles.has(data.vehicle_id) ? '#ff8f30' : '#25c78a'})
                .bindTooltip(tooltip).addTo(fleetLayer);
        }
    }
}

function updateAlerts(snapshot) {
    if (snapshot.session_id !== activeSession || snapshot.revision <= alertRevision) return;
    alertRevision = snapshot.revision;
    networkLayer.clearLayers();
    for (const zone of snapshot.network || []) {
        const label = document.createElement('span'); label.textContent = zone.id + (zone.level ? ' · cavalcavia, livello separato' : ' · binario');
        L.polyline(zone.coordinates, {color: zone.level === 0 ? '#747b85' : '#999', weight: 4,
            dashArray: zone.level === 0 ? null : '6,6'}).bindTooltip(label).addTo(networkLayer);
    }
    alertedVehicles = new Set(snapshot.alerts.filter(a => a.risk_state !== 'RESOLVED').flatMap(a => a.recipients));
    renderFleet();
    occupiedLayer.clearLayers();
    for (const zone of snapshot.zones) {
        const label = document.createElement('span'); label.textContent = zone.id + ' · possibile occupazione';
        L.polyline(zone.coordinates, {color: '#ff5268', weight: 7, opacity: .7})
            .bindTooltip(label).addTo(occupiedLayer);
    }
    const signature = JSON.stringify([snapshot.alerts, snapshot.recipients_incomplete]);
    if (signature === alertSignature) return;
    alertSignature = signature;
    $('alerts-status').textContent = snapshot.recipients_incomplete
        ? 'Selezione destinatari incompleta · verificare con la sala di controllo'
        : 'Allertamento dei mezzi in avvicinamento e della sala di controllo · Notifica simulata';
    const list = $('alerts-list'); list.replaceChildren();
    for (const alert of snapshot.alerts) {
        const row = document.createElement('article'); row.className = 'alert-entry';
        const title = document.createElement('strong');
        title.textContent = `${alert.zone_id} · ${alert.risk_state === 'RESOLVED' ? 'Risolto: zona esclusa' : 'Possibile occupazione'}`;
        const detail = document.createElement('p');
        detail.textContent = (alert.risk_state === 'RESOLVED' ? '' : 'Allerta mantenuta: occupazione non ancora esclusa. ') +
            Object.entries(alert.notifications).map(([id,n]) => `${id === 'CONTROL_ROOM' ? 'Sala di controllo' : id}: ${n.state === 'ACKNOWLEDGED' ? 'preso in carico' : n.confirmed ? 'avviso simulato ricevuto' : n.state === 'SENDING' ? 'avviso simulato in corso' : 'avviso simulato in attesa'}`).join(' · ');
        row.append(title,detail);
        const control = alert.notifications.CONTROL_ROOM;
        if (control?.confirmed && control.state !== 'ACKNOWLEDGED' && alert.risk_state !== 'RESOLVED') {
            const button = document.createElement('button'); button.textContent = 'Prendi in carico · sala di controllo';
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    const response = await fetch('/api/alerts/ack', {method: 'POST', headers: {'Content-Type':'application/json'},
                        body: JSON.stringify({session_id: snapshot.session_id, alert_id: alert.id, recipient:'CONTROL_ROOM'})});
                    if (!response.ok) throw new Error('Presa in carico non confermata');
                    updateAlerts(await response.json());
                } catch (error) { $('alerts-status').textContent = error.message; button.disabled = false; }
            });
            row.append(button);
        }
        list.append(row);
    }
}

$('vehicle-select').addEventListener('change', () => {
    selectedVehicle = $('vehicle-select').value;
    const data = vehicleStreams.get(selectedVehicle);
    if (data) renderVehicle(data);
});

// ==========================================
// CONTROLLI
// ==========================================

$("start-btn").addEventListener("click", connectRabbitMQ);
$("stop-btn").addEventListener("click", async () => {
    await disconnectRabbitMQ();
    $("connection-status").textContent = "IN PAUSA";
    $("connection-dot").className = "dot disconnected";
});
$("export-btn").addEventListener("click", exportEvents);
$("analyze-btn").addEventListener("click", selectTrack);
$("route-view-btn").addEventListener("click", () => {
    setFollowPositions(false);
    frameRoute();
});
$("positions-view-btn").addEventListener("click", () => {
    setFollowPositions(!followPositions);
});
loadCatalog().catch(error => { $("selection-status").textContent = error.message; }).finally(connectRabbitMQ);

// Keep Leaflet aligned with the responsive dashboard grid.
new ResizeObserver(() => map.invalidateSize()).observe($("map"));
