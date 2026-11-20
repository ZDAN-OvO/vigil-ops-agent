"""会话持久化。跨域事务依次取得告警组锁、会话行锁和动作记录锁。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from psycopg import AsyncConnection, sql
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from aiops.sessions.models import (
    LEASE_SECONDS,
    AlertReceipt,
    Lease,
    LeaseLost,
    Session,
    SessionClosed,
    SessionEvent,
    SessionInput,
    Turn,
    TurnLane,
    TurnState,
)

if TYPE_CHECKING:
    from aiops.db import Store

# 等待行锁期间可能过期，租约必须在取得行锁后按数据库时钟核对。
SESSION_COLUMNS = """
    id, source, title, group_key, last_seq, created_at, updated_at, closed_at,
    CASE WHEN state = 'working'
         AND (lease_expires_at IS NULL OR lease_expires_at <= clock_timestamp())
         THEN 'interrupted' ELSE state END AS state
"""


async def _one(conn: AsyncConnection, query, params=()) -> DictRow | None:
    return await (await conn.execute(query, params)).fetchone()


def _turn(row: DictRow) -> Turn:
    return Turn(**{field.name: row[field.name] for field in fields(Turn)})


class SessionStore:
    def __init__(self, store: Store) -> None:
        self._store = store

    async def create_session(self, *, title: str) -> Session:
        async with self._store.transaction() as conn:
            session = await self._insert_session(conn, title=title, group_key=None)
            assert session is not None
            return session

    async def _insert_session(
        self, conn: AsyncConnection, *, title: str, group_key: str | None
    ) -> Session | None:
        row = await _one(
            conn,
            f"""
            INSERT INTO sessions (id, source, title, group_key)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (group_key) WHERE group_key IS NOT NULL AND closed_at IS NULL DO NOTHING
            RETURNING {SESSION_COLUMNS}
            """,
            (uuid4().hex, "alert" if group_key is not None else "user", title, group_key),
        )
        if row is None:
            return None
        session = Session(**row)
        event = await self.append_event(
            conn,
            session.id,
            "session",
            {"source": session.source, "title": title, "resumed": False},
        )
        return Session(**{**row, "last_seq": event.seq, "updated_at": event.ts})

    async def get_session(self, session_id: str) -> Session | None:
        async with self._store.transaction() as conn:
            row = await _one(
                conn, f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = %s", (session_id,)
            )
        return Session(**row) if row else None

    async def list_sessions(self, *, limit: int = 50) -> list[Session]:
        async with self._store.transaction() as conn:
            rows = await conn.execute(
                f"SELECT {SESSION_COLUMNS} FROM sessions "
                "ORDER BY updated_at DESC, id DESC LIMIT %s",
                (limit,),
            )
            return [Session(**row) for row in await rows.fetchall()]

    async def get_turn(self, turn_id: str) -> Turn | None:
        async with self._store.transaction() as conn:
            row = await _one(conn, "SELECT * FROM session_turns WHERE id = %s", (turn_id,))
            return _turn(row) if row else None

    async def list_turns(self, session_id: str) -> list[Turn]:
        async with self._store.transaction() as conn:
            rows = await conn.execute(
                "SELECT * FROM session_turns WHERE session_id = %s ORDER BY created_at, id",
                (session_id,),
            )
            return [_turn(row) for row in await rows.fetchall()]

    async def get_input(self, input_id: str) -> SessionInput | None:
        """内部队列记录包含原始文本。出站读取使用 list_events。"""
        async with self._store.transaction() as conn:
            row = await _one(conn, "SELECT * FROM session_inputs WHERE id = %s", (input_id,))
        return SessionInput(**row) if row else None

    async def list_runnable_main(self, *, limit: int = 50) -> list[str]:
        """扫描可领取的调查。待审批会话由处置模块按已决卡片另行扫描。"""
        async with self._store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT s.id FROM sessions s
                LEFT JOIN session_turns t
                    ON t.session_id = s.id AND t.lane = 'main' AND t.finished_at IS NULL
                WHERE s.closed_at IS NULL
                  AND (s.lease_expires_at IS NULL OR s.lease_expires_at <= clock_timestamp())
                  AND (t.state IN ('queued', 'running') OR (
                      t.id IS NULL AND EXISTS (
                          SELECT 1 FROM session_inputs i WHERE i.session_id = s.id
                          AND i.turn_id IS NULL AND i.checkpoint_id IS NULL
                      )
                  ))
                ORDER BY s.updated_at, s.id LIMIT %s
                """,
                (limit,),
            )
            return [row["id"] for row in await rows.fetchall()]

    async def list_events(
        self, session_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> list[SessionEvent]:
        async with self._store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT * FROM session_events WHERE session_id = %s AND seq > %s
                ORDER BY seq LIMIT %s
                """,
                (session_id, after_seq, limit),
            )
            return [SessionEvent(**row) for row in await rows.fetchall()]

    @staticmethod
    async def lock_session(conn: AsyncConnection, session_id: str) -> Session:
        row = await _one(
            conn,
            f"SELECT {SESSION_COLUMNS} FROM sessions WHERE id = %s FOR UPDATE",
            (session_id,),
        )
        if row is None:
            raise LookupError(f"会话不存在：{session_id}")
        return Session(**row)

    async def append_event(
        self,
        conn: AsyncConnection,
        session_id: str,
        type: str,
        payload: Mapping[str, Any],
        *,
        turn_id: str | None = None,
        allow_closed: bool = False,
    ) -> SessionEvent:
        """可信业务事务入口，payload 必须是出站投影。运行事件使用 run()。"""
        session = await self.lock_session(conn, session_id)
        if session.state == "closed" and not allow_closed:
            raise SessionClosed(session_id)
        row = await _one(
            conn,
            """
            UPDATE sessions SET last_seq = last_seq + 1, updated_at = clock_timestamp()
            WHERE id = %s RETURNING last_seq, updated_at
            """,
            (session_id,),
        )
        event = await _one(
            conn,
            """
            INSERT INTO session_events (session_id, seq, ts, turn_id, type, payload)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING *
            """,
            (session_id, row["last_seq"], row["updated_at"], turn_id, type, Jsonb(dict(payload))),
        )
        return SessionEvent(**event)

    async def enqueue_input(
        self, session_id: str, text: str, *, display_text: str, input_id: str | None = None
    ) -> SessionInput:
        async with self._store.transaction() as conn:
            return await self._enqueue_input(conn, session_id, text, display_text, input_id)

    async def _enqueue_input(
        self,
        conn: AsyncConnection,
        session_id: str,
        text: str,
        display_text: str,
        input_id: str | None = None,
        *,
        alert_names: tuple[str, ...] = (),
        alert_origin: bool = False,
    ) -> SessionInput:
        if (await self.lock_session(conn, session_id)).state == "closed":
            raise SessionClosed(session_id)
        input_id = input_id or uuid4().hex
        row = await _one(conn, "SELECT * FROM session_inputs WHERE id = %s", (input_id,))
        if row is not None:
            if row["session_id"] != session_id or row["text"] != text:
                raise ValueError("输入 ID 已绑定其它输入")
            return SessionInput(**row)
        payload = {"role": "user", "text": display_text, "input_id": input_id}
        if alert_origin:
            payload["source"] = "alert"
        event = await self.append_event(conn, session_id, "message", payload)
        row = await _one(
            conn,
            """
            INSERT INTO session_inputs (id, session_id, queued_seq, text, alert_names)
            VALUES (%s, %s, %s, %s, %s) RETURNING *
            """,
            (input_id, session_id, event.seq, text, list(alert_names)),
        )
        return SessionInput(**row)

    @asynccontextmanager
    async def alert_transaction(self, group_key: str) -> AsyncIterator[AsyncConnection]:
        if not group_key:
            raise ValueError("告警组键不能为空")
        async with self._store.transaction() as conn:
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (group_key,)
            )
            yield conn

    async def get_open_alert_session(self, conn: AsyncConnection, group_key: str) -> Session | None:
        row = await _one(
            conn,
            f"""
            SELECT {SESSION_COLUMNS} FROM sessions
            WHERE group_key = %s AND closed_at IS NULL FOR UPDATE
            """,
            (group_key,),
        )
        return Session(**row) if row else None

    async def record_alert(
        self,
        conn: AsyncConnection,
        group_key: str,
        *,
        title: str,
        text: str,
        display_text: str,
        repeat_display_text: str,
        alert_names: tuple[str, ...] = (),
    ) -> AlertReceipt:
        """在 alert_transaction 中归并 firing，首轮与首次输入同事务持久化。"""
        session = await self.get_open_alert_session(conn, group_key)
        if session is None:
            session = await self._insert_session(conn, title=title, group_key=group_key)
            if session is not None:
                incoming = await self._enqueue_input(
                    conn, session.id, text, display_text, alert_names=alert_names, alert_origin=True
                )
                turn = await self._new_turn(conn, incoming, lane="main")
                event = await _one(
                    conn,
                    "SELECT * FROM session_events WHERE session_id = %s AND seq = %s",
                    (session.id, incoming.queued_seq),
                )
                return AlertReceipt(session.id, True, SessionEvent(**event), turn.id)
            session = await self.get_open_alert_session(conn, group_key)
            if session is None:
                raise RuntimeError("告警组并发变更，请重试事务")
        event = await self.append_alert_message(conn, session.id, display_text=repeat_display_text)
        return AlertReceipt(session.id, False, event, None)

    async def append_alert_message(
        self, conn: AsyncConnection, session_id: str, *, display_text: str
    ) -> SessionEvent:
        """告警组提示只追加事件，调用方持有组锁。"""
        if (await self.lock_session(conn, session_id)).source != "alert":
            raise ValueError("会话来源必须为告警")
        return await self.append_event(
            conn, session_id, "message", {"role": "user", "text": display_text, "source": "alert"}
        )

    async def list_open_alert_sessions(self, group_keys: list[str]) -> list[Session]:
        if not group_keys:
            return []
        async with self._store.transaction() as conn:
            rows = await conn.execute(
                f"SELECT {SESSION_COLUMNS} FROM sessions "
                "WHERE group_key = ANY(%s) AND closed_at IS NULL",
                (group_keys,),
            )
            return [Session(**row) for row in await rows.fetchall()]

    async def close_session(
        self, conn: AsyncConnection, session_id: str, *, reason: str
    ) -> SessionEvent | None:
        """供处置模块组合原子关闭。告警会话须先在同一事务取得组锁。"""
        session = await self.lock_session(conn, session_id)
        if session.state == "closed":
            return None
        await conn.execute(
            "UPDATE sessions SET state = 'closed', closed_at = clock_timestamp() WHERE id = %s",
            (session_id,),
        )
        await conn.execute(
            """
            UPDATE session_turns SET state = 'aborted', finished_at = clock_timestamp()
            WHERE session_id = %s AND finished_at IS NULL
            """,
            (session_id,),
        )
        return await self.append_event(
            conn, session_id, "session_closed", {"reason": reason}, allow_closed=True
        )

    async def acquire_main_lease(self, session_id: str, owner: str) -> Lease | None:
        async with self._store.transaction() as conn:
            session = await self.lock_session(conn, session_id)
            if session.state == "closed":
                raise SessionClosed(session_id)
            return await self._acquire_lease(conn, session_id, owner)

    async def acquire_side_lease(self, session_id: str, owner: str) -> Lease | None:
        async with self._store.transaction() as conn:
            session = await self.lock_session(conn, session_id)
            if session.state == "closed":
                raise SessionClosed(session_id)
            turn = await self._active_turn(conn, session_id, "side")
            if turn is None:
                if session.state != "awaiting_approval":
                    return None
                incoming = await self._first_pending(conn, session_id)
                if incoming is None:
                    return None
                turn = await self._new_turn(conn, incoming, lane="side")
            return await self._acquire_lease(conn, session_id, owner, side_turn_id=turn.id)

    async def list_runnable_side(self, *, limit: int = 50) -> list[str]:
        """恢复仍活跃的旁路，也拾取待审批会话里的未投递输入。"""
        async with self._store.transaction() as conn:
            rows = await conn.execute(
                """
                SELECT s.id FROM sessions s
                LEFT JOIN session_turns t
                    ON t.session_id = s.id AND t.lane = 'side' AND t.finished_at IS NULL
                WHERE s.closed_at IS NULL AND (
                    (t.id IS NOT NULL AND (
                        t.lease_expires_at IS NULL OR t.lease_expires_at <= clock_timestamp()
                    ))
                    OR (t.id IS NULL AND s.state = 'awaiting_approval' AND EXISTS (
                        SELECT 1 FROM session_inputs i WHERE i.session_id = s.id
                        AND i.turn_id IS NULL AND i.checkpoint_id IS NULL
                    ))
                )
                ORDER BY s.updated_at, s.id LIMIT %s
                """,
                (limit,),
            )
            return [row["id"] for row in await rows.fetchall()]

    async def _acquire_lease(
        self, conn: AsyncConnection, session_id: str, owner: str, *, side_turn_id: str | None = None
    ) -> Lease | None:
        table = sql.Identifier("session_turns" if side_turn_id else "sessions")
        params = (side_turn_id or session_id,)
        row = await _one(
            conn,
            sql.SQL("""
                SELECT lease_owner, lease_generation, lease_expires_at,
                       lease_expires_at > clock_timestamp() AS valid FROM {} WHERE id = %s
            """).format(table),
            params,
        )
        if row["valid"]:
            if row["lease_owner"] != owner:
                return None
        else:
            row = await _one(
                conn,
                sql.SQL("""
                    UPDATE {} SET lease_owner = %s, lease_generation = lease_generation + 1,
                                  lease_expires_at = clock_timestamp() + make_interval(secs => %s)
                    WHERE id = %s RETURNING lease_generation, lease_expires_at
                """).format(table),
                (owner, LEASE_SECONDS, *params),
            )
        return Lease(
            session_id, owner, row["lease_generation"], row["lease_expires_at"], side_turn_id
        )

    async def renew_lease(self, lease: Lease) -> Lease:
        async with self._store.transaction() as conn:
            await self.require_lease(conn, lease)
            table = sql.Identifier("session_turns" if lease.side_turn_id else "sessions")
            row = await _one(
                conn,
                sql.SQL("""
                    UPDATE {} SET lease_expires_at = clock_timestamp() + make_interval(secs => %s)
                    WHERE id = %s AND lease_expires_at > clock_timestamp()
                    RETURNING lease_expires_at
                """).format(table),
                (LEASE_SECONDS, lease.side_turn_id or lease.session_id),
            )
            if row is None:
                raise LeaseLost(lease.session_id)
            return Lease(
                lease.session_id,
                lease.owner,
                lease.generation,
                row["lease_expires_at"],
                lease.side_turn_id,
            )

    async def release_lease(self, lease: Lease) -> bool:
        async with self._store.transaction() as conn:
            await self.lock_session(conn, lease.session_id)
            table = sql.Identifier("session_turns" if lease.side_turn_id else "sessions")
            scope = sql.SQL(" AND session_id = %s") if lease.side_turn_id else sql.SQL("")
            params = (lease.side_turn_id or lease.session_id, lease.owner, lease.generation)
            cursor = await conn.execute(
                sql.SQL("""
                    UPDATE {} SET lease_owner = NULL, lease_expires_at = NULL
                    WHERE id = %s AND lease_owner = %s AND lease_generation = %s{}
                """).format(table, scope),
                (*params, lease.session_id) if lease.side_turn_id else params,
            )
            return cursor.rowcount == 1

    @classmethod
    async def require_lease(
        cls, conn: AsyncConnection, lease: Lease, *, main_only: bool = False
    ) -> Session:
        """持会话行锁核对代次与有效期。处置调用 main_only=True。"""
        session = await cls.lock_session(conn, lease.session_id)
        if session.state == "closed" or (main_only and lease.side_turn_id is not None):
            raise LeaseLost(lease.session_id)
        await cls._check_locked_lease(conn, lease)
        return session

    @staticmethod
    async def _check_locked_lease(
        conn: AsyncConnection, lease: Lease, *, finishing_side: bool = False
    ) -> None:
        if lease.side_turn_id is None:
            row = await _one(
                conn,
                """
                SELECT 1 FROM sessions WHERE id = %s AND lease_owner = %s
                  AND lease_generation = %s AND lease_expires_at > clock_timestamp()
                """,
                (lease.session_id, lease.owner, lease.generation),
            )
        else:
            row = await _one(
                conn,
                """
                SELECT 1 FROM session_turns WHERE id = %s AND session_id = %s AND lane = 'side'
                  AND (finished_at IS NULL OR %s) AND lease_owner = %s
                  AND lease_generation = %s AND lease_expires_at > clock_timestamp()
                """,
                (
                    lease.side_turn_id,
                    lease.session_id,
                    finishing_side,
                    lease.owner,
                    lease.generation,
                ),
            )
        if row is None:
            raise LeaseLost(lease.session_id)

    async def start_main_turn(self, lease: Lease) -> Turn | None:
        """在缺少活跃主线时进入下一轮；接手时返回原轮及其状态。"""
        async with self._store.transaction() as conn:
            await self.require_lease(conn, lease, main_only=True)
            turn = await self._active_turn(conn, lease.session_id, "main")
            if turn is None:
                incoming = await self._first_pending(conn, lease.session_id)
                if incoming is None:
                    return None
                turn = await self._new_turn(conn, incoming, lane="main")
            if turn.state == "queued":
                row = await _one(
                    conn,
                    "UPDATE session_turns SET state = 'running' WHERE id = %s RETURNING *",
                    (turn.id,),
                )
                await self._set_session_state(conn, lease.session_id, "working")
                turn = _turn(row)
            await self.require_lease(conn, lease, main_only=True)
            return turn

    async def _active_turn(
        self, conn: AsyncConnection, session_id: str, lane: TurnLane
    ) -> Turn | None:
        row = await _one(
            conn,
            """
            SELECT * FROM session_turns WHERE session_id = %s AND lane = %s AND finished_at IS NULL
            """,
            (session_id, lane),
        )
        return _turn(row) if row else None

    async def _new_turn(
        self, conn: AsyncConnection, incoming: SessionInput, *, lane: TurnLane
    ) -> Turn:
        turn_id = uuid4().hex
        session_id = incoming.session_id
        thread_id = session_id if lane == "main" else f"{session_id}/side/{turn_id}"
        row = await _one(
            conn,
            """
            INSERT INTO session_turns (id, session_id, lane, state, checkpoint_thread_id,
                                       first_input_id)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                turn_id,
                session_id,
                lane,
                "queued" if lane == "main" else "running",
                thread_id,
                incoming.id,
            ),
        )
        await conn.execute(
            "UPDATE session_inputs SET turn_id = %s WHERE id = %s", (turn_id, incoming.id)
        )
        return _turn(row)

    async def _first_pending(self, conn: AsyncConnection, session_id: str) -> SessionInput | None:
        row = await _one(
            conn,
            """
            SELECT * FROM session_inputs
            WHERE session_id = %s AND checkpoint_id IS NULL AND turn_id IS NULL
            ORDER BY queued_seq LIMIT 1
            """,
            (session_id,),
        )
        return SessionInput(**row) if row else None

    @staticmethod
    async def require_turn(conn: AsyncConnection, lease: Lease, turn_id: str) -> Turn:
        await SessionStore.require_lease(conn, lease)
        lane = "side" if lease.side_turn_id is not None else "main"
        if lease.side_turn_id is not None and lease.side_turn_id != turn_id:
            raise LeaseLost(turn_id)
        row = await _one(
            conn,
            """
            SELECT * FROM session_turns WHERE id = %s AND session_id = %s AND lane = %s
            AND state IN ('running', 'awaiting_approval')
            """,
            (turn_id, lease.session_id, lane),
        )
        if row is None:
            raise LeaseLost(turn_id)
        return _turn(row)

    @asynccontextmanager
    async def run(self, lease: Lease, turn_id: str) -> AsyncIterator[RunTransaction]:
        """短事务：同一连接提交账本、轮次和事件。外部调用放在事务之外。"""
        async with self._store.transaction() as conn:
            turn = await self.require_turn(conn, lease, turn_id)
            transaction = RunTransaction(self, conn, lease, turn)
            yield transaction
            await self._check_locked_lease(
                conn, lease, finishing_side=transaction.turn.finished_at is not None
            )

    async def pending_inputs(
        self, lease: Lease, turn_id: str, *, limit: int = 100
    ) -> list[SessionInput]:
        async with self.run(lease, turn_id) as tx:
            return await tx.pending_inputs(limit=limit)

    @staticmethod
    async def _set_session_state(conn: AsyncConnection, session_id: str, state: str) -> None:
        await conn.execute(
            "UPDATE sessions SET state = %s, updated_at = clock_timestamp() WHERE id = %s",
            (state, session_id),
        )


