#!/bin/bash

# 🤖 Cloudflare DNS Load Balancer Bot - Автоматическая установка на Ubuntu 24.04
# Скрипт для быстрого развертывания бота на VPS

set -e  # Остановить выполнение при ошибке

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Функция для вывода сообщений
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Проверка, что скрипт запущен от root или с sudo
if [[ $EUID -eq 0 ]]; then
    print_warning "Скрипт запущен от root. Это нормально для установки."
elif ! sudo -n true 2>/dev/null; then
    print_error "Этот скрипт требует права sudo. Запустите с sudo или войдите как root."
    exit 1
fi

print_status "🚀 Начинаем установку Cloudflare DNS Load Balancer Bot..."

# Обновление системы
print_status "📦 Обновляем систему..."
if [[ $EUID -eq 0 ]]; then
    apt update && apt upgrade -y
else
    sudo apt update && sudo apt upgrade -y
fi

# Установка необходимых пакетов
print_status "🔧 Устанавливаем необходимые пакеты..."
PACKAGES="curl wget git python3 python3-pip python3-venv sqlite3"
if [[ $EUID -eq 0 ]]; then
    apt install -y $PACKAGES
else
    sudo apt install -y $PACKAGES
fi

# Установка Docker и Docker Compose
print_status "🐳 Устанавливаем Docker и Docker Compose..."
if ! command -v docker &> /dev/null; then
    # Удаляем старые версии Docker
    if [[ $EUID -eq 0 ]]; then
        apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
    else
        sudo apt remove -y docker docker-engine docker.io containerd runc 2>/dev/null || true
    fi
    
    # Добавляем официальный репозиторий Docker
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | if [[ $EUID -eq 0 ]]; then gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg; else sudo gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg; fi
    
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | if [[ $EUID -eq 0 ]]; then tee /etc/apt/sources.list.d/docker.list > /dev/null; else sudo tee /etc/apt/sources.list.d/docker.list > /dev/null; fi
    
    # Обновляем пакеты и устанавливаем Docker
    if [[ $EUID -eq 0 ]]; then
        apt update
        apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    else
        sudo apt update
        sudo apt install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
    fi
    
    # Добавляем текущего пользователя в группу docker
    if [[ $EUID -ne 0 ]]; then
        sudo usermod -aG docker $USER
        print_warning "Пользователь $USER добавлен в группу docker. Перелогиньтесь для применения изменений."
    fi
    
    # Запускаем Docker
    if [[ $EUID -eq 0 ]]; then
        systemctl enable docker
        systemctl start docker
    else
        sudo systemctl enable docker
        sudo systemctl start docker
    fi
else
    print_success "Docker уже установлен"
fi

# Создание директории для проекта
PROJECT_DIR="/opt/cf-dns-bot"
print_status "📁 Создаем директорию проекта: $PROJECT_DIR"
if [[ $EUID -eq 0 ]]; then
    mkdir -p $PROJECT_DIR
    cd $PROJECT_DIR
else
    sudo mkdir -p $PROJECT_DIR
    sudo chown $USER:$USER $PROJECT_DIR
    cd $PROJECT_DIR
fi

# Скачивание проекта с GitHub
print_status "⬇️ Скачиваем проект с GitHub..."
if [[ -d ".git" ]]; then
    print_status "Проект уже существует, обновляем..."
    git pull origin main
else
    git clone https://github.com/vol9b/cf_dns_balanse_bot.git .
fi

# Создание файла .env из примера
print_status "⚙️ Создаем файл конфигурации..."
if [[ ! -f ".env" ]]; then
    cp env.example .env
    print_warning "Создан файл .env из примера. ОБЯЗАТЕЛЬНО отредактируйте его перед запуском!"
    print_warning "Минимально нужно заполнить:"
    print_warning "  - CLOUDFLARE_API_TOKEN"
    print_warning "  - CF_ZONE_ID" 
    print_warning "  - CF_HOSTNAME"
else
    print_success "Файл .env уже существует"
fi

# Создание systemd сервиса для автозапуска
print_status "🔧 Создаем systemd сервис..."
SERVICE_FILE="/etc/systemd/system/cf-dns-bot.service"
if [[ $EUID -eq 0 ]]; then
    cat > $SERVICE_FILE << EOF
[Unit]
Description=Cloudflare DNS Load Balancer Bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
else
    sudo tee $SERVICE_FILE > /dev/null << EOF
[Unit]
Description=Cloudflare DNS Load Balancer Bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PROJECT_DIR
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
fi

# Перезагрузка systemd и включение сервиса
if [[ $EUID -eq 0 ]]; then
    systemctl daemon-reload
    systemctl enable cf-dns-bot.service
else
    sudo systemctl daemon-reload
    sudo systemctl enable cf-dns-bot.service
fi

# Создание скрипта управления
print_status "📝 Создаем скрипт управления..."
cat > manage.sh << 'EOF'
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
EOF

chmod +x manage.sh

print_success "✅ Установка завершена!"
echo ""
print_status "📋 Следующие шаги:"
echo "1. Отредактируйте конфигурацию:"
echo "   cd $PROJECT_DIR"
echo "   nano .env"
echo ""
echo "2. Запустите бота:"
echo "   ./manage.sh start"
echo ""
echo "3. Проверьте логи:"
echo "   ./manage.sh logs"
echo ""
print_warning "⚠️ ВАЖНО: Обязательно заполните в .env:"
print_warning "   - CLOUDFLARE_API_TOKEN (ваш API токен Cloudflare)"
print_warning "   - CF_ZONE_ID (ID вашей зоны)"
print_warning "   - CF_HOSTNAME (ваши домены)"
echo ""
print_status "🔧 Управление ботом:"
echo "   ./manage.sh start    - Запустить"
echo "   ./manage.sh stop     - Остановить"
echo "   ./manage.sh restart  - Перезапустить"
echo "   ./manage.sh status   - Статус"
echo "   ./manage.sh logs     - Логи"
echo "   ./manage.sh update   - Обновить"
echo "   ./manage.sh config   - Редактировать .env"
echo ""
print_success "🎉 Бот готов к работе!"
