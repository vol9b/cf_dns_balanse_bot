# 🤖 Cloudflare DNS Load Balancer Bot

Автоматическая балансировка DNS-записей на основе доступности серверов. Бот пингует серверы и обновляет A/AAAA-записи в Cloudflare, оставляя только доступные.

## 🚀 Быстрый старт

### Автоматическая установка (рекомендуется)

```bash
curl -fsSL https://raw.githubusercontent.com/vol9b/cf_dns_balanse_bot/main/install.sh | sudo bash
```

После установки:
```bash
cd /opt/cf-dns-bot
sudo nano .env  # Настройте конфигурацию
./manage.sh start
```

### Ручная установка

```bash
git clone https://github.com/vol9b/cf_dns_balanse_bot.git
cd cf_dns_balanse_bot
cp env.example .env
nano .env  # Настройте конфигурацию
docker compose up -d
```

## ⚙️ Конфигурация

Обязательные переменные в `.env`:

```env
# Cloudflare
CLOUDFLARE_API_TOKEN=your_token
CF_ZONE_HOSTNAME=zone_id_1:app.example.com,zone_id_2:api.example.com
```

### 📋 Формат CF_ZONE_HOSTNAME

Поддерживает любое количество доменов в каждой зоне:

```env
# Пример: 3 домена в зоне 1, 2 домена в зоне 2
CF_ZONE_HOSTNAME=zone_id_1:domain1.com,zone_id_1:domain2.com,zone_id_1:domain3.com,zone_id_2:domain4.com,zone_id_2:domain5.com
```

**Правила:**
- Формат: `zone_id:domain.com`
- Разделители: запятые `,` между парами
- Пробелы: можно добавлять для читабельности
- Порядок: не важен, бот сам сгруппирует по зонам

### Опциональные настройки

| Переменная | Описание | По умолчанию |
|------------|----------|--------------|
| `CF_RECORD_TYPES` | Типы DNS записей (A,AAAA) | `A` |
| `CF_PROXIED` | Проксирование через Cloudflare | `false` |
| `PING_INTERVAL_SECONDS` | Интервал пинга (сек) | `10` |
| `CF_SYNC_INTERVAL_MINUTES` | Интервал синхронизации (мин) | `3` |
| `FLAP_UP_THRESHOLD` | Порог для подъема сервера | `2` |
| `FLAP_DOWN_THRESHOLD` | Порог для падения сервера | `3` |
| `CF_MANAGE_DNS` | Управление DNS записями | `true` |
| `CF_DB_PATH` | Путь к базе данных | `./cf_dns.db` |
| `LOG_LEVEL` | Уровень логирования (DEBUG,INFO,WARNING,ERROR) | `INFO` |
| `TELEGRAM_ENABLED` | Telegram уведомления | `false` |
| `TELEGRAM_BOT_TOKEN` | Токен бота | |
| `TELEGRAM_CHAT_ID` | ID чата | |

## 🔧 Управление

```bash
cd /opt/cf-dns-bot

./manage.sh start    # Запустить
./manage.sh stop     # Остановить
./manage.sh restart  # Перезапустить
./manage.sh status   # Статус
./manage.sh logs     # Логи
./manage.sh update   # Обновить
./manage.sh config   # Редактировать .env
```

## 🔑 Получение токенов

### API Token Cloudflare
1. [Cloudflare Dashboard](https://dash.cloudflare.com/profile/api-tokens) → Create Token
2. Custom token → Permissions: `Zone:Zone:Read`, `Zone:DNS:Edit`
3. Zone Resources: `Include:All zones`

### Zone ID и домены
1. [Cloudflare Dashboard](https://dash.cloudflare.com) → Выберите домен
2. Правая панель → Zone ID
3. Для каждого домена повторите шаги 1-2
4. Используйте формат: `CF_ZONE_HOSTNAME=zone_id:domain.com,zone_id:another.com`

**Пример для множественных доменов:**
```env
# 3 домена в зоне 1, 2 домена в зоне 2
CF_ZONE_HOSTNAME=zone_id_1:domain1.com,zone_id_1:domain2.com,zone_id_1:domain3.com,zone_id_2:domain4.com,zone_id_2:domain5.com
```

### Telegram Bot (опционально)
1. [@BotFather](https://t.me/botfather) → `/newbot`
2. Скопируйте токен
3. Для Chat ID: напишите боту и перейдите по ссылке:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`

## 📊 Как это работает

- Бот пингует серверы каждые 10 секунд
- При падении сервера (3 неудачных пинга) удаляет его из DNS
- При подъеме сервера (2 успешных пинга) добавляет обратно
- Синхронизируется с Cloudflare каждые 3 минуты
- Отправляет уведомления в Telegram при изменениях

## ⚡ Оптимизация

По умолчанию настроено для критически важных сервисов:
- Быстрое восстановление (20 сек)
- Умеренное обнаружение падения (30 сек)
- Минимальный простой при TTL=1 минута

## 📄 Лицензия

MIT License