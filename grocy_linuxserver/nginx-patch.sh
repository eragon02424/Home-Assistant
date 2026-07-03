#!/bin/sh
# Patcht nginx default.conf für Ingress-Subpfad
# Läuft nach ls.io-init (custom-cont-init.d)
CONF="/config/nginx/site-confs/default.conf"

if [ ! -f "$CONF" ]; then
    echo "[grocy-ha] FEHLER: ${CONF} nicht gefunden"
    exit 0
fi

cat > "$CONF" << 'EOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    server_name _;

    set $root /app/www/public;
    root $root;
    index index.php;

    client_max_body_size 0;

    location /57f327aa_grocy_linuxserver {
        try_files $uri $uri/ /57f327aa_grocy_linuxserver/index.php$is_args$args;
    }

    location ~ ^/57f327aa_grocy_linuxserver/(.+\.php)(.*)$ {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME $document_root/$1;
        fastcgi_param PATH_INFO $2;
        fastcgi_param GROCY_AUTH_CLASS "Grocy\Middleware\ReverseProxyAuthMiddleware";
        fastcgi_param HTTP_REMOTE_USER admin;
        include /etc/nginx/fastcgi_params;
    }

    location ~ /\.ht {
        deny all;
    }
}
EOF

nginx -s reload
echo "[grocy-ha] nginx gepatcht und neu geladen"
