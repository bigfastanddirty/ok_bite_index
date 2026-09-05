# OK Bite Index

Oklahoma lake telemetry and solunar bite-scoring platform

## Prerequisites & Documentation
- [Install Docker Engine](https://docs.docker.com/engine/install/)
- [Docker Compose Overview & Usage](https://docs.docker.com/compose/)

## Services
- **db**: TimescaleDB / PostgreSQL (`5432`)
- **web**: FastAPI REST service (`8000`)
- **ingestor**: Background telemetry ingestion daemon (USACE, USGS, Open-Meteo)

## Deployment

1. Copy the example environment file:
```bash
cp .env.example .env
```

2. Configure credentials in `.env`:
```env
POSTGRES_USER=lake_admin
POSTGRES_PASSWORD=<POSTGRES PW>
POSTGRES_DB=ok_fishing_db
TZ=America/Chicago
DOCKER_ROOT=<Data Location>
```

3. Build and launch all services:
```bash
docker compose up -d --build
```

*Note: On first startup, `01_init.sql` executes automatically to configure TimescaleDB hypertables and seed the 26 reservoir profiles.*

4. Verify containers are running:
```bash
docker compose ps
docker compose logs -f ingestor
docker compose logs -f web
```

## Historical Data Backfill

Populate past telemetry, thermal layers, and elevation records before live polling:

```bash
docker compose exec ingestor python -m ingestor.backfill --days 30
```

Flags:
- `--days`: Number of historical days to fetch (e.g., `7`, `30`, `90`)
- `--lake`: (Optional) Limit backfill to a specific reservoir name
