FROM lscr.io/linuxserver/grocy:latest

# env-init: setzt Grocy Umgebungsvariablen (läuft früh, vor nginx)
COPY env-init.sh /etc/cont-init.d/99-grocy-env
# nginx-patch: patcht default.conf nach nginx-Start (custom-cont-init läuft nach ls.io-init)
COPY nginx-patch.sh /custom-cont-init.d/99-grocy-nginx
RUN chmod +x /etc/cont-init.d/99-grocy-env /custom-cont-init.d/99-grocy-nginx
