"""
FK-safe erasure of everything an account owns (GDPR "delete forever").

Why the statement list is *derived* instead of written out
----------------------------------------------------------
Every FK in this schema is ``NO ACTION``, so a ``users`` row can only go once
every row that references it is gone. An ``IntegrityError`` mid-erasure used to
be the reward for missing one — a 500, and an account still sitting in the
middle of its own deletion. The hand-maintained list in ``delete_account`` is
exactly how that happened: persona, subscription, billing, ledger, usage,
notification, interview-prep, intel, funding and scheduler tables were all
added to the schema and never added to the list.

So there is no list here. The plan is computed from ``Base.metadata``:

* **owned** — every table with a FK to ``users.id`` is deleted by ``user_id``.
  A new tenant table cannot be forgotten, because there is nothing to forget.
* **cycle breakers** — the schema has intentional cycles (``jobs`` ↔ ``resumes``,
  ``personas`` ↔ ``resumes``, ``profiles`` → ``resumes``) that no delete order
  can satisfy. Every *nullable* FK between two owned tables is therefore nulled
  for this account's rows first; that is what makes an order exist at all.
* **order** — the FKs that cannot be nulled (``job_events.job_id``,
  ``email_events.email_id``) are the ones that actually constrain the order, so
  the deletes are topologically sorted over those edges: children before
  parents, ``users`` last.
* **link tables** — a table with no ``user_id`` that hangs off an owned table
  (``funding_scan_companies``) is cleaned too: join rows are deleted when their
  FK is NOT NULL, nulled when it is nullable. The erasure therefore never
  depends on ``ON DELETE CASCADE`` firing, which it only does where FK
  enforcement is enabled at all.

The null-outs deliberately do **not** filter on ``user_id``: a reference from
another tenant's row (a tenancy bug, or a row picked up by the v1.2 → 2.0
``claim_legacy_rows`` upgrade) must not turn an erasure into a 500, and dropping
a pointer is strictly better than refusing to delete. Rows are only ever
*deleted* by owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Set, Tuple, cast

from sqlalchemy import Executable, Table, bindparam, delete, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import BindParameter

from app.core.logging import get_logger
from app.db import Base

log = get_logger("app.erasure")

#: The root of the FK graph. Its row is the last statement of an erasure.
USERS_TABLE = "users"
#: The column every tenant-owned table carries (see ``app/models/models.py``).
USER_COLUMN = "user_id"

#: Bound by the caller: ``db.execute(stmt, {"uid": user_id})``.
UID: BindParameter[Any] = bindparam("uid")


def _rowcount(db: Session, statement: Executable, params: Dict[str, Any]) -> int:
    """Rows affected by a DML statement; ``Session.execute`` returns a
    ``CursorResult`` for these at runtime, but types it as a plain ``Result``."""
    return int(cast(CursorResult[Any], db.execute(statement, params)).rowcount or 0)


@dataclass(frozen=True)
class Statements:
    """Labelled Core statements — ``table`` for a delete, ``table.column`` for a
    null-out, so the affected-row counts read well in a log line or a failure."""

    items: Tuple[Tuple[str, Executable], ...] = field(default_factory=tuple)

    def run(self, db: Session, user_id: int, counts: Dict[str, int]) -> Dict[str, int]:
        for label, stmt in self.items:
            counts[label] = _rowcount(db, stmt, {"uid": user_id})
        return counts


@dataclass(frozen=True)
class ErasureResult:
    """What an erasure did: rows deleted per table, references detached per column.

    Kept apart because the two numbers answer different questions — ``rows_removed``
    is what a data subject's "delete everything" has to be able to prove, while
    ``references_detached`` is the count of pointers the cycle-breaking pass had to
    cut before any order of deletes existed.
    """

    deleted: Dict[str, int] = field(default_factory=dict)
    detached: Dict[str, int] = field(default_factory=dict)

    @property
    def rows_removed(self) -> int:
        return sum(self.deleted.values())

    @property
    def references_detached(self) -> int:
        return sum(self.detached.values())


@dataclass(frozen=True)
class ErasurePlan:
    """Everything an erasure has to do, in the order it has to do it."""

    break_cycles: Statements = field(default_factory=Statements)
    link_rows: Statements = field(default_factory=Statements)
    delete_owned: Statements = field(default_factory=Statements)
    delete_user: Statements = field(default_factory=Statements)

    @property
    def owned_tables(self) -> Tuple[str, ...]:
        """Tenant tables in delete order (children first), ``users`` excluded."""
        return tuple(label for label, _ in self.delete_owned.items)


def _table(name: str) -> Table:
    return Base.metadata.tables[name]


def _pk(table: Table):
    """The table's single primary key column (every model in this schema has one)."""
    columns = list(table.primary_key.columns)
    if len(columns) != 1:
        raise RuntimeError(f"{table.name}: the erasure plan needs a single-column primary key")
    return columns[0]


