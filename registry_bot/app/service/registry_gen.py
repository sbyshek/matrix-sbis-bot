import os
import re
import fitz
import pandas as pd
from datetime import datetime
from io import BytesIO
from .smb_client import SMBClient
import config
import logging

logger = logging.getLogger(__name__)

def sanitize_filename(name: str) -> str:
    """Удаляет символы, запрещённые в именах файлов Windows/SMB"""
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()

def generate_registry(client_drive_path: str):
    if not client_drive_path.upper().startswith(config.CLIENT_DRIVE):
        raise ValueError(f"Путь должен начинаться с {config.CLIENT_DRIVE}")

    # 1. Нормализуем путь (прямые/обратные слэши -> обратные)
    clean_path = client_drive_path.replace('/', '\\').rstrip('\\')
    
    # 2. Извлекаем ТОЛЬКО имя целевой папки
    parts = clean_path.split('\\')
    folder_name = parts[-1] if parts[-1] else "Документы"
    
    # 3. Делаем имя безопасным для файловой системы
    safe_folder_name = sanitize_filename(folder_name) or "Документы"

    # 4. Формируем путь для SMB (убираем диск, меняем на прямые слэши)
    rel_part = clean_path[len(config.CLIENT_DRIVE):].lstrip('\\')
    smb_path = f"/{rel_part}" if rel_part else "/"

    client = SMBClient()
    try:
        client.connect()
        data = []

        for dirpath, filenames in client.walk(smb_path):
            for fname in filenames:
                # Пропускаем уже созданные реестры
                if fname.lower().startswith("реестр_") and fname.lower().endswith(".xlsx"):
                    continue
                    
                try:
                    file_data = client.read_file_bytes(dirpath, fname)
                    size_mb = round(len(file_data) / (1024 * 1024), 3)
                    
                    pages = 1
                    if fname.lower().endswith('.pdf'):
                        try:
                            with fitz.open(stream=file_data, filetype="pdf") as doc:
                                pages = doc.page_count
                        except Exception as e:
                            logger.warning(f"Ошибка чтения PDF {fname}: {e}")
                            pages = "Ошибка"

                    rel_from_target = dirpath.replace(smb_path, "", 1).strip("/")
                    full_rel = f"{rel_from_target}/{fname}" if rel_from_target else fname

                    data.append({
                        "Имя файла": fname,
                        "Путь в папке": full_rel,
                        "Размер (МБ)": size_mb,
                        "Страниц": pages
                    })
                except Exception as e:
                    logger.error(f"Пропущен файл {fname}: {e}")
                    continue

        if not data:
            return None, "️ Файлы не найдены или папка пуста."

        df = pd.DataFrame(data)
        timestamp = datetime.now().strftime("%Y-%m-%d")
        # ✅ Имя файла теперь содержит только имя папки + дату
        output_fname = f"реестр_{safe_folder_name}_{timestamp}.xlsx"

        # Генерация Excel в памяти
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        excel_bytes = output.read()

        # Загружаем в целевую папку на шаре
        client.upload_file(smb_path, output_fname, excel_bytes)

        return output_fname, f"✅ Реестр создан: `{output_fname}` ({len(data)} файлов)"

    finally:
        client.close()