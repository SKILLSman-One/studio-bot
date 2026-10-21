import asyncio
import hashlib
import hmac
import secrets
import time


def hash_password(password: str):
    if not 12 <= len(password) <= 512:
        raise ValueError("Пароль должен содержать от 12 до 512 символов.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def verify_password(password, encoded):
    try:
        scheme, rounds, salt, wanted = encoded.split("$")
        if scheme != "pbkdf2_sha256" or not 100_000 <= int(rounds) <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
        return hmac.compare_digest(actual, wanted)
    except (ValueError, TypeError):
        return False


class AdminAuth:
    def __init__(self, store, settings, clock=time.time):
        self.store, self.settings, self.clock = store, settings, clock
        self.sessions = {}
        self.login_lock = asyncio.Lock()

    def allowed(self, uid):
        return not self.store.is_banned(uid) and (
            not self.settings.admin_ids or uid in self.settings.admin_ids
        )

    def recipients(self):
        return tuple(
            row["user_id"]
            for row in self.store.all(
                "SELECT user_id FROM admin_recipients WHERE password_tag=?", (self.password_tag,)
            )
            if self.allowed(row["user_id"])
        )

    @property
    def password_tag(self):
        return hashlib.sha256(self.settings.password_hash.encode()).hexdigest()

    def logged_in(self, uid):
        return self.allowed(uid) and self.sessions.get(uid, 0) > self.clock()

    async def login(self, uid, password):
        async with self.login_lock:
            return await self._login(uid, password)

    async def _login(self, uid, password):
        if not self.allowed(uid):
            return False
        row = self.store.one("SELECT * FROM login_attempts WHERE user_id=?", (uid,))
        if row and row["blocked_until"] > self.clock():
            return False
        # A shared limit also bounds attempts spread across multiple Telegram users.
        global_row = self.store.one("SELECT * FROM login_attempts WHERE user_id=0")
        if global_row and global_row["blocked_until"] > self.clock():
            return False
        success = len(password) <= 512 and await asyncio.to_thread(
            verify_password, password, self.settings.password_hash
        )
        if not self.allowed(uid):
            return False
        with self.store.db:
            if success:
                self.store.db.execute("DELETE FROM login_attempts WHERE user_id=?", (uid,))
                self.sessions[uid] = self.clock() + 3600
                self.store.db.execute(
                    "INSERT OR REPLACE INTO admin_recipients VALUES (?,?)", (uid, self.password_tag)
                )
            else:
                count = (
                    row["failures"] + 1 if row and self.clock() - row["last_attempt"] < 900 else 1
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO login_attempts VALUES (?,?,?,?)",
                    (uid, count, self.clock(), self.clock() + 900 if count >= 5 else 0),
                )
                total = (
                    global_row["failures"] + 1
                    if global_row and self.clock() - global_row["last_attempt"] < 900
                    else 1
                )
                self.store.db.execute(
                    "INSERT OR REPLACE INTO login_attempts VALUES (0,?,?,?)",
                    (total, self.clock(), self.clock() + 900 if total >= 25 else 0),
                )
        return success

    def logout(self, uid):
        self.sessions.pop(uid, None)
