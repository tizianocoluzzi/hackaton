FROM nginx:1.28-alpine
RUN apk add --no-cache jq
COPY services/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY services/frontend/index.html /usr/share/nginx/html/index.html
COPY services/frontend/css /usr/share/nginx/html/css
COPY services/frontend/js /usr/share/nginx/html/js
COPY services/frontend/config.js.template /etc/dashboard/config.js.template
COPY --chmod=755 services/frontend/40-config.sh /docker-entrypoint.d/40-config.sh