class RunTransaction:
    def __init__(
        self, sessions: SessionStore, conn: AsyncConnection, lease: Lease, turn: Turn
    ) -> None:
        self._sessions = sessions
        self.conn = conn
        self.turn = turn
        self.ledger = sessions._store.for_turn(lease, turn.id, _conn=conn)

    async def append_event(self, type: str, payload: Mapping[str, Any]) -> SessionEvent:
        return await self._sessions.append_event(
            self.conn, self.turn.session_id, type, payload, turn_id=self.turn.id
        )

    async def pending_inputs(self, *, limit: int = 100) -> list[SessionInput]:
        if self.turn.state == "awaiting_approval":
            return []
        rows = await self.conn.execute(
            """
            SELECT * FROM session_inputs WHERE session_id = %s AND checkpoint_id IS NULL
            AND (turn_id = %s OR (%s AND turn_id IS NULL)) ORDER BY queued_seq LIMIT %s
            """,
            (self.turn.session_id, self.turn.id, self.turn.lane == "main", limit),
        )
        return [SessionInput(**row) for row in await rows.fetchall()]

    async def set_turn_state(self, state: TurnState) -> Turn:
        """主线暂停和继续要与相关事件同事务；旁路只改变自己的轮次。"""
        current = self.turn.state
        if state == current:
            return self.turn
        terminal = state in ("completed", "aborted")
        if current not in ("running", "awaiting_approval") or (
            not terminal
            and not (self.turn.lane == "main" and state in ("running", "awaiting_approval"))
        ):
            raise ValueError(f"轮次状态无法从 {current} 转为 {state}")
        row = await _one(
            self.conn,
            """
            UPDATE session_turns SET state = %s,
                finished_at = CASE WHEN %s THEN clock_timestamp() END
            WHERE id = %s RETURNING *
            """,
            (state, terminal, self.turn.id),
        )
        if terminal:
            await self.conn.execute(
                "UPDATE session_inputs SET turn_id = NULL "
                "WHERE session_id = %s AND turn_id = %s AND checkpoint_id IS NULL",
                (self.turn.session_id, self.turn.id),
            )
        if self.turn.lane == "main":
            session_state = "idle" if terminal else "working" if state == "running" else state
            await self._sessions._set_session_state(self.conn, self.turn.session_id, session_state)
        self.turn = _turn(row)
        return self.turn
