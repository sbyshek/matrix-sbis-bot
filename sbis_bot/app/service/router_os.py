import logging
from librouteros import connect
from librouteros.exceptions import LibRouterosError

logger = logging.getLogger(__name__)

class RouterOsClient:
    def __init__(self, config: dict):
        self.host = config.get("host")
        self.username = config.get("username")
        self.password = config.get("password")
        self.api = None

    def __enter__(self):
        try:
            logger.info(f"🔌 Connecting to MikroTik API {self.host}:8728...")
            self.api = connect(
                host=self.host,
                username=self.username,
                password=self.password,
                port=8728,
                timeout=10
            )
            logger.info("✅ MikroTik API connected.")
            return self
        except Exception as e:
            logger.error(f"❌ Failed to connect to MikroTik API: {e}")
            raise

    def add_to_address_list(self, ip: str, list_name: str):
        try:
            logger.info(f" Adding {ip} to address-list '{list_name}'...")
            # Синтаксис librouteros: cmd + ключевые слова аргументов
            self.api(
                '/ip/firewall/address-list/add',
                address=ip,
                list=list_name,
                comment='auto_vpn_bot'
            )
            logger.info("✅ Successfully added to address-list.")
        except LibRouterosError as e:
            logger.error(f"🚫 MikroTik API error: {e}")
            raise RuntimeError(f"MikroTik API failed: {e}")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.api:
            self.api.close()