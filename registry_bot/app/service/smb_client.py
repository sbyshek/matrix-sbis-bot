import io
import logging
import re
import config
from smb.SMBConnection import SMBConnection

logger = logging.getLogger(__name__)

class SMBClient:
    def __init__(self):
        self.conn = None

    def connect(self):
        logger.info(f"🔗 Подключение к SMB: {config.SMB_SERVER}/{config.SMB_SHARE}")
        self.conn = SMBConnection(
            config.SMB_USERNAME, config.SMB_PASSWORD,
            "registry_bot", config.SMB_SERVER,
            domain=config.SMB_DOMAIN,
            use_ntlm_v2=True,
            is_direct_tcp=True
        )
        self.conn.connect(config.SMB_SERVER, 445)

    def close(self):
        if self.conn:
            self.conn.close()

    def _sanitize_path(self, path: str) -> str:
        # 1. Убираем букву диска (N:, C: и т.д.)
        path = re.sub(r'^[A-Za-z]:\\?', '', path)
        # 2. Нормализуем слэши и убираем лишние
        path = path.replace("\\", "/").strip("/")
        path = re.sub(r'/+', '/', path)
        # 3. Защита от пустого пути
        return path if path else "."

    def walk(self, remote_path: str):
        smb_path = self._sanitize_path(remote_path)
        logger.debug(f"📂 SMB LIST: SHARE='{config.SMB_SHARE}' | PATH='{smb_path}'")
        
        try:
            items = self.conn.listPath(config.SMB_SHARE, smb_path)
        except Exception as e:
            logger.warning(f"⚠️ SMB ошибка доступа к '{smb_path}': {type(e).__name__}: {e}")
            return

        dirs = [i.filename for i in items if i.isDirectory and i.filename not in (".", "..")]
        files = [i.filename for i in items if not i.isDirectory]
        yield remote_path, files

        for d in dirs:
            next_path = f"{remote_path}/{d}" if remote_path != "/" else d
            yield from self.walk(next_path)

    def read_file_bytes(self, dirpath: str, filename: str) -> bytes:
        path = self._sanitize_path(f"{dirpath}/{filename}")
        logger.debug(f"📥 SMB READ: {path}")
        buf = io.BytesIO()
        self.conn.retrieveFile(config.SMB_SHARE, path, buf)
        return buf.getvalue()

    def upload_file(self, remote_dir: str, filename: str, data: bytes):
        path = self._sanitize_path(f"{remote_dir}/{filename}")
        logger.debug(f" SMB WRITE: {path}")
        buf = io.BytesIO(data)
        buf.seek(0)
        self.conn.storeFile(config.SMB_SHARE, path, buf)