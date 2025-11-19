# Cloudflare DNS Load Balancer

High-performance DNS load balancer for Cloudflare managed domains. Written in Python using **AsyncIO**, it monitors server availability via ICMP ping and automatically updates DNS records to ensure high availability.

## Key Features

*   **Asynchronous Core**: Built with `asyncio` and `aiohttp` for non-blocking I/O, capable of monitoring dozens of hosts simultaneously with minimal resource usage.
*   **Stateful Logic**: Uses SQLite (`aiosqlite`) to track host state and prevent "flapping" (rapidly switching between up/down states).
*   **Anti-Flap System**: Configurable thresholds for marking a host as UP or DOWN.
*   **Dockerized**: Ready-to-use Docker container with `docker-compose` support.
*   **Notifications**: Telegram alerts on status changes.

## Installation

### Docker Compose (Recommended)

1.  Clone the repository:
    ```bash
    git clone https://github.com/vol9b/cf_dns_balanse_bot.git
    cd cf_dns_balanse_bot
    ```

2.  Configure environment:
    ```bash
    cp env.example .env
    nano .env
    ```

3.  Start the service:
    ```bash
    docker-compose up -d
    ```

### Automated Script (Ubuntu/Debian)

```bash
curl -fsSL https://raw.githubusercontent.com/vol9b/cf_dns_balanse_bot/main/install.sh | sudo bash
```

## Configuration

Configuration is handled entirely via the `.env` file.

### Core Settings

| Variable | Description | Default |
|----------|-------------|---------|
| `CLOUDFLARE_API_TOKEN` | **Required**. Cloudflare API Token with `Zone:DNS:Edit` permissions. | - |
| `CF_ZONE_HOSTNAME` | **Required**. List of monitored zones and hosts. | - |
| `CF_RECORD_TYPES` | DNS record types to manage (comma-separated). | `A` |
| `CF_PROXIED` | Whether to enable Cloudflare Proxy (orange cloud). | `false` |

### Monitoring & Logic

| Variable | Description | Default |
|----------|-------------|---------|
| `PING_INTERVAL_SECONDS` | Interval between ping checks. | `10` |
| `CF_SYNC_INTERVAL_MINUTES` | Full sync with Cloudflare API. | `3` |
| `FLAP_UP_THRESHOLD` | Consecutive successful pings to mark host UP. | `2` |
| `FLAP_DOWN_THRESHOLD` | Consecutive failed pings to mark host DOWN. | `3` |

### Host Configuration Format

The `CF_ZONE_HOSTNAME` variable defines which domains to monitor. It supports multiple zones and multiple domains per zone.

**Format:** `zone_id:domain,zone_id:domain`

**Example:**
```env
# Monitor 'app.example.com' in Zone A and 'api.test.com' in Zone B
CF_ZONE_HOSTNAME=zone_id_A:app.example.com,zone_id_B:api.test.com
```

## Architecture

The bot operates in a continuous loop:

1.  **Sync**: Periodically fetches current DNS records from Cloudflare to ensure the local DB is in sync.
2.  **Check**: Pings all known IPs for the configured domains in parallel.
3.  **Evaluate**: Updates the local state (SQLite). Applies anti-flap logic.
4.  **Reconcile**: If the stable state changes (e.g., host goes DOWN):
    *   Sends a Telegram notification.
    *   Updates Cloudflare DNS records (removes DOWN hosts, adds UP hosts).

## Management

A helper script `manage.sh` is included for common tasks:

```bash
./manage.sh logs    # View logs
./manage.sh restart # Restart container
./manage.sh update  # Pull latest changes and rebuild
```

## License

MIT License