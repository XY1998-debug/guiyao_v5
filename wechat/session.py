"""企业微信会话管理 — 多用户多轮对话上下文"""

import time
import threading
from typing import Optional


class Session:
    """单个用户的会话上下文"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self.created_at = time.time()
        self.last_active = time.time()
        self.message_count = 0

    def touch(self):
        self.last_active = time.time()
        self.message_count += 1

    def is_expired(self, ttl: int = 1800) -> bool:
        """30分钟无活动则过期"""
        return time.time() - self.last_active > ttl


class SessionManager:
    """会话管理器"""

    def __init__(self, ttl: int = 1800, max_sessions: int = 100):
        self._sessions: dict[str, Session] = {}
        self._ttl = ttl
        self._max = max_sessions
        self._lock = threading.Lock()

    def get_or_create(self, user_id: str) -> Session:
        with self._lock:
            # 清理过期
            expired = [k for k, v in self._sessions.items() if v.is_expired(self._ttl)]
            for k in expired:
                del self._sessions[k]

            # 已有会话
            if user_id in self._sessions:
                session = self._sessions[user_id]
                session.touch()
                return session

            # 超限清理最旧的
            if len(self._sessions) >= self._max:
                oldest = min(self._sessions.values(), key=lambda s: s.last_active)
                del self._sessions[oldest.user_id]

            session = Session(user_id)
            self._sessions[user_id] = session
            return session

    def remove(self, user_id: str):
        with self._lock:
            self._sessions.pop(user_id, None)

    @property

    def active_count(self) -> int:
        return len(self._sessions)


# 全局实例
session_manager = SessionManager()
