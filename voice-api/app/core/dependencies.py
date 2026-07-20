# app/core/dependencies.py
from fastapi import HTTPException

def verify_voice_secret(provided: str, expected: str):
    """Прямая проверка секрета. Выбрасывает 403 при несовпадении."""
    if not provided or provided != expected:
        raise HTTPException(status_code=403, detail="Invalid X-Secret header")