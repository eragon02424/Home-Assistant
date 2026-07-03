#!/bin/sh
# Patcht nginx default.conf für Ingress-Subpfad
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

    root /app/www/public;
    index index.php;

    client_max_body_size 0;

    # Subpfad-Strip: /57f327aa_grocy_linuxserver/... -> /...
    location /57f327aa_grocy_linuxserver/ {
        alias /app/www/public/;
        try_files $uri $uri/ @grocy_php;
    }

    location @grocy_php {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME /app/www/public/index.php;
        fastcgi_param GROCY_AUTH_CLASS "Grocy\Middleware\ReverseProxyAuthMiddleware";
        fastcgi_param HTTP_REMOTE_USER admin;
        include /etc/nginx/fastcgi_params;
    }

    location ~ ^/57f327aa_grocy_linuxserver/(.+\.php)$ {
        alias /app/www/public/;
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_param SCRIPT_FILENAME /app/www/public/$1;
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
