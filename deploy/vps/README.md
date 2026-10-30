# Eksport awaryjny na VPS

Gdy dom albo panel nie działa, procedury, runbooki, kontakty i spis urządzeń (HTML i PDF) są na VPS-ie.
Eksport **nie zawiera haseł**: skarbiec zostaje w panelu, a kopia haseł w menedżerze haseł. Zawiera za to adresy IP i
opis sieci, więc na VPS-ie stoi za logowaniem Pangolina, nigdy publicznie.

```
VM z Denali                            VPS
  worker → data/export/  ── rsync/ssh ──▶  /srv/denali-export/  ──▶  serwer plików  ──▶  Pangolin (logowanie)
  (po każdej zmianie,       co 5 min,       klucz może tylko
   co godzinę na pewno)     timer systemd   pisać do tego katalogu
```

## 1. VPS: konto tylko do odbioru plików

```sh
sudo useradd --system --create-home --shell /bin/sh denali-export
sudo mkdir -p /srv/denali-export && sudo chown denali-export: /srv/denali-export
# rrsync ogranicza klucz do zapisu w jednym katalogu (pakiet rsync; w Debianie: /usr/bin/rrsync)
if ! command -v rrsync; then
  sudo install -m 755 /usr/share/doc/rsync/scripts/rrsync /usr/local/bin/rrsync
fi
```

## 2. VM z Denali: klucz i timer

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin denali-sync
sudo mkdir -p /etc/denali
sudo ssh-keygen -t ed25519 -N "" -C denali-sync -f /etc/denali/sync_ed25519
sudo chown denali-sync: /etc/denali/sync_ed25519
sudo setfacl -R -m u:denali-sync:rX /opt/denali/data/export     # albo grupa z prawem odczytu do data/export
```

Na **VPS** do `/home/denali-export/.ssh/authorized_keys` dopisz jedną linię (klucz z `/etc/denali/sync_ed25519.pub`):

```
restrict,command="rrsync -wo /srv/denali-export" ssh-ed25519 AAAA… denali-sync
```

`restrict` wyłącza przekierowania i terminal; `rrsync -wo` pozwala tylko zapisywać w tym katalogu. Ten klucz nie
uruchomi na VPS-ie niczego innego.

Na **VM**: zaakceptuj klucz hosta VPS-a raz ręcznie (`sudo -u denali-sync ssh -i /etc/denali/sync_ed25519 denali-export@VPS`
— połączenie zostanie odrzucone, ale klucz trafi do `known_hosts`), potem:

```sh
echo 'DENALI_SYNC_TARGET=denali-export@vps.example.com:' | sudo tee /etc/default/denali-sync-export
sudo chmod +x /opt/denali/deploy/sync-export.sh
sudo cp /opt/denali/deploy/systemd/denali-sync-export.* /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now denali-sync-export.timer
sudo systemctl start denali-sync-export.service && journalctl -u denali-sync-export -n 20
```

## 3. VPS: serwowanie za Pangolinem

Najprościej osobny kontener z serwerem plików, podpięty w Pangolinie jako zasób **z logowaniem**
(np. `awaryjny.serwispowers.com`):

```yaml
# /opt/denali-export/compose.yml na VPS-ie
services:
  denali-export:
    image: caddy:2.10-alpine
    restart: unless-stopped
    command: caddy file-server --root /srv --listen :8080
    volumes:
      - /srv/denali-export:/srv:ro
    # bez "ports:" — ruch tylko przez Pangolina/Newt w tej samej sieci
```

## 4. Telefon

PDF `denali-procedury.pdf` leży w tym samym katalogu. Pobierz go raz do telefonu i odświeżaj po większych zmianach.
Data na górze każdej strony mówi, z kiedy jest kopia. Jeśli jest starsza niż godzina, a panel działa, to znaczy, że
worker albo synchronizacja stoją.
