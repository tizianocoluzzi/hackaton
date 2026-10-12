#!/bin/sh
set -eu
# Credenziali dimostrative semplici: sostituzione sicura nel JSON del broker.
for value in "$ADMIN_USER" "$ADMIN_PASSWORD" "$SIMULATION_USER" "$SIMULATION_PASSWORD" \
             "$ALERTS_USER" "$ALERTS_PASSWORD" "$CLASSIFICATION_USER" "$CLASSIFICATION_PASSWORD" "$FRONTEND_USER" "$FRONTEND_PASSWORD"; do
    case "$value" in ''|*[!a-zA-Z0-9_-]*) echo 'Credenziali mockup: usare solo lettere, numeri, _ e -' >&2; exit 1;; esac
done
sed -e "s/__ADMIN_USER__/$ADMIN_USER/g" -e "s/__ADMIN_PASSWORD__/$ADMIN_PASSWORD/g" \
    -e "s/__SIMULATION_USER__/$SIMULATION_USER/g" -e "s/__SIMULATION_PASSWORD__/$SIMULATION_PASSWORD/g" \
    -e "s/__CLASSIFICATION_USER__/$CLASSIFICATION_USER/g" -e "s/__CLASSIFICATION_PASSWORD__/$CLASSIFICATION_PASSWORD/g" \
    -e "s/__ALERTS_USER__/$ALERTS_USER/g" -e "s/__ALERTS_PASSWORD__/$ALERTS_PASSWORD/g" \
    -e "s/__FRONTEND_USER__/$FRONTEND_USER/g" -e "s/__FRONTEND_PASSWORD__/$FRONTEND_PASSWORD/g" \
    /etc/rabbitmq/definitions.json > /tmp/definitions.json
exec docker-entrypoint.sh rabbitmq-server
