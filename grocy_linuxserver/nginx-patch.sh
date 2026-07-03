#!/bin/sh
# Patcht nginx default.conf: Ingress-Subpfad, Auth, Feature-Flags als fastcgi_param.
# /etc/environment wird von PHP-FPM (s6-overlay) NICHT eingelesen - deshalb muessen
# alle GROCY_* Werte als fastcgi_param uebergeben werden, nicht als Environment-Variable.
OPTIONS="/data/options.json"
INGRESS_PATH="/57f327aa_grocy_linuxserver"
CONF="/config/nginx/site-confs/default.conf"

if [ -f "$OPTIONS" ]; then
    CULTURE=$(jq -r '.culture // "de"' "$OPTIONS")
    CURRENCY=$(jq -r '.currency // "EUR"' "$OPTIONS")
    ENTRY_PAGE=$(jq -r '.entry_page // "stock"' "$OPTIONS")
    GROCYCODE_TYPE=$(jq -r '.grocycode_type // "2D"' "$OPTIONS")
    FEAT_BATTERIES=$(jq -r '.features.batteries // false' "$OPTIONS")
    FEAT_CALENDAR=$(jq -r '.features.calendar // true' "$OPTIONS")
    FEAT_CHORES=$(jq -r '.features.chores // true' "$OPTIONS")
    FEAT_EQUIPMENT=$(jq -r '.features.equipment // false' "$OPTIONS")
    FEAT_RECIPES=$(jq -r '.features.recipes // true' "$OPTIONS")
    FEAT_SHOPPINGLIST=$(jq -r '.features.shoppinglist // true' "$OPTIONS")
    FEAT_STOCK=$(jq -r '.features.stock // true' "$OPTIONS")
    FEAT_TASKS=$(jq -r '.features.tasks // false' "$OPTIONS")
else
    CULTURE="de"; CURRENCY="EUR"; ENTRY_PAGE="stock"; GROCYCODE_TYPE="2D"
    FEAT_BATTERIES="false"; FEAT_CALENDAR="true"; FEAT_CHORES="true"
    FEAT_EQUIPMENT="false"; FEAT_RECIPES="true"; FEAT_SHOPPINGLIST="true"
    FEAT_STOCK="true"; FEAT_TASKS="false"
fi

if [ ! -f "$CONF" ]; then
    echo "[grocy-ha] FEHLER: ${CONF} nicht gefunden"
    exit 0
fi

cat > "$CONF" << EOF
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    root /app/www/public;
    index index.php;
    client_max_body_size 0;

    location / {
        try_files \$uri /index.php\$is_args\$args;
    }

    location ~ \.php\$ {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param HTTP_REMOTE_USER admin;
        fastcgi_param GROCY_AUTH_CLASS 'Grocy\\Middleware\\ReverseProxyAuthMiddleware';
        fastcgi_param GROCY_BASE_PATH '${INGRESS_PATH}';
        fastcgi_param GROCY_BASE_URL '${INGRESS_PATH}';
        fastcgi_param GROCY_CULTURE '${CULTURE}';
        fastcgi_param GROCY_CURRENCY '${CURRENCY}';
        fastcgi_param GROCY_ENTRY_PAGE '${ENTRY_PAGE}';
        fastcgi_param GROCY_GROCYCODE_TYPE '${GROCYCODE_TYPE}';
        fastcgi_param GROCY_FEATURE_FLAG_BATTERIES '${FEAT_BATTERIES}';
        fastcgi_param GROCY_FEATURE_FLAG_CALENDAR '${FEAT_CALENDAR}';
        fastcgi_param GROCY_FEATURE_FLAG_CHORES '${FEAT_CHORES}';
        fastcgi_param GROCY_FEATURE_FLAG_EQUIPMENT '${FEAT_EQUIPMENT}';
        fastcgi_param GROCY_FEATURE_FLAG_RECIPES '${FEAT_RECIPES}';
        fastcgi_param GROCY_FEATURE_FLAG_SHOPPINGLIST '${FEAT_SHOPPINGLIST}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK '${FEAT_STOCK}';
        fastcgi_param GROCY_FEATURE_FLAG_TASKS '${FEAT_TASKS}';
        include /etc/nginx/fastcgi_params;
    }

    location ~ /\.ht {
        deny all;
    }
}
EOF

nginx -s reload 2>/dev/null || true
echo "[grocy-ha] nginx gepatcht: culture=${CULTURE} batteries=${FEAT_BATTERIES}"
