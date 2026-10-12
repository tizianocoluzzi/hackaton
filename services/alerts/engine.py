"""Conservative occupancy tracking; notification receipts never resolve risk."""
import hashlib
import math
from shared.contracts import parse_timestamp
from shared.uncertainty import intersects


class AlertEngine:
    def __init__(self, network, horizon_s=300, clearance_s=10, margin_m=10, freshness_s=15):
        if not all(math.isfinite(v) and v >= 0 for v in (horizon_s, clearance_s, margin_m, freshness_s)):
            raise ValueError('Parametri allarmi non validi')
        self.network = network
        self.networks = {network['id']: network}
        self.horizon = horizon_s
        self.clearance = clearance_s
        self.margin = margin_m
        self.freshness = freshness_s
        ids = [s['id'] for s in network['segments']]
        if len(ids) != len(set(ids)):
            raise ValueError('Identificativi infrastruttura duplicati')
        for segment in network['segments']:
            if len(segment['coordinates']) < 2 or not set(segment['connections']) <= set(ids):
                raise ValueError('Geometria o collegamenti non validi')
        self.state = dict(sessions={})

    def register_network(self, network):
        AlertEngine(network)  # Apply the same geometry and connection validation.
        if network['id'] in self.networks:
            raise ValueError('Identificativo rete duplicato')
        self.networks[network['id']] = network

    def session_network(self, session):
        return self.networks.get(session.get('network_id'), self.network)

    def session(self, sid):
        return self.state['sessions'].setdefault(sid, dict(vehicles={}, occupations={}, alerts={}, revision=0, clock=None))

    def receipt(self, sid, alert_id, recipient, acknowledged=False):
        notification = self.session(sid)['alerts'][alert_id]['notifications'][recipient]
        if notification['attempts'] == 0:
            raise ValueError('Notifica mai inviata')
        notification['state'] = 'ACKNOWLEDGED' if acknowledged else 'DELIVERED'
        notification['confirmed'] = True

    def update(self, message):
        sid, vid = message['session_id'], message['vehicle_id']
        session = self.session(sid)
        network_id = message.get('network_id')
        if 'network_id' in session and session['network_id'] != network_id:
            raise ValueError('Rete cambiata nella stessa sessione')
        session['network_id'] = network_id
        network = self.session_network(session)
        old = session['vehicles'].get(vid)
        if old and message['sequence'] <= old['sequence']:
            return self.snapshot(sid)
        now = parse_timestamp(message['timestamp']).timestamp()
        if old and now - parse_timestamp(old['timestamp']).timestamp() > self.freshness:
            for occupation in session['occupations'].values():
                if occupation['vehicle_id'] == vid:
                    occupation['clear_since'] = None
        if old and now <= parse_timestamp(old['timestamp']).timestamp():
            raise ValueError('Timestamp non crescente')
        session['clock'] = max(now, session['clock'] or now)
        session['vehicles'][vid] = message
        session['revision'] += 1
        loc = message.get('localization', {})
        areas = loc.get('areas', [])
        traffic = message.get('traffic', {})
        level = traffic.get('level')
        known_network = message.get('network_id') == network['id']
        nearby = []
        for segment in network['segments']:
            key = vid + ':' + segment['id']
            occ = session['occupations'].get(key)
            compatible = level is None or level == segment['level']
            hit = known_network and compatible and any(intersects(a, segment['coordinates'], segment.get('width_m', 2)) for a in areas)
            if not hit and known_network and compatible and any(intersects(a, segment['coordinates'], 30) for a in areas):
                nearby.append(segment['id'])
            if hit:
                if occ is None:
                    occ = dict(vehicle_id=vid, zone_id=segment['id'], since=now, clear_since=None,
                               active=True, evidence='possible_occupancy')
                    session['occupations'][key] = occ
                occ.update(active=True, clear_since=None, last_possible=now, uncertain=not loc.get('clearance_evidence', False))
            elif occ and occ['active']:
                separated = known_network and loc.get('clearance_evidence') and areas and not any(
                    intersects(a, segment['coordinates'], self.margin + segment.get('width_m', 2)) for a in areas)
                if separated:
                    if occ['clear_since'] is None:
                        occ['clear_since'] = now
                    if now - occ['clear_since'] >= self.clearance:
                        occ['active'] = False
                else:
                    occ['clear_since'] = None
        message['nearby_zones'] = nearby
        self.refresh(sid)
        return self.snapshot(sid)

    def refresh(self, sid):
        session = self.session(sid)
        network = self.session_network(session)
        now = session['clock']
        incomplete = False
        for vehicle in session['vehicles'].values():
            t = vehicle.get('traffic', {})
            age = now - parse_timestamp(vehicle['timestamp']).timestamp()
            if (age > self.freshness or not t.get('route') or t.get('direction') not in (-1, 1)
                    or not isinstance(t.get('transits'), list) or t.get('level') is None
                    or vehicle.get('network_id') != network['id']
                    or not vehicle.get('localization', {}).get('position')):
                incomplete = True
        expected = network.get('expected_vehicles', [])
        incomplete |= not set(expected) <= set(session['vehicles'])
        session['recipients_incomplete'] = incomplete
        for key, occ in session['occupations'].items():
            aid = hashlib.sha256((sid + ':' + key).encode()).hexdigest()[:24]
            alert = session['alerts'].get(aid)
            if not occ['active']:
                if alert:
                    alert.update(risk_state='RESOLVED', resolved_at=now)
                continue
            recipients = {'CONTROL_ROOM'}
            zone = next(s for s in network['segments'] if s['id'] == occ['zone_id'])
            for vid, vehicle in session['vehicles'].items():
                if vid == occ['vehicle_id']:
                    continue
                t = vehicle.get('traffic', {})
                if t.get('level') is not None and t['level'] != zone['level']:
                    continue
                present = any(o['active'] and o['vehicle_id'] == vid and o['zone_id'] == zone['id'] for o in session['occupations'].values())
                transits = t.get('transits', [])
                # Occupancy end is unknown until excluded: [since, infinity).
                approaching = any(p.get('zone_id') == zone['id'] and
                                  p.get('enter', math.inf) <= now + self.horizon and
                                  p.get('exit', -math.inf) >= max(now, occ['since']) for p in transits)
                if present or approaching:
                    recipients.add(vid)
            if alert is None and not occ.get('uncertain', True) and len(recipients) == 1:
                continue
            if alert is None:
                alert = dict(id=aid, vehicle_id=occ['vehicle_id'], zone_id=occ['zone_id'],
                             risk_state='DETECTED', notifications={}, detected_at=now, simulated=True)
                session['alerts'][aid] = alert
            if alert['risk_state'] == 'RESOLVED':
                alert['notifications'] = {}
            alert['risk_state'] = 'DETECTED'
            alert['resolved_at'] = None
            alert['recipients'] = sorted(recipients)
            alert['recipients_incomplete'] = incomplete
            for recipient in recipients:
                alert['notifications'].setdefault(recipient, dict(state='DETECTED', attempts=0,
                        confirmed=False, last_attempt=None, simulated=True))

    def dispatch(self, sid, receiver, retry_s=5):
        """Receiver explicitly returns a receipt; failed sends retain SENDING and retry."""
        session = self.session(sid)
        now = session['clock']
        for alert in session['alerts'].values():
            if alert['risk_state'] == 'RESOLVED':
                continue
            for recipient in alert['recipients']:
                n = alert['notifications'][recipient]
                if n['confirmed'] or (n['last_attempt'] is not None and now - n['last_attempt'] < retry_s):
                    continue
                n.update(state='SENDING', attempts=n['attempts'] + 1, last_attempt=now)
                receipt = receiver(alert['id'], recipient, n['attempts'])
                if receipt:
                    self.receipt(sid, alert['id'], recipient)
        session['revision'] += 1

    def snapshot(self, sid):
        s = self.session(sid)
        network = self.session_network(s)
        occupied = [o for o in s['occupations'].values() if o['active']]
        zones = {o['zone_id'] for o in occupied}
        return dict(schema_version='1', session_id=sid, revision=s['revision'], timestamp=s['clock'],
                    network_id=network['id'], network=network['segments'] if any(v.get('network_id') == network['id'] for v in s['vehicles'].values()) else [], occupations=occupied,
                    zones=[z for z in network['segments'] if z['id'] in zones],
                    alerts=list(s['alerts'].values()), recipients_incomplete=s.get('recipients_incomplete', True),
                    vehicles=list(s['vehicles'].values()), simulated=True,
                    control_room_required=s.get('recipients_incomplete', True) or any(
                        v.get('localization', {}).get('control_room_required') for v in s['vehicles'].values()))
