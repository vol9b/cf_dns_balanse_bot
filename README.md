# Cloudflare DNS Load Balancer

DNS балансировщик для Cloudflare с Telegram-ботом. Мониторит серверы через ICMP, автоматически переключает DNS при падении.

## Возможности

- **Мониторинг** — пинг серверов с anti-flap защитой от ложных срабатываний
- **Автобалансировка** — удаление/добавление DNS записей при изменении доступности
- **Telegram UI** — управление доменами, статистика, SLA в реальном времени
- **SLA трекинг** — месячная статистика доступности для каждого IP
- **Multi-zone** — поддержка нескольких зон и доменов Cloudflare

## Быстрый старт

```bash
git clone https://github.com/user/cf_dns_balanse_bot.git
cd cf_dns_balanse_bot
cp env.example .env
# отредактировать .env
docker-compose up -d
```

## Конфигурация (.env)

| Переменная | Описание |
|------------|----------|
| `CLOUDFLARE_API_TOKEN` | API токен с правами `Zone:DNS:Edit` |
| `CF_ZONE_HOSTNAME` | Зоны и домены: `zone_id:domain,zone_id:domain` |
| `TELEGRAM_BOT_TOKEN` | Токен бота от @BotFather |
| `TELEGRAM_CHAT_ID` | ID чата для уведомлений |
| `PING_INTERVAL_SECONDS` | Интервал пинга (по умолчанию 10) |
| `FLAP_UP_THRESHOLD` | Пингов для UP статуса (по умолчанию 2) |
| `FLAP_DOWN_THRESHOLD` | Пингов для DOWN статуса (по умолчанию 3) |

## Telegram бот

Команды:
- `/start` — главное меню
- `/status` — статус всех серверов с SLA

Через UI можно:
- Добавлять/удалять домены
- Включать/выключать мониторинг
- Смотреть uptime и SLA

```
📊 Статус системы

📈 Здоровье: 75%
    ▓▓▓▓▓▓▓▓▓░░░

🟢 3 онлайн  🔴 1 офлайн

📍 example.com
    🟢 1.2.3.4 онлайн • 2д 5ч | SLA 🟢99.95%
    🔴 5.6.7.8 офлайн • 15м | SLA 🟡99.1%
```

## Структура

```
bot.py           # главный цикл
tg_handler.py    # Telegram UI
cloudflare.py    # CF API
database.py      # SQLite + SLA
health.py        # пинг
notifications.py # отправка сообщений
config.py        # конфиг из .env
```

## Лицензия

MIT
