#!/bin/sh
set -eu
export WS_URL_JSON="$(printf '%s' "$WEB_STOMP_URL" | jq -Rs .)"
export WS_USER_JSON="$(printf '%s' "$FRONTEND_USER" | jq -Rs .)"
export WS_PASSWORD_JSON="$(printf '%s' "$FRONTEND_PASSWORD" | jq -Rs .)"
envsubst '${WS_URL_JSON} ${WS_USER_JSON} ${WS_PASSWORD_JSON}' \
    < /etc/dashboard/config.js.template > /usr/share/nginx/html/config.js
