# Развёртывание конкурса оценивания на VPS (Ubuntu 22.04 / 24.04)

Пошаговая инструкция: от чистого сервера до работающего HTTPS-сайта.
Ориентировочное время — 20 минут.

Везде ниже замените:

- `DOMAIN` — ваш домен, например `konkurs.example.com`
- `SERVER_IP` — IP-адрес VPS
- пути и имена пользователей — если отличаются

---

## 1. VPS и домен

1. Купите VPS с Ubuntu 22.04 или 24.04 (минимум 1 vCPU / 1 GB RAM).
2. Купите домен (например, у регистратора доменов).
3. В DNS-панели домена создайте A-записи:

   | Тип | Имя | Значение |
   |-----|-----|----------|
   | A | `@` | `SERVER_IP` |
   | A | `www` | `SERVER_IP` |

4. Дождитесь обновления DNS (обычно 5–30 минут). Проверка:

   ```bash
   dig +short DOMAIN
   ```

Первый вход на сервер:

```bash
ssh root@SERVER_IP
```

---

## 2. Базовые пакеты

```bash
apt update && apt upgrade -y
apt install -y python3 python3-venv python3-pip nginx certbot python3-certbot-nginx git ufw
```

---

## 3. Пользователь deploy и каталог приложения

```bash
adduser --disabled-password --gecos "" deploy
mkdir -p /var/www/konkurs
chown -R deploy:www-data /var/www/konkurs
chmod -R 775 /var/www/konkurs
```

Далее можно работать под `deploy` (`su - deploy`) или заливать файлы от root.

---

## 4. Загрузка кода

Скопируйте файлы проекта (`app.py`, `models.py`, `seed_data.py`, `extensions.py`,
`wsgi.py`, `requirements.txt`, `.env.example`, каталоги `templates/`, `deploy/`,
`static/`) в `/var/www/konkurs`.

Варианты:

```bash
# вариант А: с локальной машины
scp -r ./konkurs/* root@SERVER_IP:/var/www/konkurs/

# вариант Б: из git-репозитория
cd /var/www/konkurs
git clone <URL_РЕПОЗИТОРИЯ> .
```

---

## 5. Виртуальное окружение и зависимости

```bash
cd /var/www/konkurs
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

---

## 6. Файл .env и SECRET_KEY

Сгенерируйте ключ:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Создайте `.env` (на основе `.env.example`):

```bash
cp .env.example .env
nano .env
```

Пример содержимого:

```
SECRET_KEY=<вставьте_сгенерированный_ключ>
DATABASE_PATH=/var/www/konkurs/konkurs.db
FLASK_ENV=production
SESSION_COOKIE_SECURE=1
ADMIN_EMAIL=admin@example.com
```

Защитите файл:

```bash
chmod 600 .env
```

---

## 7. Инициализация базы данных

```bash
cd /var/www/konkurs
./venv/bin/python app.py --init
```

Будут созданы БД `konkurs.db`, все 60 критериев, 5 команд и пользователи:

- `admin` / `admin` — администратор
- `ekspert1` … `ekspert5` — эксперты (пароль совпадает с логином)

Эксперты обязаны сменить пароль при первом входе.

Дополнительно в админке можно создавать пользователей с ролью
**«Наблюдатель»** (`/admin/users` → роль «Наблюдатель»): у них доступ только
к онлайн-табло результатов `/observer`, без возможности что-либо менять.

Пересоздание БД с нуля (удаляет все данные, спросит подтверждение):

```bash
./venv/bin/python app.py --init --force
```

---

## 8. Права доступа

Процесс Gunicorn работает от `www-data` и должен читать код и писать в каталог с БД:

```bash
chown -R deploy:www-data /var/www/konkurs
chmod -R 775 /var/www/konkurs
chmod 664 /var/www/konkurs/konkurs.db
```

---

## 9. systemd-сервис

```bash
cp /var/www/konkurs/deploy/konkurs.service /etc/systemd/system/konkurs.service
systemctl daemon-reload
systemctl enable konkurs
systemctl start konkurs
systemctl status konkurs --no-pager
```

Если статус `active (running)` — приложение поднялось на unix-сокете
`/var/www/konkurs/konkurs.sock`.

---

## 10. Nginx: временный HTTP-конфиг и получение сертификата

Готовый `deploy/nginx.conf` рассчитан на уже существующий сертификат Let's Encrypt,
поэтому сначала получаем сертификат, а затем включаем целевой конфиг.

Создайте временный HTTP-сайт:

```bash
rm -f /etc/nginx/sites-enabled/default

