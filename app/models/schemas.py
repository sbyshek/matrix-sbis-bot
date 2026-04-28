from enum import StrEnum
from pydantic import BaseModel, Field, PrivateAttr
from typing import Optional

class ExecutionStatus(StrEnum):
    OK = 'ok'
    ERROR = 'error'
    UNDEFINED = 'undefined'

class AvaliableCommands(StrEnum):
    PING = 'ping'
    HELP = 'help'
    STATUS = 'status'
    CONNECT = 'connect'
    DISCONNECT = 'disconnect'
    ADMIN_DISCONNECT = 'admin_disconnect'
    CHECK_USER = 'check_user'
    NOTIFY_USER = 'notify_user'
    HISTORY = 'history'
    LOG = 'log'
    PENDING_CLEAR = 'pending_clear'
    RULE_ENABLE = 'rule_enable'
    RULE_DISABLE = 'rule_disable'
    PENDING_LIST = 'pending_list'
    RESTART_SERVICE = 'restart_service'

class CommandResult(BaseModel):
    command: AvaliableCommands = Field(default=AvaliableCommands.HELP)
    sender: Optional[str] = None
    description: Optional[str] = None
    computer_name: Optional[str] = None
    status: ExecutionStatus = ExecutionStatus.UNDEFINED
    client_ip: Optional[str] = None
    _state_manager: Optional[object] = PrivateAttr(default=None)
    _logger: Optional[object] = PrivateAttr(default=None)
    _ldap_raw: Optional[object] = PrivateAttr(default=None)