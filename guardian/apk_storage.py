"""SQLite persistence for the APK-compatible `/v1` contract.

Kept separate from `storage.py` so the original prototype schema and the
phone-facing schema can evolve independently while sharing one database file.
"""
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from . import outbox


def digest(value) -> str:
    import hashlib
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


ROLES = ("wearer", "caregiver", "legacy")


def _role(value: str) -> str:
    if value not in ROLES:
        raise ValueError("Role must be wearer, caregiver or legacy")
    return value


class ApkStorage:
    def __init__(self, path: str, credential_ttl_days: int = 30):
        if not 1 <= credential_ttl_days <= 365:
            raise ValueError("Credential lifetime must be 1-365 days")
        self.path = path
        self.credential_ttl_days = credential_ttl_days
        self.lock = threading.RLock()
        self._family_id = None
        self._setup()

    @contextmanager
    def _db(self):
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _setup(self):
        with self.lock, self._db() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS apk_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS apk_credentials (
                    id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                    guardian_id TEXT NOT NULL, family_id TEXT NOT NULL,
                    name TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT, revoked INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL DEFAULT 'legacy');
                CREATE TABLE IF NOT EXISTS apk_pairing_codes (
                    id TEXT PRIMARY KEY, code_hash TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, consumed_at TEXT,
                    role TEXT NOT NULL DEFAULT 'legacy');
                CREATE TABLE IF NOT EXISTS apk_consents (
                    guardian_id TEXT NOT NULL, scope TEXT NOT NULL,
                    granted INTEGER NOT NULL, version TEXT NOT NULL,
                    updated TEXT NOT NULL, PRIMARY KEY (guardian_id, scope));
                CREATE TABLE IF NOT EXISTS apk_devices (
                    id TEXT PRIMARY KEY, family_id TEXT NOT NULL,
                    local_id TEXT NOT NULL, model TEXT NOT NULL,
                    firmware TEXT, protocol TEXT, created_at TEXT NOT NULL,
                    UNIQUE (family_id, local_id));
                CREATE TABLE IF NOT EXISTS apk_batches (
                    device_id TEXT NOT NULL, batch_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL, receipt_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY (device_id, batch_id));
                CREATE TABLE IF NOT EXISTS apk_samples (
                    device_id TEXT NOT NULL, sample_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL, metric TEXT NOT NULL,
                    value REAL NOT NULL, unit TEXT NOT NULL,
                    observed_at TEXT NOT NULL, phone_received_at TEXT NOT NULL,
                    stored_at TEXT NOT NULL, source TEXT NOT NULL,
                    quality TEXT NOT NULL, time_basis TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    PRIMARY KEY (device_id, sample_id));
                CREATE INDEX IF NOT EXISTS ix_apk_samples_window
                    ON apk_samples(device_id, observed_at, sample_id);
                CREATE TABLE IF NOT EXISTS apk_observations (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    guardian_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
                    text TEXT NOT NULL, recorded_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS apk_alerts (
                    id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                    creator TEXT NOT NULL, kind TEXT NOT NULL,
                    text TEXT NOT NULL, evidence_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS apk_deliveries (
                    alert_id TEXT NOT NULL, guardian_id TEXT NOT NULL,
                    received_at TEXT, confirmed_at TEXT, note TEXT,
                    PRIMARY KEY (alert_id, guardian_id));
                CREATE TABLE IF NOT EXISTS apk_conversations (
                    id TEXT PRIMARY KEY, family_id TEXT NOT NULL,
                    guardian_id TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS apk_ai_requests (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                    device_id TEXT NOT NULL, guardian_id TEXT NOT NULL,
                    question TEXT NOT NULL, context_json TEXT NOT NULL,
                    context_hash TEXT NOT NULL, status TEXT NOT NULL,
                    answer TEXT, error TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS apk_config_versions (
                    device_id TEXT NOT NULL, version INTEGER NOT NULL,
                    guardian_id TEXT NOT NULL, parameters_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, PRIMARY KEY (device_id, version));
                CREATE TABLE IF NOT EXISTS apk_config_acks (
                    device_id TEXT NOT NULL, guardian_id TEXT NOT NULL,
                    version INTEGER NOT NULL, status TEXT NOT NULL,
                    reason TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY (device_id, guardian_id, version));
            """)
            # The APK batch and its Agent-adaptation job commit together.
            outbox.ensure_schema(con)
            # Existing databases predate credential expiry; backfill instead of
            # dropping credentials on upgrade.
            columns = {row["name"] for row in con.execute("PRAGMA table_info(apk_credentials)")}
            if "expires_at" not in columns:
                try:
                    con.execute("ALTER TABLE apk_credentials ADD COLUMN expires_at TEXT")
                except sqlite3.OperationalError:
                    # Another process completed the same upgrade between the
                    # PRAGMA and ALTER; the column now exists.
                    pass
            # Roles were added with the client-app phase. Existing credentials
            # keep full backward-compatible permissions; new pairing codes must
            # opt into wearer or caregiver explicitly.
            for table in ("apk_credentials", "apk_pairing_codes"):
                columns = {row["name"] for row in con.execute(f"PRAGMA table_info({table})")}
                if "role" not in columns:
                    try:
                        con.execute(
                            f"ALTER TABLE {table} ADD COLUMN role TEXT NOT NULL DEFAULT 'legacy'")
                    except sqlite3.OperationalError:
                        pass
            for row in con.execute(
                    "SELECT id, created_at FROM apk_credentials WHERE expires_at IS NULL").fetchall():
                try:
                    created = datetime.fromisoformat(row["created_at"])
                except ValueError:
                    continue
                con.execute(
                    "UPDATE apk_credentials SET expires_at=? WHERE id=?",
                    ((created + timedelta(days=self.credential_ttl_days)).isoformat(), row["id"]),
                )

    # -- identity ---------------------------------------------------------
    def family_id(self) -> str:
        with self.lock:
            if self._family_id:
                return self._family_id
            with self._db() as con:
                row = con.execute("SELECT value FROM apk_meta WHERE key='family_id'").fetchone()
                if not row:
                    con.execute("INSERT OR IGNORE INTO apk_meta(key,value) VALUES('family_id',?)",
                                (str(uuid4()),))
                    row = con.execute(
                        "SELECT value FROM apk_meta WHERE key='family_id'").fetchone()
                self._family_id = row["value"]
            return self._family_id

    def issue_pairing_code(self, name: str, ttl_minutes: int = 10,
                           code: str | None = None, role: str = "legacy") -> dict:
        """Create a single-use pairing code; only its hash is stored."""
        role = _role(role)
        if not 1 <= ttl_minutes <= 60:
            raise ValueError("Pairing code lifetime must be 1-60 minutes")
        if code is not None and not 12 <= len(code) <= 100:
            raise ValueError("Pairing code must be 12-100 characters")
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(minutes=ttl_minutes)
        with self.lock, self._db() as con:
            con.execute("DELETE FROM apk_pairing_codes WHERE expires_at<=?",
                        (now.isoformat(),))
            for _ in range(5):
                plaintext = code or secrets.token_urlsafe(9)  # 12 URL-safe characters
                try:
                    con.execute(
                        "INSERT INTO apk_pairing_codes(id,code_hash,name,created_at,expires_at,role) "
                        "VALUES(?,?,?,?,?,?)",
                        (str(uuid4()), digest(plaintext), name,
                         now.isoformat(), expires_at.isoformat(), role),
                    )
                except sqlite3.IntegrityError:
                    if not code:
                        continue
                    # A configured bootstrap code is re-issued on restart: refresh
                    # its lifetime and make it usable again.
                    con.execute(
                        "UPDATE apk_pairing_codes SET name=?,created_at=?,expires_at=?,"
                        "consumed_at=NULL,role=? WHERE code_hash=?",
                        (name, now.isoformat(), expires_at.isoformat(), role,
                         digest(plaintext)),
                    )
                return {"code": plaintext, "name": name,
                        "expires_at": expires_at.isoformat(), "role": role}
        raise RuntimeError("Could not allocate a unique pairing code")

    def claim_pairing_code(self, code_hash: str):
        """Consume a valid code atomically; reused or expired codes return None."""
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_pairing_codes WHERE code_hash=? AND consumed_at IS NULL "
                "AND expires_at>?", (code_hash, now),
            ).fetchone()
            if not row:
                return None
            updated = con.execute(
                "UPDATE apk_pairing_codes SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (now, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            return dict(row)

    # -- credentials / consents ------------------------------------------
    def create_credential(self, token_hash: str, name: str, role: str = "legacy") -> dict:
        role = _role(role)
        guardian_id = str(uuid4())
        family_id = self.family_id()
        now = datetime.now(timezone.utc)
        credential = {
            "id": str(uuid4()), "guardian_id": guardian_id,
            "family_id": family_id, "name": name, "role": role,
            "expires_at": (now + timedelta(days=self.credential_ttl_days)).isoformat(),
        }
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_credentials(id,token_hash,guardian_id,family_id,name,created_at,"
                "expires_at,revoked,role) VALUES(?,?,?,?,?,?,?,0,?)",
                (credential["id"], token_hash, guardian_id, family_id, name,
                 now.isoformat(), credential["expires_at"], role),
            )
        return credential

    def credential_by_token(self, token_hash: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_credentials WHERE token_hash=? AND revoked=0", (token_hash,)
            ).fetchone()
            if not row:
                return None
            if row["expires_at"] and datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
                return None
            return dict(row)

    def revoke_credential(self, credential_id: str) -> bool:
        with self.lock, self._db() as con:
            updated = con.execute(
                "UPDATE apk_credentials SET revoked=1 WHERE id=?", (credential_id,))
            return updated.rowcount > 0

    def list_credentials(self) -> list[dict]:
        """Operator view for the local CLI; token hashes are never returned."""
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT id,guardian_id,family_id,name,created_at,expires_at,revoked,role "
                "FROM apk_credentials ORDER BY created_at").fetchall()
            return [dict(row) for row in rows]

    def family_credentials(self, family_id: str) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT * FROM apk_credentials WHERE family_id=? AND revoked=0", (family_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def set_consent(self, guardian_id: str, scope: str, granted: bool, version: str):
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_consents(guardian_id,scope,granted,version,updated) "
                "VALUES(?,?,?,?,?) ON CONFLICT(guardian_id,scope) DO UPDATE SET "
                "granted=excluded.granted,version=excluded.version,updated=excluded.updated",
                (guardian_id, scope, int(granted), version, datetime.now(timezone.utc).isoformat()),
            )

    def consents(self, guardian_id: str) -> dict:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT scope,granted FROM apk_consents WHERE guardian_id=?", (guardian_id,)
            ).fetchall()
            return {row["scope"]: bool(row["granted"]) for row in rows}

    def consent_granted(self, guardian_id: str, scope: str) -> bool:
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT granted FROM apk_consents WHERE guardian_id=? AND scope=?",
                (guardian_id, scope),
            ).fetchone()
            return bool(row and row["granted"])

    # -- devices ----------------------------------------------------------
    def register_device(self, family_id: str, local_id: str, model: str,
                        firmware: str | None, protocol: str) -> str:
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_devices(id,family_id,local_id,model,firmware,protocol,created_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(family_id,local_id) DO UPDATE SET "
                "model=excluded.model,firmware=excluded.firmware,protocol=excluded.protocol",
                (str(uuid4()), family_id, local_id, model, firmware, protocol,
                 datetime.now(timezone.utc).isoformat()),
            )
            row = con.execute(
                "SELECT id FROM apk_devices WHERE family_id=? AND local_id=?",
                (family_id, local_id),
            ).fetchone()
            return row["id"]

    def device(self, device_id: str, family_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_devices WHERE id=? AND family_id=?",
                (device_id, family_id),
            ).fetchone()
            return dict(row) if row else None

    def devices(self, family_id: str) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT * FROM apk_devices WHERE family_id=? ORDER BY created_at", (family_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    # -- measurement batches ---------------------------------------------
    def batch(self, device_id: str, batch_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_batches WHERE device_id=? AND batch_id=?",
                (device_id, batch_id),
            ).fetchone()
            return dict(row) if row else None

    def sample(self, device_id: str, sample_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_samples WHERE device_id=? AND sample_id=?",
                (device_id, sample_id),
            ).fetchone()
            return dict(row) if row else None

    def ingest_batch(self, device_id: str, batch_id: str, content_hash: str,
                     receipt: dict, samples: list[dict], *,
                     job: dict | None = None) -> int | None:
        """Insert an accepted batch atomically; caller has already checked conflicts.

        When ``job`` is supplied, the raw batch/sample rows and the durable
        Agent-adaptation job are inserted in the same transaction.  A retry can
        therefore never observe "raw committed but no adaptation job".
        """
        with self.lock, self._db() as con:
            for data in samples:
                con.execute(
                    "INSERT INTO apk_samples(device_id,sample_id,content_hash,metric,value,unit,"
                    "observed_at,phone_received_at,stored_at,source,quality,time_basis,evidence_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (device_id, data["sample_id"], data["content_hash"], data["metric"],
                     data["value"], data["unit"], data["observed_at"],
                     data["phone_received_at"], data["stored_at"], data["source"],
                     data["quality"], data["time_basis"], data["evidence_json"]),
                )
            con.execute(
                "INSERT INTO apk_batches(device_id,batch_id,content_hash,receipt_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (device_id, batch_id, content_hash, json.dumps(receipt, ensure_ascii=False),
                 receipt["received_at"]),
            )
            if job is not None:
                return outbox.insert_job(
                    con, kind="apk_batch", device_id=device_id,
                    payload=job["payload"], stages=outbox.APK_BATCH_STAGES,
                    batch_id=batch_id,
                    max_attempts=job.get("max_attempts",
                                         outbox.DEFAULT_MAX_ATTEMPTS),
                    now=job.get("created_at"))
            return None

    def ensure_batch_job(self, device_id: str, batch_id: str, payload: dict,
                         *, max_attempts: int = outbox.DEFAULT_MAX_ATTEMPTS,
                         created_at: str | None = None) -> int:
        """Create the adaptation job for a batch committed before the outbox.

        Idempotent: the deterministic ``apk_batch:<device>:<batch>`` key means
        repeated retries reuse the existing row.
        """
        with self.lock, self._db() as con:
            return outbox.insert_job(
                con, kind="apk_batch", device_id=device_id, payload=payload,
                stages=outbox.APK_BATCH_STAGES, batch_id=batch_id,
                max_attempts=max_attempts, now=created_at)

    def samples_in(self, device_id: str, start: datetime, end: datetime) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT * FROM apk_samples WHERE device_id=? AND observed_at>=? AND observed_at<? "
                "ORDER BY observed_at, sample_id",
                (device_id, utc_text(start), utc_text(end)),
            ).fetchall()
            return [dict(row) for row in rows]

    def samples_after(self, device_id: str, start: datetime, end: datetime,
                      after: tuple[str, str], limit: int,
                      metric: str | None = None) -> list[dict]:
        sql = ("SELECT * FROM apk_samples WHERE device_id=? AND observed_at>=? AND observed_at<? "
               "AND (observed_at>? OR (observed_at=? AND sample_id>?))")
        params = [device_id, utc_text(start), utc_text(end), after[0], after[0], after[1]]
        if metric:
            sql += " AND metric=?"
            params.append(metric)
        sql += " ORDER BY observed_at, sample_id LIMIT ?"
        params.append(limit)
        with self.lock, self._db() as con:
            rows = con.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def all_samples(self, device_id: str) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT * FROM apk_samples WHERE device_id=? ORDER BY observed_at, sample_id",
                (device_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    # -- observations -----------------------------------------------------
    def observation(self, observation_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_observations WHERE id=?", (observation_id,)
            ).fetchone()
            return dict(row) if row else None

    def put_observation(self, observation_id: str, device_id: str, guardian_id: str,
                        occurred_at: str, text: str) -> None:
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_observations(id,device_id,guardian_id,occurred_at,text,recorded_at) "
                "VALUES(?,?,?,?,?,?)",
                (observation_id, device_id, guardian_id, occurred_at, text,
                 datetime.now(timezone.utc).isoformat()),
            )

    def observations(self, device_id: str) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT * FROM apk_observations WHERE device_id=? ORDER BY occurred_at DESC LIMIT 200",
                (device_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    # -- alerts -----------------------------------------------------------
    def alert(self, alert_id: str):
        with self.lock, self._db() as con:
            row = con.execute("SELECT * FROM apk_alerts WHERE id=?", (alert_id,)).fetchone()
            return dict(row) if row else None

    def put_alert(self, alert_id: str, device_id: str, creator: str, kind: str,
                  text: str, evidence: dict, content_hash: str, guardians: list[str]) -> None:
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_alerts(id,device_id,creator,kind,text,evidence_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (alert_id, device_id, creator, kind, text,
                 json.dumps(evidence, ensure_ascii=False), content_hash,
                 datetime.now(timezone.utc).isoformat()),
            )
            for guardian_id in guardians:
                con.execute(
                    "INSERT OR IGNORE INTO apk_deliveries(alert_id,guardian_id) VALUES(?,?)",
                    (alert_id, guardian_id),
                )

    def alerts_for(self, family_id: str, guardian_id: str) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT a.*, d.received_at, d.confirmed_at, d.note FROM apk_alerts a "
                "JOIN apk_deliveries d ON d.alert_id=a.id "
                "JOIN apk_devices v ON v.id=a.device_id "
                "WHERE v.family_id=? AND d.guardian_id=? ORDER BY a.created_at DESC LIMIT 100",
                (family_id, guardian_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def delivery(self, alert_id: str, guardian_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_deliveries WHERE alert_id=? AND guardian_id=?",
                (alert_id, guardian_id),
            ).fetchone()
            return dict(row) if row else None

    def receipt(self, alert_id: str, guardian_id: str, status: str, note: str | None) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_deliveries WHERE alert_id=? AND guardian_id=?",
                (alert_id, guardian_id),
            ).fetchone()
            if row is None:
                return False
            received = row["received_at"] or now
            confirmed = row["confirmed_at"]
            if status == "confirmed" and not confirmed:
                confirmed = now
            con.execute(
                "UPDATE apk_deliveries SET received_at=?,confirmed_at=?,note=? "
                "WHERE alert_id=? AND guardian_id=?",
                (received, confirmed, note if status == "confirmed" else row["note"],
                 alert_id, guardian_id),
            )
            return True

    def deliveries(self, alert_id: str) -> list[dict]:
        with self.lock, self._db() as con:
            rows = con.execute(
                "SELECT * FROM apk_deliveries WHERE alert_id=?", (alert_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    # -- conversations / AI requests -------------------------------------
    def create_conversation(self, family_id: str, guardian_id: str) -> str:
        conversation_id = str(uuid4())
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_conversations(id,family_id,guardian_id,created_at) VALUES(?,?,?,?)",
                (conversation_id, family_id, guardian_id,
                 datetime.now(timezone.utc).isoformat()),
            )
        return conversation_id

    def conversation(self, conversation_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            return dict(row) if row else None

    def create_ai_request(self, conversation_id: str, device_id: str, guardian_id: str,
                          question: str, context: dict, context_hash: str, status: str,
                          answer: str | None, error: str | None) -> str:
        request_id = str(uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.lock, self._db() as con:
            con.execute(
                "INSERT INTO apk_ai_requests(id,conversation_id,device_id,guardian_id,question,"
                "context_json,context_hash,status,answer,error,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, conversation_id, device_id, guardian_id, question,
                 json.dumps(context, ensure_ascii=False), context_hash, status,
                 answer, error, now, now),
            )
        return request_id

    def ai_request(self, request_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_ai_requests WHERE id=?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    # -- device configuration --------------------------------------------
    def latest_config(self, device_id: str):
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT * FROM apk_config_versions WHERE device_id=? ORDER BY version DESC LIMIT 1",
                (device_id,),
            ).fetchone()
            return dict(row) if row else None

    def publish_config(self, device_id: str, guardian_id: str, base_version: int,
                       parameters: dict) -> int | None:
        """Return the new version, or None when base_version is stale."""
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT COALESCE(MAX(version),0) AS version FROM apk_config_versions WHERE device_id=?",
                (device_id,),
            ).fetchone()
            current = int(row["version"])
            if current != base_version:
                return None
            version = current + 1
            con.execute(
                "INSERT INTO apk_config_versions(device_id,version,guardian_id,parameters_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (device_id, version, guardian_id,
                 json.dumps(parameters, ensure_ascii=False),
                 datetime.now(timezone.utc).isoformat()),
            )
            return version

    def ack_config(self, device_id: str, guardian_id: str, version: int,
                   status: str, reason: str | None) -> bool:
        with self.lock, self._db() as con:
            row = con.execute(
                "SELECT 1 FROM apk_config_versions WHERE device_id=? AND version=?",
                (device_id, version),
            ).fetchone()
            if row is None:
                return False
            con.execute(
                "INSERT INTO apk_config_acks(device_id,guardian_id,version,status,reason,updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(device_id,guardian_id,version) DO UPDATE SET "
                "status=excluded.status,reason=excluded.reason,updated_at=excluded.updated_at",
                (device_id, guardian_id, version, status, reason,
                 datetime.now(timezone.utc).isoformat()),
            )
            return True

    # -- data lifecycle ---------------------------------------------------
    def delete_device_data(self, device_id: str) -> None:
        alert_ids = "SELECT id FROM apk_alerts WHERE device_id=?"
        with self.lock, self._db() as con:
            con.execute(f"DELETE FROM apk_deliveries WHERE alert_id IN ({alert_ids})", (device_id,))
            con.execute(f"DELETE FROM apk_alerts WHERE device_id=?", (device_id,))
            con.execute("DELETE FROM apk_ai_requests WHERE device_id=?", (device_id,))
            con.execute("DELETE FROM apk_observations WHERE device_id=?", (device_id,))
            con.execute("DELETE FROM apk_batches WHERE device_id=?", (device_id,))
            con.execute("DELETE FROM apk_samples WHERE device_id=?", (device_id,))
