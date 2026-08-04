from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field
from pathlib import Path
from typing import Optional, List
import json



class RoutingRule(BaseModel):
    """Правило маршрутизации по шаблону номера"""
    pattern: str          # Regex-паттерн (например, "^4[0-4]\\d{2}$")
    channel: str          # Тип канала (SIP, IAX2/office, trunk)
    description: str = "" # Человекочитаемое описание

class AsteriskNode(BaseModel):
    """Конфигурация одного Asterisk-узла"""
    id: str
    name: str
    host: str
    port: int = 5038
    username: str
    secret: str
    internal_prefix: str = "4"
    internal_max_length: int = 5
    external_trunks: List[str] = Field(default_factory=list)
    max_concurrent_per_trunk: int = 5
    caller_id: str = "Voice Alert <8800>"
    active: bool = True
    routing_rules: List[RoutingRule] = Field(default_factory=list)  # 🔥 НОВОЕ ПОЛЕ
    
class AsteriskNodesConfig(BaseModel):
    """Конфигурация всех узлов"""
    nodes: List[AsteriskNode] = Field(default_factory=list)

def load_asterisk_nodes(config_path: str = "config/asterisk_nodes.json") -> AsteriskNodesConfig:
    """Загружает конфигурацию Asterisk-узлов из JSON файла"""
    path = Path(config_path)
    if not path.exists():
        return AsteriskNodesConfig()
    with open(path, "r", encoding="utf-8") as f:
        return AsteriskNodesConfig(**json.load(f))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    # API
    VOICE_API_SECRET: str
    VOICE_API_PORT: int = 8000

    # Redis
    REDIS_URL: str = "redis://redis:6379/3"

    # Matrix
    # MATRIX_HOMESERVER: str
    # MATRIX_BOT_TOKEN: str
    # MATRIX_FEEDBACK_ROOM_ID: str

    # STT
    WHISPER_MODEL: str = "small"
    WHISPER_COMPUTE_TYPE: str = "int8"
    WHISPER_DEVICE: str = "cpu"
    WHISPER_LANGUAGE: str = "ru"
    WHISPER_PROMPT: str = ""

    # Storage
    VOICE_DATA_DIR: str = "/app/data/voice"
    VOICE_RETENTION_HOURS: int = 24

    # Asterisk
    VOICE_MAX_CONCURRENT: int = 2
    ASTERISK_CALLBACK_TIMEOUT: int = 90
    
    # Incident Dispatcher
    INCIDENT_DISPATCHER_URL: str = "http://incident-dispatcher:8000"
    INCIDENT_DISPATCHER_TOKEN: str = ""
    
    
    
    @property
    def asterisk_nodes_list(self) -> List[AsteriskNode]:
        return [AsteriskNode(**node) for node in json.loads(self.ASTERISK_NODES)]

settings = Settings()
asterisk_config = load_asterisk_nodes()