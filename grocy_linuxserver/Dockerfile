FROM lscr.io/linuxserver/grocy:latest

# Copy custom nginx ingress config
COPY ingress.conf /config/nginx/site-confs/ingress.conf

# Copy run script that sets env vars
COPY run.sh /etc/cont-init.d/99-grocy-ha
RUN chmod +x /etc/cont-init.d/99-grocy-ha
