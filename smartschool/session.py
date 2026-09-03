"""Token storage and rotation.

The old flow cached a token with a 23-hour time-based guess and a
validate_token() that always returned True, so an expired token was never
detected - it just produced empty homework runs. Here the token is treated as
a rotating credential: persisted after every rotation, and reported as dead
only when the server actually rejects it.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from .exceptions import TokenMissing
from .models import TokenState


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TokenStore:
    """Reads/writes the active webToken.

    Two inputs, by design:
      * `token_file` (config/token.txt) - where the user pastes a fresh token
      * `cache_file` (config/token_cache.json) - where we persist rotations

    A token.txt value we have not seen before always wins over the cache, so
    refreshing by hand is just "paste and go". Once ingested, rotation carries
    the cache ahead of the file without the file pulling it back.
    """

    def __init__(self, config_dir: Path):
        self.config_dir = Path(config_dir)
        self.token_file = self.config_dir / "token.txt"
        self.cache_file = self.config_dir / "token_cache.json"

    # ------------------------------------------------------------------
    def load(self) -> TokenState:
        """Return the token to use.

        A token.txt value we have not ingested before always wins, so replacing
        an expired token is just "write the file". Otherwise the cache wins,
        because rotation moves it *ahead* of the file - comparing the file
        against cached.token instead would keep resurrecting the stale paste.
        """
        pasted, pasted_expiry = self._read_token_and_expiry()
        cached = self._read_cache()

        is_new_paste = bool(pasted) and (cached is None or pasted != cached.pasted_token)

        if is_new_paste:
            if pasted_expiry:
                logger.info(f"Ingesting a new token from token.txt (expires {pasted_expiry})")
            else:
                logger.info("Ingesting a new token from token.txt (no expiry given)")
            state = TokenState(
                token=pasted,
                obtained_at=_now_iso(),
                expires_at=pasted_expiry,
                pasted_token=pasted,
            )
            self.save(state)
            return state

        if cached and cached.token:
            logger.info(
                f"Using cached token (rotated {cached.rotated_count}x, obtained {cached.obtained_at})"
            )
            return cached

        raise TokenMissing(
            f"No webToken available. Paste one into {self.token_file} "
            "(browser DevTools -> Application -> Cookies -> webToken)."
        )

    def save(self, state: TokenState) -> None:
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(state.as_dict(), f, indent=2)
        except OSError as e:
            logger.error(f"Failed to persist token cache: {e}")

    def save_renewed(self, new_token: str) -> TokenState:
        """Persist a token minted by bioLogin renewal.

        A renewed token starts a fresh lifetime, so obtained_at resets. The
        pasted_token marker is set to whatever token.txt currently holds, so a
        later reload does NOT mistake the now-stale file for a fresh paste and
        revert to the dead token it contains. A genuinely new hand-paste (file
        content changes) still overrides, as intended.
        """
        current_file_token, _ = self._read_token_and_expiry()
        state = TokenState(
            token=new_token,
            obtained_at=_now_iso(),
            pasted_token=current_file_token,
        )
        self.save(state)
        logger.info("Persisted a bioLogin-renewed token")
        return state

    def record_rotation(self, state: TokenState, new_token: str, expires: Optional[str]) -> TokenState:
        """Persist a server-rotated token so a restart keeps the live session."""
        updated = TokenState(
            token=new_token,
            obtained_at=_now_iso(),
            expires_at=expires or state.expires_at,
            rotated_count=state.rotated_count + 1,
            pasted_token=state.pasted_token,
        )
        self.save(updated)
        logger.info(f"Persisted rotated token (rotation #{updated.rotated_count})")
        return updated

    # ------------------------------------------------------------------
    def _read_token_file(self) -> Optional[str]:
        return self._read_token_and_expiry()[0]

    def _read_token_and_expiry(self):
        """Read token.txt. Line 1 is the token; an optional line 2 is its
        expiry (the cookie's Expires, ISO-8601).

        A remember-me webToken lasts ~6 months; a plain one lasts 9 hours, and
        nothing in the token string reveals which. Recording the real Expires
        lets the monitor predict correctly instead of falsely warning after 9h.
        """
        if not self.token_file.exists():
            return None, None
        try:
            lines = [
                ln.strip()
                for ln in self.token_file.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            ]
        except OSError as e:
            logger.error(f"Failed to read {self.token_file}: {e}")
            return None, None
        if not lines:
            return None, None
        token = lines[0]
        expiry = None
        if len(lines) > 1:
            raw = lines[1]
            try:
                # Accept the browser's trailing-Z form and plain ISO.
                datetime.fromisoformat(raw.replace("Z", "+00:00"))
                expiry = raw.replace("Z", "+00:00")
            except ValueError:
                logger.warning(f"Ignoring unparseable expiry on line 2 of token.txt: {raw!r}")
        return token, expiry

    def _read_cache(self) -> Optional[TokenState]:
        if not self.cache_file.exists():
            return None
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            logger.warning(f"Token cache unreadable ({e}); ignoring it")
            return None

        # Tolerate the legacy shape: {"<username>": {"token": ..., ...}} and the
        # variant where that inner dict was stored as a repr string.
        if isinstance(data, dict) and "token" not in data:
            for value in data.values():
                if isinstance(value, str):
                    try:
                        import ast

                        value = ast.literal_eval(value)
                    except (ValueError, SyntaxError):
                        continue
                if isinstance(value, dict) and value.get("token"):
                    logger.info("Migrated token from legacy per-username cache format")
                    return TokenState(
                        token=value["token"],
                        obtained_at=value.get("timestamp", ""),
                        pasted_token=value["token"],
                    )
            return None

        if isinstance(data, dict) and data.get("token"):
            return TokenState(
                token=data["token"],
                obtained_at=data.get("obtained_at", ""),
                expires_at=data.get("expires_at"),
                rotated_count=int(data.get("rotated_count", 0) or 0),
                pasted_token=data.get("pasted_token"),
            )
        return None
