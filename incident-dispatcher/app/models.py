from pydantic import BaseModel, Field
from typing import Optional, List


# --- Pydantic модели ---
class EventCreate(BaseModel):
    type: str
    comment: str = ""
    user_id: Optional[str] = None
    room_id: Optional[str] = None
    source: str = "api"

class EventClose(BaseModel):
    event_id: str
    
    
class AsteriskTriggerRequest(BaseModel):
    loc_id: str
    caller_id: str
    comment: str
    source: str = "asterisk_voice"
    
class VoiceTriggerRequest(BaseModel):
    exten: str
    caller_id: str
    comment: str
    initiator: str = "Неизвестно"
    
    
class AlertTriggerRequest(BaseModel):
    template_id: str
    loc_id: str
    
    
class BulkAlertTriggerRequest(BaseModel):
    template_id: str
    loc_ids: List[str]  # Список ID локаций
    
class DeskAlertTriggerRequest(BaseModel):
    template_id: str