#!/bin/sh

set -eu

if alembic upgrade head; then
  :
else
  migration_status=$?
  cat >&2 <<'EOF'
Database migration failed. If this is an old disposable pre-Alembic database, stop the service, back up or rename the database manually, then retry.
The application never deletes or stamps databases automatically.
EOF
  exit "$migration_status"
fi

exec uvicorn src.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-9999}"
