from pydantic_settings import BaseSettings, SettingsConfigDict
# from pydantic import BaseModel, Field


# CONFIG = {
#     "hs": os.getenv("MATRIX_HOMESERVER"),
#     "bot_token": os.getenv("MATRIX_BOT_TOKEN", os.getenv("MATRIX_ACCESS_TOKEN")),
#     "dashboard_allowed_ips": [ip.strip() for ip in os.getenv("DASHBOARD_ALLOWED_IPS", "").split(",") if ip.strip()],
#     "dashboard_room_id": os.getenv("DASHBOARD_MATRIX_ROOM_ID", ""),
#     "dashboard_token": os.getenv("DASHBOARD_API_TOKEN"),  # ← Добавь это
#     "auto_close_attention": int(os.getenv("AUTO_CLOSE_ATTENTION_MINUTES", "0")),
#     "auto_close_alarm": int(os.getenv("AUTO_CLOSE_ALARM_MINUTES", "0")),
#     "history_ttl_hours": int(os.getenv("HISTORY_TTL_HOURS", "48")),
#     "timezone": os.getenv("TIMEZONE", "Asia/Novosibirsk"),
#     "redis_url": os.getenv("REDIS_URL", "redis://redis:6379/4"),
#     "asterisk_api_token": os.getenv("ASTERISK_API_TOKEN", ""),
#     "dispatcher_write_token": os.getenv("DISPATCHER_WRITE_TOKEN", ""),
#     "voice_api_url": os.getenv("VOICE_API_URL", "http://voice-api:8000"),
#     "voice_api_secret": os.getenv("VOICE_API_SECRET", ""),
#     "local_api_token": os.getenv("VOICE_API_SECRET", ""),
#     "voice_api_secret": os.getenv("VOICE_API_SECRET", ""),
# }


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True)

    MATRIX_HOMESERVER: str = "https://matrix.vpk-oil.ru"
    MATRIX_ACCESS_TOKEN: str
    REDIS_URL: str = "redis://redis:6379/4"

    # Дашборд
    DASHBOARD_MATRIX_ROOM_ID: str
    DASHBOARD_API_TOKEN: str
    DASHBOARD_ALLOWED_IPS: str

    TEMPLATE_NORMAL: str="✅ **{location}: НОРМА.**\nСистема работает в штатном режиме.\nВремя: {time}"
    TEMPLATE_ATTENTION: str ="⚠️ **{location}: ВНИМАНИЕ.**\n{comment}\nВремя: {time}"
    TEMPLATE_ALARM: str="🚨 **{location}: ТРЕВОГА!**\n{comment}\nВремя: {time}"

    TIMEZONE: str = "Asia/Novosibirsk"

    
    AUTO_CLOSE_ATTENTION_MINUTES: int = 60
    AUTO_CLOSE_ALARM_MINUTES: int = 0

    # Хранение истории (в часах)
    HISTORY_TTL_HOURS: int = 48
  

    ASTERISK_API_TOKEN: str = None
    VOICE_API_URL: str = "http://voice-api:8000"
    VOICE_API_SECRET: str
    DISPATCHER_WRITE_TOKEN: str
    ASTERISK_ORIGINATE_TIMEOUT: int = 40

    #
    LOCAL_API_TOKEN: str
    ALLOWED_IPS: str = "127.0.0.1"
    
    @property
    def allowed_ips_list(self) -> list[str]:
        return [ip.strip() for ip in self.ALLOWED_IPS.split(",") if ip.strip()]
    
    

settings = Settings()
