#!/bin/sh
# Run with: sh tools/deploy.sh up
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

usage() {
    cat <<'EOF'
Usage: sh tools/deploy.sh COMMAND [OPTIONS]
  check                 Validate Compose and the delivered files (host Python3).
  up                    Build, start and wait for healthy services.
  status                Show service status and published ports.
  logs [OPTIONS]        Show logs; e.g. logs -f backend
  models                Verify all private model SHA-256 hashes in the backend.
  environment           Check installed versions and model imports in the backend.
  smoke [OPTIONS]       Run HTTP examples in the backend container.
                        Default: --case import-benchmark --download
                        Full: --all --download --cleanup
  down                  Stop services; retain runtime/ and dataset/.
EOF
}

command_name=${1:-help}
case "$command_name" in
    help|-h|--help) usage; exit 0 ;;
    check|up|status|logs|models|environment|smoke|down) shift ;;
    *) usage >&2; exit 2 ;;
esac

command -v docker >/dev/null 2>&1 || { echo 'Docker is not installed or not on PATH.' >&2; exit 1; }
docker compose version >/dev/null
docker info >/dev/null
if [ ! -f .env ]; then
    cp .env.example .env
    echo 'Created .env from .env.example; an existing .env is never overwritten.'
fi
mkdir -p runtime dataset/business

case "$command_name" in
    check)
        docker compose config --quiet
        if command -v python3 >/dev/null 2>&1; then
            python3 -B tools/verify_package.py
        else
            echo 'Compose is valid. Install host Python3 to run tools/verify_package.py.' >&2
            exit 1
        fi
        ;;
    up)
        docker compose config --quiet
        docker compose up -d --build --wait --wait-timeout "${SME_START_WAIT_SECONDS:-180}"
        docker compose ps
        echo 'Frontend binding:'
        docker compose port frontend 80
        echo 'Entry container: op-pre-eva; frontend and APIs share this published port.'
        ;;
    status) docker compose ps ;;
    logs) docker compose logs --tail=100 "$@" ;;
    models) docker compose exec -T backend python -B tools/verify_models.py ;;
    environment) docker compose exec -T backend python -B tools/check_environment.py ;;
    smoke)
        if [ "$#" -eq 0 ]; then set -- --case import-benchmark --download; fi
        docker compose exec -T backend python -B tools/run_example.py --url http://127.0.0.1:6289 "$@"
        ;;
    down) docker compose down ;;
esac
