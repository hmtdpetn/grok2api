#!/usr/bin/env sh
set -eu

/app/scripts/init_storage.sh

# When deploying the anti-ban (WARP) stack, auto-configure proxy settings
# before the application starts so grok2api can reach grok.com through the tunnel.
if [ -f /app/scripts/init_proxy_config.py ]; then
    python3 /app/scripts/init_proxy_config.py 2>/dev/null || true
fi

exec "$@"
