#!/bin/sh
OPTIONS="/data/options.json"

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
    TWEAK_COUNT_OPENED=$(jq -r '.tweaks.stock_count_opened_products_against_minimum_stock_amount // true' "$OPTIONS")
else
    CULTURE="de"
    CURRENCY="EUR"
    ENTRY_PAGE="stock"
    GROCYCODE_TYPE="2D"
    FEAT_BATTERIES="false"
    FEAT_CALENDAR="true"
    FEAT_CHORES="true"
    FEAT_EQUIPMENT="false"
    FEAT_RECIPES="true"
    FEAT_SHOPPINGLIST="true"
    FEAT_STOCK="true"
    FEAT_TASKS="false"
    TWEAK_CHORES_ASSIGN="true"
    TWEAK_COUNT_OPENED="true"
fi

# Grocy Environment Variablen setzen
{
    echo "GROCY_CULTURE=${CULTURE}"
    echo "GROCY_CURRENCY=${CURRENCY}"
    echo "GROCY_ENTRY_PAGE=${ENTRY_PAGE}"
    echo "GROCY_GROCYCODE_TYPE=${GROCYCODE_TYPE}"
    echo "GROCY_AUTH_CLASS=Grocy\\Middleware\\ReverseProxyAuthMiddleware"
    echo "GROCY_FEATURE_FLAG_BATTERIES=${FEAT_BATTERIES}"
    echo "GROCY_FEATURE_FLAG_CALENDAR=${FEAT_CALENDAR}"
    echo "GROCY_FEATURE_FLAG_CHORES=${FEAT_CHORES}"
    echo "GROCY_FEATURE_FLAG_EQUIPMENT=${FEAT_EQUIPMENT}"
    echo "GROCY_FEATURE_FLAG_RECIPES=${FEAT_RECIPES}"
    echo "GROCY_FEATURE_FLAG_SHOPPINGLIST=${FEAT_SHOPPINGLIST}"
    echo "GROCY_FEATURE_FLAG_STOCK=${FEAT_STOCK}"
    echo "GROCY_FEATURE_FLAG_TASKS=${FEAT_TASKS}"
    echo "GROCY_FEATURE_SETTING_CHORES_ASSIGNMENTS=${TWEAK_CHORES_ASSIGN}"
    echo "GROCY_FEATURE_SETTING_STOCK_COUNT_OPENED_PRODUCTS_AGAINST_MINIMUM_STOCK_AMOUNT=${TWEAK_COUNT_OPENED}"
} >> /etc/environment

# default.conf patchen: GROCY_AUTH_CLASS und REMOTE_USER zu PHP-FPM hinzufügen
# Warten bis /config/nginx/site-confs/default.conf vom s6-init angelegt wurde
CONF="/config/nginx/site-confs/default.conf"
TRIES=0
while [ ! -f "$CONF" ] && [ $TRIES -lt 10 ]; do
    sleep 1
    TRIES=$((TRIES+1))
done

if [ -f "$CONF" ]; then
    # Nur patchen wenn noch nicht gepatcht
    if ! grep -q "GROCY_AUTH_CLASS" "$CONF"; then
        sed -i 's|include /etc/nginx/fastcgi_params;|fastcgi_param GROCY_AUTH_CLASS "Grocy\\Middleware\\ReverseProxyAuthMiddleware";\n        fastcgi_param HTTP_REMOTE_USER admin;\n        include /etc/nginx/fastcgi_params;|' "$CONF"
        echo "[grocy-ha] nginx default.conf gepatcht"
        # nginx neu laden falls bereits läuft
        nginx -s reload 2>/dev/null || true
    fi
fi

echo "[grocy-ha] culture=${CULTURE} currency=${CURRENCY} entry=${ENTRY_PAGE}"
