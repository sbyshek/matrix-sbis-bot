import json
import os
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, List

class ConnectionStateManager:
    def __init__(self, state_file: str = '/app/data/connection_state.json'):
        self.state_file = state_file
        # Separate file for pending users to persist across restarts
        self.pending_file = os.path.join(os.path.dirname(self.state_file), 'pending_users.json')
        self.lock = threading.Lock()
        self._ensure_file()
        self._ensure_pending_file()
    
    def _ensure_file(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        if not os.path.exists(self.state_file):
            self._write({'active': {}, 'history': [], 'pending': []})  # ✅ Пустой словарь

    def _ensure_pending_file(self):
        try:
            dirp = os.path.dirname(self.pending_file)
            os.makedirs(dirp, exist_ok=True)
            if not os.path.exists(self.pending_file):
                with open(self.pending_file, 'w') as f:
                    json.dump([], f, indent=2)
        except Exception:
            pass
    
    def _read(self) -> Dict:
        try:
            with open(self.state_file, 'r') as f:
                return json.load(f)
        except Exception:
            return {'active': {}, 'history': [], 'pending': []}

    # helpers for pending file (persisted separately)
    def _read_pending_file(self) -> list:
        try:
            with open(self.pending_file, 'r') as f:
                return json.load(f)
        except Exception:
            return []

    def _write_pending_file(self, data: list):
        try:
            with open(self.pending_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)
        except Exception:
            pass
    
    def _write(self, data: Dict):
        with self.lock:
            with open(self.state_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)
    
    def is_user_connected(self, user_login: str) -> bool:
        """Проверить подключен ли конкретный пользователь"""
        state = self._read()
        # active = state.get('active', {})
        active = state.get('active') or {}
        
        if user_login not in active:
            return False
        
        expires = datetime.fromisoformat(active[user_login]['expires_at'])
        if datetime.now() > expires:
            self.release(user_login)
            return False
        
        return True
    
    def get_user_connections(self) -> Dict[str, Dict]:
        """Получить все активные подключения"""
        state = self._read()
        # active = state.get('active', {}).copy()
        active = state.get('active') or {}
        
        # Очищаем истекшие
        for user in list(active.keys()):
            expires = datetime.fromisoformat(active[user]['expires_at'])
            if datetime.now() > expires:
                del active[user]
                self.release(user)
        
        return active
    
    def get_user_status(self, user_login: str) -> Optional[Dict]:
        """Получить статус конкретного пользователя"""
        # if not self.is_user_connected(user_login):
        #     return None
        # return self._read()['active'][user_login]
        
        if not self.is_user_connected(user_login):
            return None
        state = self._read()
        # ✅ Исправление
        active = state.get('active') or {}
        return active.get(user_login)
        
    
    def reserve(self, user_login: str, computer_ip: str, duration_minutes: int = 15) -> bool:
        """Зарезервировать доступ для пользователя"""
        if self.is_user_connected(user_login):  # ✅ Проверяем конкретного
            return False
        
        state = self._read()
        if state.get('active') is None:
            state['active'] = {}
        
        state['active'][user_login] = {
            'computer_ip': computer_ip,
            'reserved_at': datetime.now().isoformat(),
            'expires_at': (datetime.now() + timedelta(minutes=duration_minutes)).isoformat(),
            'duration_minutes': duration_minutes
        }
        self._write(state)
        return True
    
    def release(self, user_login: str) -> bool:
        """Освободить доступ пользователя"""
        state = self._read()
        active = state.get('active') or {}
        
        if user_login not in state['active']:
            return False
        
        # Добавляем в историю
        state['history'].append({
            **state['active'][user_login],
            'user_login': user_login,
            'released_at': datetime.now().isoformat()
        })
        
        # state['history'] = state['history'][-50:]
        # del state['active'][user_login]
        # self._write(state)
        # return True
        del active[user_login]
        state['active'] = active  # ✅ Сохраняем обновленный active
        
        # Ограничиваем историю
        state['history'] = state['history'][-50:]
        self._write(state)
        return True
        
    
    def force_release(self, target_login: str) -> bool:
        """Принудительно отключить пользователя (для админов)"""
        return self.release(target_login)

    # Pending users management
    def add_pending(self, user_login: str, reason: str = '', raw: Dict = None) -> bool:
        """Add or update a pending user record (admin should review)."""
        pending = self._read_pending_file() or []
        # remove existing entry for user if present
        pending = [p for p in pending if p.get('user_login') != user_login]

        entry = {
            'user_login': user_login,
            'reason': reason,
            'added_at': datetime.now().isoformat(),
        }
        if raw:
            # store a compact preview of attributes if provided
            try:
                entry['preview'] = {
                    'dn': raw.get('dn'),
                    'attributes': {k: raw.get('attributes', {}).get(k) for k in list(raw.get('attributes', {}).keys())[:12]}
                }
            except Exception:
                entry['preview'] = None

        pending.append(entry)
        # keep reasonable size
        pending = pending[-500:]
        self._write_pending_file(pending)
        return True

    def list_pending(self) -> list:
        return self._read_pending_file() or []

    def clear_pending(self) -> bool:
        self._write_pending_file([])
        return True

    def remove_pending(self, user_login: str) -> bool:
        pending = self._read_pending_file() or []
        new = [p for p in pending if p.get('user_login') != user_login]
        if len(new) == len(pending):
            return False
        self._write_pending_file(new)
        return True