def _owned_tables() -> Dict[str, Table]:
    """Every table whose ``user_id`` is a FK to ``users.id``."""
    owned: Dict[str, Table] = {}
    for name, table in sorted(Base.metadata.tables.items()):
        if name == USERS_TABLE:
            continue
        column = table.columns.get(USER_COLUMN)
        if column is None:
            continue
        if any(fk.target_fullname == f"{USERS_TABLE}.id" for fk in column.foreign_keys):
            owned[name] = table
        else:
            # A user_id with no constraint behind it: the erasure cannot reach
            # these rows by ownership, so the drift has to be visible, not silent.
            log.warning("table %s has a %s column with no FK to %s.id — the erasure plan cannot cover it",
                        name, USER_COLUMN, USERS_TABLE)
    return owned


def _owned_ids(parent: Table):
    """``SELECT parent.id WHERE parent.user_id = :uid`` — the ids this account owns."""
    return select(_pk(parent)).where(parent.c[USER_COLUMN] == UID).scalar_subquery()


@lru_cache(maxsize=1)
def build_plan() -> ErasurePlan:
    """Compute the erasure plan from the live schema metadata (once per process)."""
    owned = _owned_tables()
    nulls: List[Tuple[str, Executable]] = []
    link_statements: List[Tuple[str, Executable]] = []
    #: parent table -> children that reference it through a NOT NULL FK.
    children_of: Dict[str, Set[str]] = {}

    for name, table in owned.items():
        for fk in sorted(table.foreign_keys, key=lambda fk: fk.parent.name):
            parent_name = fk.target_fullname.split(".")[0]
            parent = owned.get(parent_name)
            if parent is None or parent_name == USERS_TABLE:
                continue  # the user link is what the deletes below handle
            column = table.c[fk.parent.name]
            if column.nullable:
                # A cycle can only be broken by a NULL, so every nullable
                # reference between two owned tables is dropped up front.
                nulls.append((
                    f"{name}.{column.name}",
                    update(table).where(column.in_(_owned_ids(parent))).values({column.name: None}),
                ))
            else:
                # Cannot be nulled — so this child must be gone before its parent.
                children_of.setdefault(parent_name, set()).add(name)

    # Tables with no user_id at all can still point at an owned row; the pointer
    # is the only way to reach them.
    for name, table in sorted(Base.metadata.tables.items()):
        if name in owned or name == USERS_TABLE:
            continue
        for fk in sorted(table.foreign_keys, key=lambda fk: fk.parent.name):
            parent = owned.get(fk.target_fullname.split(".")[0])
            if parent is None:
                continue
            column = table.c[fk.parent.name]
            condition = column.in_(_owned_ids(parent))
            if column.nullable:
                link_statements.append((f"{name}.{column.name}",
                                        update(table).where(condition).values({column.name: None})))
            else:
                # A NOT NULL link from an ownerless table is a join row: it means
                # nothing once its parent is gone, so it goes with the parent.
                link_statements.append((f"{name}.{column.name}", delete(table).where(condition)))

    order: List[str] = []
    done: Set[str] = set()
    stack: Set[str] = set()

    def visit(name: str) -> None:
        """Post-order over parent → children: a parent is appended only after
        every child that has to precede it."""
        if name in done:
            return
        if name in stack:  # a NOT NULL cycle: no order satisfies it, say so loudly
            log.error("foreign key cycle among %s — the erasure order there is best effort",
                      " → ".join(sorted(stack)))
            return
        stack.add(name)
        for child in sorted(children_of.get(name, ())):
            visit(child)
        stack.discard(name)
        done.add(name)
        order.append(name)

    for name in sorted(owned):
        visit(name)
    # ``users`` is what every owned table points at; it goes last even where the
    # user_id is nullable (error logs, the audit trail, billing events).
    order = [name for name in order if name != USERS_TABLE] + [USERS_TABLE]

    deletes: List[Tuple[str, Executable]] = []
    for name in order:
        table = _table(name)
        if name == USERS_TABLE:
            deletes.append((USERS_TABLE, delete(table).where(_pk(table) == UID)))
        else:
            deletes.append((name, delete(table).where(table.c[USER_COLUMN] == UID)))

    return ErasurePlan(
        break_cycles=Statements(tuple(nulls)),
        link_rows=Statements(tuple(link_statements)),
        delete_owned=Statements(tuple(entry for entry in deletes if entry[0] != USERS_TABLE)),
        delete_user=Statements(tuple(entry for entry in deletes if entry[0] == USERS_TABLE)),
    )


