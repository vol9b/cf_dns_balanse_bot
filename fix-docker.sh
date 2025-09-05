#!/bin/bash

# Скрипт для исправления проблем с Docker

echo "🔧 Исправляем проблемы с Docker..."

# Останавливаем контейнеры
echo "⏹️ Останавливаем контейнеры..."
docker compose down 2>/dev/null || true

# Удаляем старые образы
echo "🗑️ Удаляем старые образы..."
docker rmi cf_dns_balanse_bot-cf-dns-bot 2>/dev/null || true

# Очищаем кэш Docker
echo "🧹 Очищаем кэш Docker..."
docker system prune -f

# Пересобираем образ
echo "🔨 Пересобираем образ..."
docker compose build --no-cache

# Запускаем
echo "🚀 Запускаем бота..."
docker compose up -d

echo "✅ Готово! Проверьте статус:"
echo "docker compose ps"
echo "docker compose logs -f"
