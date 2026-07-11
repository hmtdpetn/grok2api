#!/usr/bin/env python3
"""
Init script: 在 grok2api 启动前写入代理配置到 data/config.toml。
用于 docker-compose.warp.yml 的 init-config 服务。
"""
import pathlib

DATA_DIR = pathlib.Path("/app/data")
CONFIG_PATH = DATA_DIR / "config.toml"

PROXY_CONFIG = """
[proxy.egress]
mode = "single_proxy"
proxy_url = "http://privoxy:8118"
resource_proxy_url = "http://privoxy:8118"
proxy_pool = []
resource_proxy_pool = []
skip_ssl_verify = false

[proxy.clearance]
mode = "flaresolverr"
cf_cookies = ""
user_agent = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
browser = "chrome136"
flaresolverr_url = "http://flaresolverr:8191"
timeout_sec = 60
refresh_interval = 3600
"""

DATA_DIR.mkdir(parents=True, exist_ok=True)

if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text(PROXY_CONFIG.strip() + "\n")
    print("[init-config] Created config.toml with proxy settings")
else:
    content = CONFIG_PATH.read_text()
    if "privoxy" not in content:
        # 替换已有的 proxy 配置段，或追加
        import re
        # 移除旧的 proxy.egress 和 proxy.clearance 段
        content = re.sub(
            r'\[proxy\.egress\].*?(?=\[|\Z)',
            '',
            content,
            flags=re.DOTALL,
        )
        content = re.sub(
            r'\[proxy\.clearance\].*?(?=\[|\Z)',
            '',
            content,
            flags=re.DOTALL,
        )
        content = content.rstrip() + "\n" + PROXY_CONFIG.strip() + "\n"
        CONFIG_PATH.write_text(content)
        print("[init-config] Updated config.toml with proxy settings")
    else:
        print("[init-config] Proxy settings already present, skipping")
