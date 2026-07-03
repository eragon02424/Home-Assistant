#!/bin/sh
# Patcht nginx default.conf: Ingress-Subpfad, Auth, Feature-Flags als fastcgi_param.
#
# WICHTIG 1: /etc/environment wird von PHP-FPM (s6-overlay) NICHT eingelesen -
# alle GROCY_* Werte muessen als fastcgi_param uebergeben werden (bestaetigt via
# /app/www/helpers/extensions.php: Setting() liest getenv('GROCY_' . $name)).
#
# WICHTIG 2: GROCY_BASE_PATH darf NICHT gesetzt werden (bricht Slim-Routing, da
# der Supervisor Ingress-Requests OHNE Praefix weiterleitet).
#
# WICHTIG 3: Grocys eigener Root-Controller (SystemController::Root) macht bei
# GET / IMMER einen HTTP 302 auf einen absoluten Pfad (UrlManager::ConstructUrl
# baut zwingend BasePath+Pfad zusammen, nie relativ - siehe UrlManager.php).
# Dieser absolute Redirect wird vom HA-Ingress-Frontend als eigene interne Route
# fehlinterpretiert und laedt die normale HA-Oberflaeche statt Grocy nachzuladen.
#
# Fix: location = / ruft PHP-FPM DIREKT auf (nicht ueber try_files/rewrite) und
# setzt REQUEST_URI hart auf die Entry-Page-Route. Ein nginx "rewrite ... last"
# reicht NICHT, weil $request_uri (das fastcgi_params per Default fuer
# REQUEST_URI nutzt) bei internen Rewrites unveraendert bleibt - $uri aendert
# sich zwar, wird aber durch das nachfolgende try_files$-Fallback auf /index.php
# wieder ueberschrieben. Deshalb REQUEST_URI hier fest auf die Zielroute setzen.
# Mapping laut SystemController::GetEntryPageRelative().
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
    TWEAK_CHORES_ASSIGN=$(jq -r '.tweaks.chores_assignment // true' "$OPTIONS")
    TWEAK_MULTI_SHOP=$(jq -r '.tweaks.multiple_shopping_lists // true' "$OPTIONS")
    TWEAK_BBD=$(jq -r '.tweaks.stock_best_before_date_tracking // true' "$OPTIONS")
    TWEAK_LOCATION=$(jq -r '.tweaks.stock_location_tracking // true' "$OPTIONS")
    TWEAK_PRICE=$(jq -r '.tweaks.stock_price_tracking // true' "$OPTIONS")
    TWEAK_FREEZE=$(jq -r '.tweaks.stock_product_freezing // true' "$OPTIONS")
    TWEAK_OPENED=$(jq -r '.tweaks.stock_product_opened_tracking // true' "$OPTIONS")
else
    CULTURE="de"; CURRENCY="EUR"; ENTRY_PAGE="stock"; GROCYCODE_TYPE="2D"
    FEAT_BATTERIES="false"; FEAT_CALENDAR="true"; FEAT_CHORES="true"
    FEAT_EQUIPMENT="false"; FEAT_RECIPES="true"; FEAT_SHOPPINGLIST="true"
    FEAT_STOCK="true"; FEAT_TASKS="false"
    TWEAK_CHORES_ASSIGN="true"; TWEAK_MULTI_SHOP="true"; TWEAK_BBD="true"
    TWEAK_LOCATION="true"; TWEAK_PRICE="true"; TWEAK_FREEZE="true"; TWEAK_OPENED="true"
fi

# Mapping entry_page -> interner Grocy-Pfad (siehe SystemController::GetEntryPageRelative)
case "$ENTRY_PAGE" in
    stock) ENTRY_ROUTE="/stockoverview" ;;
    shoppinglist) ENTRY_ROUTE="/shoppinglist" ;;
    recipes) ENTRY_ROUTE="/recipes" ;;
    chores) ENTRY_ROUTE="/choresoverview" ;;
    tasks) ENTRY_ROUTE="/tasks" ;;
    batteries) ENTRY_ROUTE="/batteriesoverview" ;;
    equipment) ENTRY_ROUTE="/equipment" ;;
    calendar) ENTRY_ROUTE="/calendar" ;;
    *) ENTRY_ROUTE="/stockoverview" ;;
esac

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

    location = / {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME \$document_root/index.php;
        fastcgi_param HTTP_REMOTE_USER admin;
        fastcgi_param GROCY_AUTH_CLASS 'Grocy\\Middleware\\ReverseProxyAuthMiddleware';
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
        fastcgi_param GROCY_FEATURE_FLAG_CHORES_ASSIGNMENTS '${TWEAK_CHORES_ASSIGN}';
        fastcgi_param GROCY_FEATURE_FLAG_SHOPPINGLIST_MULTIPLE_LISTS '${TWEAK_MULTI_SHOP}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_BEST_BEFORE_DATE_TRACKING '${TWEAK_BBD}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_LOCATION_TRACKING '${TWEAK_LOCATION}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_PRICE_TRACKING '${TWEAK_PRICE}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_PRODUCT_FREEZING '${TWEAK_FREEZE}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_PRODUCT_OPENED_TRACKING '${TWEAK_OPENED}';
        include /etc/nginx/fastcgi_params;
        fastcgi_param REQUEST_URI ${ENTRY_ROUTE};
    }

    location / {
        try_files \$uri /index.php\$is_args\$args;
    }

    location ~ \.php\$ {
        fastcgi_pass 127.0.0.1:9000;
        fastcgi_index index.php;
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param HTTP_REMOTE_USER admin;
        fastcgi_param GROCY_AUTH_CLASS 'Grocy\\Middleware\\ReverseProxyAuthMiddleware';
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
        fastcgi_param GROCY_FEATURE_FLAG_CHORES_ASSIGNMENTS '${TWEAK_CHORES_ASSIGN}';
        fastcgi_param GROCY_FEATURE_FLAG_SHOPPINGLIST_MULTIPLE_LISTS '${TWEAK_MULTI_SHOP}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_BEST_BEFORE_DATE_TRACKING '${TWEAK_BBD}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_LOCATION_TRACKING '${TWEAK_LOCATION}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_PRICE_TRACKING '${TWEAK_PRICE}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_PRODUCT_FREEZING '${TWEAK_FREEZE}';
        fastcgi_param GROCY_FEATURE_FLAG_STOCK_PRODUCT_OPENED_TRACKING '${TWEAK_OPENED}';
        include /etc/nginx/fastcgi_params;
    }

    location ~ /\.ht {
        deny all;
    }
}
EOF

nginx -s reload 2>/dev/null || true
echo "[grocy-ha] nginx gepatcht: culture=${CULTURE} entry_route=${ENTRY_ROUTE}"
