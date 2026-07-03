#!/usr/bin/with-contenv bashio

# nginx ingress config bereitstellen
mkdir -p /config/nginx/site-confs
cp /ingress.conf.tpl /config/nginx/site-confs/ingress.conf

# Werte aus HA Addon Config lesen
CULTURE=$(bashio::config 'culture')
CURRENCY=$(bashio::config 'currency')
ENTRY_PAGE=$(bashio::config 'entry_page')
GROCYCODE_TYPE=$(bashio::config 'grocycode_type')

# Features
FEAT_BATTERIES=$(bashio::config 'features.batteries')
FEAT_CALENDAR=$(bashio::config 'features.calendar')
FEAT_CHORES=$(bashio::config 'features.chores')
FEAT_EQUIPMENT=$(bashio::config 'features.equipment')
FEAT_RECIPES=$(bashio::config 'features.recipes')
FEAT_SHOPPINGLIST=$(bashio::config 'features.shoppinglist')
FEAT_STOCK=$(bashio::config 'features.stock')
FEAT_TASKS=$(bashio::config 'features.tasks')

# Tweaks
TWEAK_CHORES_ASSIGN=$(bashio::config 'tweaks.chores_assignment')
TWEAK_MULTI_SHOP=$(bashio::config 'tweaks.multiple_shopping_lists')
TWEAK_BBD=$(bashio::config 'tweaks.stock_best_before_date_tracking')
TWEAK_LOCATION=$(bashio::config 'tweaks.stock_location_tracking')
TWEAK_PRICE=$(bashio::config 'tweaks.stock_price_tracking')
TWEAK_FREEZE=$(bashio::config 'tweaks.stock_product_freezing')
TWEAK_OPENED=$(bashio::config 'tweaks.stock_product_opened_tracking')
TWEAK_COUNT_OPENED=$(bashio::config 'tweaks.stock_count_opened_products_against_minimum_stock_amount')

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

bashio::log.info "Grocy: culture=${CULTURE} currency=${CURRENCY} entry=${ENTRY_PAGE}"