def owned_tables() -> Tuple[str, ...]:
    """Every tenant table the erasure deletes from, children first.

    Derived from the metadata, so a test can assert coverage against the schema
    itself instead of against a second hand-written list.
    """
    return build_plan().owned_tables


def break_references(db: Session, table_name: str, row_id: int) -> Tuple[Dict[str, int], List[str]]:
    """
    Null every nullable reference to one row; report what could not be nulled.

    This is the single-row sibling of :func:`purge_user_data` — an endpoint that
    deletes one row (a resume, later a profile) has the same problem in miniature:
    the schema decides which columns point at it, not the endpoint's author, and a
    column added later must not turn the delete into a 500.

    Returns ``(nulled, blockers)``: ``nulled`` maps ``table.column`` to the number
    of rows detached, ``blockers`` names the ``table.column`` pairs that reference
    the row through a NOT NULL FK and therefore cannot be detached at all — those
    are the caller's problem to answer with a 409, not the database's to answer
    with a 500. A caller that *re-points* one of the nullable pointers instead of
    leaving it NULL (the profile's master resume) can do so afterwards: the rows
    it has loaded are simply assigned a new target before the commit.
    """
    nulled: Dict[str, int] = {}
    blockers: List[str] = []
    for name, table in sorted(Base.metadata.tables.items()):
        for fk in sorted(table.foreign_keys, key=lambda fk: fk.parent.name):
            if fk.target_fullname.split(".")[0] != table_name:
                continue
            column = table.c[fk.parent.name]
            label = f"{name}.{column.name}"
            if not column.nullable:
                blockers.append(label)
                continue
            count = _rowcount(db, update(table).where(column == row_id).values({column.name: None}), {})
            if count:
                nulled[label] = count
    return nulled, blockers


def purge_user_data(db: Session, user_id: int, *, include_user: bool = True) -> ErasureResult:
    """
    Delete every row this account owns, in a FK-safe order.

    Runs inside the caller's transaction and does **not** commit — the caller
    owns the question of what else belongs to the erasure (files, the audit row)
    and when it becomes permanent. The caller's own ``IntegrityError`` handling
    stays the last line of defence for a schema mutated outside the models.
    """
    plan = build_plan()
    result = ErasureResult()
    plan.break_cycles.run(db, user_id, result.detached)
    plan.link_rows.run(db, user_id, result.detached)
    plan.delete_owned.run(db, user_id, result.deleted)
    if include_user:
        plan.delete_user.run(db, user_id, result.deleted)
    log.info("erased user %s: %s row(s) in %s table(s), %s reference(s) detached",
             user_id, result.rows_removed, len(result.deleted), result.references_detached)
    return result


def count_user_rows(db: Session, user_id: int) -> Dict[str, int]:
    """
    Tables that still hold a row for ``user_id`` — empty after a clean erasure.

    This is also the assertion a deletion test should make: it walks the same
    derived table list as the plan, so a table the plan forgot shows up as a
    leftover instead of passing unnoticed. Ownerless link tables are reported as
    ``table.column``, counted as "still points at this account, or dangles" —
    zero before an erasure (the FK prevents it) and zero after one, because an
    orphaned join row is exactly the garbage this endpoint used to leave behind.
    """
    plan = build_plan()
    leftovers: Dict[str, int] = {}
    for name in (USERS_TABLE,) + plan.owned_tables:
        table = _table(name)
        column = _pk(table) if name == USERS_TABLE else table.c[USER_COLUMN]
        count = int(db.execute(
            select(func.count()).select_from(table).where(column == UID), {"uid": user_id}
        ).scalar_one())
        if count:
            leftovers[name] = count
    for label, _ in plan.link_rows.items:
        table_name, column_name = label.split(".", 1)
        table = _table(table_name)
        column = table.c[column_name]
        parent = _table(next(fk.target_fullname.split(".")[0] for fk in column.foreign_keys))
        # An orphan is as much a leftover as an owned row: both mean the erasure
        # stopped half way.
        count = int(db.execute(
            select(func.count()).select_from(table)
            .outerjoin(parent, _pk(parent) == column)
            .where(or_(parent.c[USER_COLUMN] == UID, _pk(parent).is_(None))),
            {"uid": user_id},
        ).scalar_one())
        if count:
            leftovers[label] = count
    return leftovers
