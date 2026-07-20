# app/services/stt.py
import os
import logging
from faster_whisper import WhisperModel
from app.core.config import settings

logger = logging.getLogger("voice-api.stt")

class STTService:
    """
    Сервис транскрибации речи на базе faster-whisper.
    Оптимизирован для промышленных условий (шум, спецтермины).
    """
    
    def __init__(self):
        self.model = None
        self._is_loaded = False
        
    def load_model(self):
        """
        Загружает модель в память. 
        Лучше вызывать один раз при старте приложения.
        """
        if self._is_loaded:
            return
            
        logger.info(f" Loading Whisper model '{settings.WHISPER_MODEL}' on {settings.WHISPER_DEVICE}...")
        
        try:
            self.model = WhisperModel(
                model_size_or_path=settings.WHISPER_MODEL,
                device=settings.WHISPER_DEVICE,
                compute_type=settings.WHISPER_COMPUTE_TYPE,
                local_files_only=False # Если нужно качать при старте
            )
            self._is_loaded = True
            logger.info("✅ Whisper model loaded successfully.")
            
        except Exception as e:
            logger.error(f"❌ Failed to load Whisper model: {e}")
            raise

    def transcribe(self, audio_path: str) -> str:
        """
        Транскрибирует аудиофайл в текст.
        """
        if not self.model:
            self.load_model()
            
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
            
        logger.debug(f"🔊 Transcribing: {audio_path}")
        
        try:
            # VAD_FILTER=True критически важен для телефонии:
            # он убирает длинные паузы и фоновый шум, оставляя только речь.
            segments, _ = self.model.transcribe(
                audio_path,
                language=settings.WHISPER_LANGUAGE,
                beam_size=5,          # Точность поиска
                vad_filter=True,      # ВКЛЮЧИТЬ фильтр активности голоса
                vad_parameters=dict(
                    min_silence_duration_ms=500, # Игнорировать тишину короче 0.5с
                    speech_pad_ms=100            # Добавить контекст вокруг речи
                ),
                # PROMPT помогает модели не "галлюцинировать" и знать термины
                prompt=settings.WHISPER_PROMPT 
            )
            
            # Собираем текст из сегментов
            text = " ".join([segment.text.strip() for segment in segments])
            return text
            
        except Exception as e:
            logger.error(f"❌ Transcription failed for {audio_path}: {e}")
            return "[Ошибка транскрибации]"

# Глобальный инстанс для использования в других модулях
stt_service = STTService()