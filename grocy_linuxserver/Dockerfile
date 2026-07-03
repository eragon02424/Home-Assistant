FROM lscr.io/linuxserver/grocy:latest

# nginx-patch: schreibt default.conf mit allen GROCY_* Werten als fastcgi_param
# (custom-cont-init.d läuft nach ls.io-init, wenn default.conf bereits existiert)
COPY nginx-patch.sh /custom-cont-init.d/99-grocy-nginx
RUN chmod +x /custom-cont-init.d/99-grocy-nginx
