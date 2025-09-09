#!/bin/bash

# Скрипт управления Cloudflare DNS Load Balancer Bot

case "$1" in
    start)
        echo "🚀 Запускаем бота..."
        docker compose up -d
        ;;
    stop)
        echo "⏹️ Останавливаем бота..."
        docker compose down
        ;;
    restart)
        echo "🔄 Перезапускаем бота..."
        docker compose down
        docker compose up -d
        ;;
    status)
        echo "📊 Статус бота:"
        docker compose ps
        ;;
    logs)
        echo "📋 Логи бота:"
        docker compose logs -f
        ;;
    update)
        echo "⬇️ Обновляем бота..."
        git pull origin main
        docker compose down
        docker compose build --no-cache
        docker compose up -d
        ;;
    rebuild)
        echo "🔨 Пересобираем образ..."
        docker compose down
        docker compose build --no-cache
        docker compose up -d
        ;;
    config)
        echo "⚙️ Редактируем конфигурацию..."
        nano .env
        echo "⚠️ После изменения .env перезапустите бота: ./manage.sh restart"
        ;;
    *)
        echo "Использование: $0 {start|stop|restart|status|logs|update|rebuild|config}"
        echo ""
        echo "Команды:"
        echo "  start   - Запустить бота"
        echo "  stop    - Остановить бота"
        echo "  restart - Перезапустить бота"
        echo "  status  - Показать статус"
        echo "  logs    - Показать логи"
        echo "  update  - Обновить бота"
        echo "  rebuild - Пересобрать образ"
        echo "  config  - Редактировать .env"
        exit 1
        ;;
esac
