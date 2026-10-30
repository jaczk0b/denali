#!/bin/sh
# Wysyła eksport awaryjny (procedury, runbooki, spis urządzeń — bez haseł) na VPS.
# Uruchamiany na VM z Denali przez timer systemd (deploy/systemd/denali-sync-export.*), co 5 minut.
#
# Klucz SSH jest osobny i na VPS-ie ograniczony do zapisu w jednym katalogu (rrsync) — patrz deploy/vps/README.md.
# Panel sam niczego nie wysyła i nie uruchamia poleceń: to zwykłe zadanie systemowe hosta.
#
# Ustawienia (w /etc/default/denali-sync-export albo w środowisku):
#   DENALI_EXPORT_DIR   katalog eksportu         (domyślnie /opt/denali/data/export)
#   DENALI_SYNC_TARGET  cel rsync, np. denali-export@vps.example.com:   (dwukropek na końcu — katalog wybiera rrsync)
#   DENALI_SYNC_KEY     klucz prywatny           (domyślnie /etc/denali/sync_ed25519)
set -eu

EXPORT_DIR="${DENALI_EXPORT_DIR:-/opt/denali/data/export}"
TARGET="${DENALI_SYNC_TARGET:?Ustaw DENALI_SYNC_TARGET, np. denali-export@vps.example.com:}"
KEY="${DENALI_SYNC_KEY:-/etc/denali/sync_ed25519}"

if [ ! -f "$EXPORT_DIR/index.html" ]; then
  echo "Brak eksportu w $EXPORT_DIR — czy worker Denali działa?" >&2
  exit 1
fi

# --delay-updates: pliki podmieniane na końcu, VPS nie pokaże pół wersji
exec rsync -a --delete --delay-updates --chmod=D755,F644 \
  -e "ssh -i $KEY -o BatchMode=yes -o StrictHostKeyChecking=yes" \
  "$EXPORT_DIR/" "$TARGET"