cat > /etc/nginx/sites-available/konkurs-tmp <<'EOF'
server {
    listen 80;
    listen [::]:80;
    server_name konkurs.example.com www.konkurs.example.com;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 200 'ok';
        add_header Content-Type text/plain;
    }
}
EOF

ln -sf /etc/nginx/sites-available/konkurs-tmp /etc/nginx/sites-enabled/konkurs-tmp
nginx -t
systemctl reload nginx
```

Получите сертификат (certbot сам создаст `/etc/letsencrypt/...`
и файл `options-ssl-nginx.conf`):

```bash
certbot --nginx -d konkurs.example.com -d www.konkurs.example.com
```

Certbot спросит e-mail и согласие; при запросе редиректа на HTTPS можно ответить «Yes».

Проверка автопродления:

```bash
certbot renew --dry-run
```

---

## 11. Nginx: включение целевого HTTPS-конфига

Теперь, когда сертификаты на месте, включаем боевой конфиг:

```bash
cp /var/www/konkurs/deploy/nginx.conf /etc/nginx/sites-available/konkurs
sed -i 's/DOMAIN/konkurs.example.com/g' /etc/nginx/sites-available/konkurs
ln -sfn /etc/nginx/sites-available/konkurs /etc/nginx/sites-enabled/konkurs
rm -f /etc/nginx/sites-enabled/konkurs-tmp
nginx -t
systemctl reload nginx
```

`nginx -t` должен завершиться сообщением `syntax is ok` / `test is successful`.

Откройте в браузере `https://konkurs.example.com` — должна открыться страница входа.

---

## 12. Файрвол (ufw)

```bash
ufw allow OpenSSH
ufw allow 'Nginx Full'
ufw --force enable
ufw status
```

---

## 13. Ежедневный бэкап базы данных

Создайте скрипт:

```bash
cat > /usr/local/bin/konkurs_backup.sh <<'EOF'
#!/bin/bash
set -e
BACKUP_DIR=/var/backups/konkurs
mkdir -p "$BACKUP_DIR"
sqlite3 /var/www/konkurs/konkurs.db ".backup '$BACKUP_DIR/konkurs_$(date +%Y%m%d_%H%M).db'"
find "$BACKUP_DIR" -name 'konkurs_*.db' -mtime +14 -delete
EOF
chmod +x /usr/local/bin/konkurs_backup.sh
```

Если `sqlite3` не установлен: `apt install -y sqlite3`.

Добавьте в cron (от root):

```bash
crontab -e
```

Строка — ежедневно в 03:00:

```
0 3 * * * /usr/local/bin/konkurs_backup.sh >> /var/log/konkurs_backup.log 2>&1
```

Резервную копию также можно скачать из админки: `/admin` → «Резервная копия».

---

## 14. Обновление приложения

```bash
cd /var/www/konkurs
# замените файлы на новые
./venv/bin/pip install -r requirements.txt
systemctl restart konkurs
systemctl status konkurs --no-pager
```

---

## 15. Диагностика

Логи приложения:

```bash
journalctl -u konkurs -n 100 --no-pager
```

Логи Nginx:

```bash
tail -n 100 /var/log/nginx/konkurs.error.log
```

Частые проблемы:

- **502 Bad Gateway** — сервис не запущен или нет доступа к сокету.
  Проверьте `systemctl status konkurs` и права на `/var/www/konkurs`.
- **CSRF-ошибка при входе** — истёк/не задан `SECRET_KEY`. Проверьте `.env`.
- **Оценки не сохраняются** — включена блокировка оценивания в админке.
- **Сайт открывается по IP, но не по домену** — проверьте A-записи и `nginx -t`.
