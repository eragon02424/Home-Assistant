#!/bin/sh
# Setzt Grocy Umgebungsvariablen aus /data/options.json
OPTIONS="/data/options.json"
INGRESS_PATH="/57f327aa_grocy_linuxserver"

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
    CULTURE="de"; CURRENCY="EUR"; ENTRY_PAGE="stock"; GROCYCODE_TYPE="2D"
    FEAT_BATTERIES="false"; FEAT_CALENDAR="true"; FEAT_CHORES="true"
    FEAT_EQUIPMENT="false"; FEAT_RECIPES="true"; FEAT_SHOPPINGLIST="true"
    FEAT_STOCK="true"; FEAT_TASKS="false"
    TWEAK_CHORES_ASSIGN="true"; TWEAK_COUNT_OPENED="true"
fi

{
    echo "GROCY_CULTURE=${CULTURE}"
    echo "GROCY_CURRENCY=${CURRENCY}"
    echo "GROCY_ENTRY_PAGE=${ENTRY_PAGE}"
    echo "GROCY_GROCYCODE_TYPE=${GROCYCODE_TYPE}"
    echo "GROCY_AUTH_CLASS=Grocy\\Middleware\\ReverseProxyAuthMiddleware"
    echo "GROCY_BASE_PATH=${INGRESS_PATH}"
    echo "GROCY_BASE_URL=${INGRESS_PATH}"
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

echo "[grocy-ha] env gesetzt: culture=${CULTURE} currency=${CURRENCY}"
