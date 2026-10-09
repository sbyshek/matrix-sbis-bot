#!/usr/bin/env python3
"""
Генератор nginx ACL из services.json.
URL и upstream используются как есть — никаких принудительных слэшей.
Ты сам решаешь в JSON, нужен trailing slash или нет.
"""
import json
from pathlib import Path

SERVICES_FILE = Path("app/services.json")
NGINX_DIR = Path("nginx/conf.d/services")


def generate():
    NGINX_DIR.mkdir(parents=True, exist_ok=True)

    with open(SERVICES_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    for category in data["categories"]:
        for service in category["services"]:
            slug = service["id"]
            url = service["url"]                      # как есть: /incident-dashboard или /incident-dashboard/
            allowed_ips = service.get("allowed_ips", [])
            upstream = service.get("upstream", f"http://{slug}:8000")  # как есть
            needs_websocket = service.get("websocket", False)

            # ACL блок
            if allowed_ips:
                acl_lines = "\n".join(f"    allow {ip};" for ip in allowed_ips)
                acl_block = f"""{acl_lines}
    deny all;"""
            else:
                acl_block = "    # Открытый доступ"

            # WebSocket поддержка
            ws_block = ""
            if needs_websocket:
                ws_block = """
    # WebSocket support
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";"""

            # proxy_redirect: убираем префикс из Location-заголовков бэкенда
            # Если url заканчивается на / — используем как есть; если нет — добавляем только в редиректах
            redirect_to = url if url.endswith('/') else url + '/'

            conf = f"""# Auto-generated from services.json — DO NOT EDIT MANUALLY
# Service: {service['name']} ({slug})

location {url} {{
{acl_block}

    proxy_pass {upstream};{ws_block}

    # Стандартные заголовки
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Prefix {url.rstrip('/')};

    # Исправляем редиректы приложения (Location: /login → /incident-dashboard/login)
    proxy_redirect / {redirect_to};

    # Исправляем path в cookies
    proxy_cookie_path / {redirect_to};

    # Таймауты
    proxy_connect_timeout 60s;
    proxy_send_timeout 60s;
    proxy_read_timeout 300s;

    # Размер тела запроса (для загрузки файлов)
    client_max_body_size 50M;
}}
"""

            out_file = NGINX_DIR / f"{slug}.conf"
            out_file.write_text(conf, encoding="utf-8")
            print(f"✅ Generated {out_file}  (url={url}, upstream={upstream})")

    print(f"\n🎯 Применить: docker exec it-gate nginx -t && docker exec it-gate nginx -s reload")


if __name__ == "__main__":
    generate()