import os
import json
import logging
import asyncio
from typing import Any, Dict, List, Optional, Set
from datetime import datetime
import httpx
from aiogram.fsm.storage.base import BaseStorage, StorageKey, StateType

logger = logging.getLogger("supabase_db")

def _load_env_fallback():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k not in os.environ:
                        os.environ[k] = v

_load_env_fallback()

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://pantkdwqeviiuosgptcs.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
DB_LOCAL_BACKUP = os.path.join(os.path.dirname(__file__), "database.json")

def _get_headers() -> Dict[str, str]:
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }


class MoviesDict(dict):
    def __init__(self, sdb: "SupabaseDB"):
        super().__init__()
        self.sdb = sdb

    def __setitem__(self, code: str, value: Any):
        super().__setitem__(str(code), value)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.sdb.save_movie(str(code), value, self.sdb.movie_channel_id))
        except RuntimeError:
            pass

    def __delitem__(self, code: str):
        super().__delitem__(str(code))
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.sdb.delete_movie(str(code)))
        except RuntimeError:
            pass


class ChannelsList(list):
    def __init__(self, sdb: "SupabaseDB"):
        super().__init__()
        self.sdb = sdb

    def append(self, ch: dict):
        # Prevent duplicates
        for i, existing in enumerate(list(self)):
            if str(existing.get("chat_id")) == str(ch.get("chat_id")):
                self.pop(i)
                break
        super().append(ch)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.sdb.add_channel(int(ch["chat_id"]), ch["link"], ch["name"]))
        except RuntimeError:
            pass

    def pop(self, index: int = -1):
        ch = super().pop(index)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.sdb.delete_channel(int(ch["chat_id"])))
        except RuntimeError:
            pass
        return ch


