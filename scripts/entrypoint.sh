#!/usr/bin/env sh
set -eu

/app/scripts/init_storage.sh

# The anti-ban Compose file explicitly enables this.  Standard deployments
# must retain their direct proxy configuration.
if [ "${ENABLE_ANTI_BAN_PROXY:-false}" = "true" ] && [ -f /app/scripts/init_proxy_config.py ]; then
    python3 /app/scripts/init_proxy_config.py 2>/dev/null || true
fi

exec "$@"
