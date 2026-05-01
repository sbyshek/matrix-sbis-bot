from netmiko import ConnectHandler

import config
from logger_config import setup_logging, get_logger


setup_logging(config)
logger = get_logger(__name__)

class RouterOsClient():
    def __init__(self, connect_config):
        self.connect_config = connect_config
        self.connection = None
        logger.info(f"Mikrotik connection string:{self.__connection_string_repr__()}")
        # self.connector = ConnectHandler(**connect_config)
        
    def __enter__(self):
        """Подключение"""
        self.connection = ConnectHandler(**self.connect_config)
        logger.info(f"Connected to MikroTik {self.connect_config['host']}")
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Отключение"""
        if self.connection and self.connection.is_alive():
            self.connection.disconnect()
            logger.info("Disconnected from MikroTik")
        
    def add_to_address_list(self, address: str, list_name: str, timeout: int = 900):
        # Defensive check: do not add obviously-invalid addresses (like 'None.st-oil.local')
        if not address or address.lower().startswith('none') or address.lower().startswith('n/a') or ' none.' in address.lower():
            logger.error(f"Refusing to add invalid address to RouterOS: {address!r}")
            raise ValueError(f"Invalid address: {address}")

        cmd = f"/ip firewall address-list add address={address} list={list_name} timeout={timeout}s comment=SBIS_bot"
        self.connection.send_command(cmd)
        logger.info(f"Added {address} to {list_name} for {timeout}s")
    
    def remove_from_address_list(self, address: str, list_name: str):
        cmd = f'/ip firewall address-list remove [/ip firewall address-list find address={address} list={list_name}]'
        # cmd = f'/ip firewall address-list remove [/ip firewall address-list find comment=SBIS_bot list={list_name}]'
        self.connection.send_command(cmd)
        logger.info(f"Removed {address} from {list_name}")

    def enable_filter_rule_by_comment(self, comment: str):
        """Enable all firewall filter rules that have the given comment."""
        if not comment:
            logger.error("No comment provided to enable_filter_rule_by_comment")
            raise ValueError("comment required")
        # Use find by comment; wrap comment in quotes to handle spaces
        cmd = f'/ip firewall filter enable [find comment="{comment}"]'
        self.connection.send_command(cmd)
        logger.info(f"Enabled firewall filter rules with comment={comment}")

    def disable_filter_rule_by_comment(self, comment: str):
        """Disable all firewall filter rules that have the given comment."""
        if not comment:
            logger.error("No comment provided to disable_filter_rule_by_comment")
            raise ValueError("comment required")
        cmd = f'/ip firewall filter disable [find comment="{comment}"]'
        self.connection.send_command(cmd)
        logger.info(f"Disabled firewall filter rules with comment={comment}")
    
    def __connection_string_repr__(self):
        return f"RouterOS(host={self.connect_config.get('host')}, username={self.connect_config.get('username')})"
        
    # def add_to_address_list(self, address, list_name, timeout=15*60):
    #     self.connector.send_command(f"/ip firewall address-list add address={address} list={list_name} timeout={timeout} comment=SBIS_bot")
        
    # def remove_from_address_list(self, address, list_name):
    #     self.connector.send_command(f'/ip firewall address-list remove [/ip firewall address-list find address={address} list={list_name}]')
        
    
        