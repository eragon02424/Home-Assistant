#!/bin/sh
set -e

# Eigene IP im hassio-Netzwerk ermitteln
MY_IP=$(hostname -i | awk '{print $1}')
echo "Barcode Buddy IP: $MY_IP"

# Grocy nginx-Config patchen: eigene IP in die allow-Liste eintragen
GROCY_CONF=$(docker inspect --format='{{range .Mounts}}{{if eq .Destination "/etc/nginx/servers"}}{{.Source}}{{end}}{{end}}' addon_a0d7b954_grocy 2>/dev/null || true)

# Direkter Ansatz: Grocy nginx-Config im laufenden Container patchen
docker exec addon_a0d7b954_grocy sh -c "
  sed -i 's|allow   172.30.32.2;|allow   172.30.32.2;\n    allow   ${MY_IP};|' /etc/nginx/servers/ingress.conf &&
  nginx -s reload
" 2>/dev/null || echo "Grocy nginx patch fehlgeschlagen - fahre trotzdem fort"

# Barcode Buddy starten
exec /app/supervisor