class SupabaseDB:
    def __init__(self):
        self.url = SUPABASE_URL.rstrip("/")
        self.headers = _get_headers()
        self.channels = ChannelsList(self)
        self.movies = MoviesDict(self)
        self.vip_users: Dict[str, Dict[str, Any]] = {}
        self.users: Dict[str, Dict[str, Any]] = {}
        self.join_requests_sets: Dict[str, Set[int]] = {}
        self.movie_channel_id: Optional[int] = -1003726206742
        self.movie_channel_link: str = "https://t.me/+a4v9DzCqZMIzMDZi"
        self._initialized = False

    async def load_all(self):
        """Loads all base data from Supabase into memory."""
        async with httpx.AsyncClient(timeout=25.0) as client:
            # 1. Settings
            try:
                r = await client.get(f"{self.url}/rest/v1/bot_settings?select=key,value", headers=self.headers)
                if r.status_code == 200:
                    for row in r.json():
                        if row["key"] == "movie_channel_id" and row["value"]:
                            try:
                                self.movie_channel_id = int(row["value"])
                            except ValueError:
                                self.movie_channel_id = row["value"]
                        elif row["key"] == "movie_channel_link" and row["value"]:
                            self.movie_channel_link = row["value"]
            except Exception as e:
                logger.error(f"Error loading settings: {e}")

            # 2. Channels
            try:
                r = await client.get(f"{self.url}/rest/v1/bot_channels?select=chat_id,link,name", headers=self.headers)
                if r.status_code == 200:
                    list.clear(self.channels)
                    for item in r.json():
                        list.append(self.channels, item)
            except Exception as e:
                logger.error(f"Error loading channels: {e}")

            # 3. Movies
            try:
                r = await client.get(f"{self.url}/rest/v1/bot_movies?select=code,channel_id,message_id,file_id,caption,media_type", headers=self.headers)
                if r.status_code == 200:
                    dict.clear(self.movies)
                    for row in r.json():
                        dict.__setitem__(self.movies, str(row["code"]), row)
            except Exception as e:
                logger.error(f"Error loading movies: {e}")

            # 4. VIP Users
            try:
                r = await client.get(f"{self.url}/rest/v1/bot_vip_users?select=user_id,activated_at,payment", headers=self.headers)
                if r.status_code == 200:
                    self.vip_users = {str(row["user_id"]): row for row in r.json()}
            except Exception as e:
                logger.error(f"Error loading vip users: {e}")

            # 5. Join Requests
            try:
                r = await client.get(f"{self.url}/rest/v1/bot_join_requests?select=channel_id,user_id", headers=self.headers)
                if r.status_code == 200:
                    self.join_requests_sets = {}
                    for row in r.json():
                        ch = str(row["channel_id"])
                        if ch not in self.join_requests_sets:
                            self.join_requests_sets[ch] = set()
                        self.join_requests_sets[ch].add(int(row["user_id"]))
            except Exception as e:
                logger.error(f"Error loading join requests: {e}")

            # 6. Users
            try:
                r = await client.get(f"{self.url}/rest/v1/bot_users?select=*", headers=self.headers)
                if r.status_code == 200:
                    self.users = {str(row["id"]): row for row in r.json()}
            except Exception as e:
                logger.error(f"Error loading users: {e}")

        self._initialized = True
        logger.info(f"Supabase initialized: {len(self.channels)} channels, {len(self.movies)} movies, {len(self.users)} users")
        self.save_local_backup()

    def save_local_backup(self):
        try:
            data = {
                "channels": list(self.channels),
                "join_requests": {k: list(v) for k, v in self.join_requests_sets.items()},
                "users": self.users,
                "vip_users": self.vip_users,
                "movie_channel_id": self.movie_channel_id,
                "movie_channel_link": self.movie_channel_link,
                "movies": dict(self.movies),
            }
            tmp = DB_LOCAL_BACKUP + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DB_LOCAL_BACKUP)
        except Exception:
            pass

    async def save_user(self, user: Any, is_message: bool = False):
        if not user:
            return
        uid_str = str(user.id)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        username_formatted = f"@{user.username}" if getattr(user, "username", None) else None
        is_premium = bool(getattr(user, "is_premium", False))

        if uid_str not in self.users:
            self.users[uid_str] = {
                "id": user.id,
                "first_name": getattr(user, "first_name", "") or "",
                "last_name": getattr(user, "last_name", "") or "",
                "username": username_formatted,
                "language_code": getattr(user, "language_code", "uz") or "uz",
                "is_premium": is_premium,
                "first_seen": now_str,
                "last_active": now_str,
                "message_count": 1 if is_message else 0,
            }
        else:
            u = self.users[uid_str]
            u["last_active"] = now_str
            if is_message:
                u["message_count"] = u.get("message_count", 0) + 1
            if getattr(user, "first_name", None):
                u["first_name"] = user.first_name
            if getattr(user, "last_name", None) is not None:
                u["last_name"] = user.last_name or ""
            if username_formatted:
                u["username"] = username_formatted
            if getattr(user, "language_code", None):
                u["language_code"] = user.language_code
            u["is_premium"] = is_premium

        payload = self.users[uid_str]
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.url}/rest/v1/bot_users",
                    json=payload,
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Failed to upsert user {user.id}: {e}")

    async def add_join_request(self, chat_key: str, user_id: int):
        chat_key = str(chat_key)
        if chat_key not in self.join_requests_sets:
            self.join_requests_sets[chat_key] = set()
        self.join_requests_sets[chat_key].add(user_id)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.url}/rest/v1/bot_join_requests",
                    json={"channel_id": chat_key, "user_id": user_id},
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error inserting join request: {e}")

    async def remove_join_request(self, chat_key: str, user_id: int):
        chat_key = str(chat_key)
        if chat_key in self.join_requests_sets:
            self.join_requests_sets[chat_key].discard(user_id)

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{self.url}/rest/v1/bot_join_requests?channel_id=eq.{chat_key}&user_id=eq.{user_id}",
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error deleting join request: {e}")

    async def add_channel(self, chat_id: int, link: str, name: str):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.url}/rest/v1/bot_channels",
                    json={"chat_id": chat_id, "link": link, "name": name},
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error adding channel {chat_id}: {e}")
        self.save_local_backup()

    async def delete_channel(self, chat_id: int):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{self.url}/rest/v1/bot_channels?chat_id=eq.{chat_id}",
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error deleting channel {chat_id}: {e}")
        self.save_local_backup()

    async def save_movie(self, code: str, movie_data: dict, movie_channel_id: Optional[int] = None):
        code_str = str(code)
        if movie_channel_id:
            self.movie_channel_id = movie_channel_id

        payload = {
            "code": code_str,
            "channel_id": movie_data.get("channel_id"),
            "message_id": movie_data.get("message_id"),
            "file_id": movie_data.get("file_id"),
            "caption": movie_data.get("caption"),
            "media_type": movie_data.get("media_type", "video"),
        }
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.url}/rest/v1/bot_movies",
                    json=payload,
                    headers=self.headers,
                )
                if movie_channel_id:
                    await client.post(
                        f"{self.url}/rest/v1/bot_settings",
                        json={"key": "movie_channel_id", "value": str(movie_channel_id)},
                        headers=self.headers,
                    )
        except Exception as e:
            logger.error(f"Error saving movie #{code}: {e}")
        self.save_local_backup()

    async def delete_movie(self, code: str):
        code_str = str(code)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.delete(
                    f"{self.url}/rest/v1/bot_movies?code=eq.{code_str}",
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error deleting movie #{code}: {e}")
        self.save_local_backup()

    async def set_vip_user(self, user_id: int, payment_info: Optional[dict] = None):
        uid = str(user_id)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.vip_users[uid] = {
            "user_id": user_id,
            "activated_at": now_str,
            "payment": payment_info or {},
        }
        if uid in self.users:
            self.users[uid]["is_vip"] = True
            self.users[uid]["vip_since"] = now_str

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.url}/rest/v1/bot_vip_users",
                    json={
                        "user_id": user_id,
                        "activated_at": now_str,
                        "payment": payment_info or {},
                    },
                    headers=self.headers,
                )
                await client.post(
                    f"{self.url}/rest/v1/bot_users",
                    json={"id": user_id, "is_vip": True, "vip_since": now_str},
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error setting VIP user {user_id}: {e}")
        self.save_local_backup()

    async def set_setting(self, key: str, value: str):
        if key == "movie_channel_id":
            try:
                self.movie_channel_id = int(value)
            except ValueError:
                self.movie_channel_id = value
        elif key == "movie_channel_link":
            self.movie_channel_link = value

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.url}/rest/v1/bot_settings",
                    json={"key": key, "value": str(value)},
                    headers=self.headers,
                )
        except Exception as e:
            logger.error(f"Error setting setting {key}: {e}")

    def is_vip(self, user_id: int, admin_ids: List[int]) -> bool:
        if user_id in admin_ids:
            return True
        uid = str(user_id)
        if uid in self.vip_users:
            return True
        if self.users.get(uid, {}).get("is_vip"):
            return True
        return False


