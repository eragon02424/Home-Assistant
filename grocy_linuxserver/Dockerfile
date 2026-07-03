FROM lscr.io/linuxserver/grocy:latest

# run.sh kopiert ingress.conf nach /config erst wenn s6 /config initialisiert hat
COPY run.sh /etc/cont-init.d/99-grocy-ha
COPY ingress.conf /ingress.conf.tpl
RUN chmod +x /etc/cont-init.d/99-grocy-ha