# ================= DICT-COMPATIBLE PROXY =================
class DBProxy:
    def __init__(self, sdb: SupabaseDB):
        self._sdb = sdb

    def __getitem__(self, key: str):
        if key == "channels":
            return self._sdb.channels
        if key == "movies":
            return self._sdb.movies
        if key == "vip_users":
            return self._sdb.vip_users
        if key == "users":
            return self._sdb.users
        if key == "movie_channel_id":
            return self._sdb.movie_channel_id
        if key == "movie_channel_link":
            return self._sdb.movie_channel_link
        if key == "join_requests":
            return {k: list(v) for k, v in self._sdb.join_requests_sets.items()}
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any):
        if key == "channels":
            list.clear(self._sdb.channels)
            for c in value:
                list.append(self._sdb.channels, c)
        elif key == "movies":
            dict.clear(self._sdb.movies)
            for k, v in value.items():
                dict.__setitem__(self._sdb.movies, str(k), v)
        elif key == "vip_users":
            self._sdb.vip_users = value
        elif key == "users":
            self._sdb.users = value
        elif key == "movie_channel_id":
            self._sdb.movie_channel_id = value
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._sdb.set_setting("movie_channel_id", str(value)))
            except RuntimeError:
                pass
        elif key == "movie_channel_link":
            self._sdb.movie_channel_link = value
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._sdb.set_setting("movie_channel_link", str(value)))
            except RuntimeError:
                pass

    def get(self, key: str, default: Any = None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: str):
        return key in ("channels", "movies", "vip_users", "users", "movie_channel_id", "movie_channel_link", "join_requests")


# ================= FSM STORAGE FOR SUPABASE =================
class SupabaseFSMStorage(BaseStorage):
    """Persistent FSM storage stored in Supabase bot_fsm table."""
    def __init__(self, sdb: SupabaseDB):
        self.sdb = sdb
        self._state_cache: Dict[tuple, Optional[str]] = {}
        self._data_cache: Dict[tuple, Dict[str, Any]] = {}

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        if state is None:
            state_str = None
        elif hasattr(state, "state"):
            state_str = state.state
        else:
            state_str = str(state)

        cache_k = (key.chat_id, key.user_id)
        self._state_cache[cache_k] = state_str

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.sdb.url}/rest/v1/bot_fsm",
                    json={
                        "chat_id": key.chat_id,
                        "user_id": key.user_id,
                        "state": state_str,
                    },
                    headers=self.sdb.headers,
                )
        except Exception as e:
            logger.error(f"FSM set_state error: {e}")

    async def get_state(self, key: StorageKey) -> Optional[str]:
        cache_k = (key.chat_id, key.user_id)
        if cache_k in self._state_cache:
            return self._state_cache[cache_k]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{self.sdb.url}/rest/v1/bot_fsm?chat_id=eq.{key.chat_id}&user_id=eq.{key.user_id}&select=state",
                    headers=self.sdb.headers,
                )
                if r.status_code == 200:
                    rows = r.json()
                    if rows:
                        st = rows[0].get("state")
                        self._state_cache[cache_k] = st
                        return st
        except Exception as e:
            logger.error(f"FSM get_state error: {e}")
        return None

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        cache_k = (key.chat_id, key.user_id)
        self._data_cache[cache_k] = data or {}

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{self.sdb.url}/rest/v1/bot_fsm",
                    json={
                        "chat_id": key.chat_id,
                        "user_id": key.user_id,
                        "data": data,
                    },
                    headers=self.sdb.headers,
                )
        except Exception as e:
            logger.error(f"FSM set_data error: {e}")

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        cache_k = (key.chat_id, key.user_id)
        if cache_k in self._data_cache:
            return self._data_cache[cache_k]

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{self.sdb.url}/rest/v1/bot_fsm?chat_id=eq.{key.chat_id}&user_id=eq.{key.user_id}&select=data",
                    headers=self.sdb.headers,
                )
                if r.status_code == 200:
                    rows = r.json()
                    if rows and rows[0].get("data") is not None:
                        d = rows[0]["data"]
                        self._data_cache[cache_k] = d
                        return d
        except Exception as e:
            logger.error(f"FSM get_data error: {e}")
        return {}

    async def close(self) -> None:
        pass
