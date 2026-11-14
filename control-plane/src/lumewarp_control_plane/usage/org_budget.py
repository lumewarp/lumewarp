"""The org level of the budget hierarchy, derived from its teams.

An org has no row in `usage_totals`: its monthly spend is the sum of every one of its
teams (archived included). After an ingest only orgs that set a budget are checked; the
first crossing inserts `org_budget_windows` with `ON CONFLICT DO NOTHING`, which makes the
audit row and the recompile exactly-once even when several teams of one org are ingested
concurrently. A batch whose sibling has not committed yet may see less than the final
total; the crossing is then detected by the next ingest or compile, the same eventual
semantics the team and owner budgets have.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from lumewarp_control_plane import audit
from lumewarp_control_plane.models import (
    ActorType,
    Org,
    OrgBudgetWindow,
    ScopeType,
    Team,
    UsageTotal,
)
from lumewarp_control_plane.windows import month_window


async def reset_org_budget_window(
    session: AsyncSession, org_id: uuid.UUID, window_start: datetime
) -> None:
    """Forget that an org already reported a crossing in this month.

    Called when a policy change can clear the exceeded state (a raised or removed budget):
    the next crossing must audit and recompile again, exactly as a team budget crossing
    does after the team budget is raised. Delete-only: a still-exceeded org reports once
    more on the next ingest, which is correct because the policy that produced the old
    report no longer applies.
    """
    await session.execute(
        delete(OrgBudgetWindow).where(
            OrgBudgetWindow.org_id == org_id,
            OrgBudgetWindow.window_start == window_start,
        )
    )


async def enforce_org_budgets(
    session: AsyncSession,
    gateway_id: str,
    team_ids: set[uuid.UUID],
    window_start: datetime,
) -> bool:
    """Mark newly exceeded orgs for `window_start`; return whether one flipped this month.

    `gateway_id` is the reporting process, used only as the audit actor. `team_ids` are the
    teams whose totals this ingest changed; only their orgs are checked. An org without a
    budget is never read. The event timestamp has already
    selected the window (`_add_totals`), so a late previous-month event is judged against
    its own month and never blocks the current one: it still records its window row and
    audit, but only a current-month crossing asks for a recompile.
    """
    if not team_ids:
        return False
    orgs = (
        await session.execute(
            select(Org.id, Org.budget_nano_usd)
            .join(Team, Team.org_id == Org.id)
            .where(Team.id.in_(team_ids), Org.budget_nano_usd.isnot(None))
            .distinct()
        )
    ).all()
    flipped = False
    for org_id, budget in orgs:
        spent = await session.scalar(
            select(func.coalesce(func.sum(UsageTotal.cost_nano_usd), 0))
            .select_from(UsageTotal)
            .join(Team, Team.id == UsageTotal.scope_id)
            .where(
                UsageTotal.scope_type == ScopeType.TEAM,
                UsageTotal.window_start == window_start,
                Team.org_id == org_id,
            )
        )
        if spent is None or spent < budget:
            continue
        inserted = await session.scalar(
            pg_insert(OrgBudgetWindow)
            .values(org_id=org_id, window_start=window_start)
            .on_conflict_do_nothing(
                index_elements=[OrgBudgetWindow.org_id, OrgBudgetWindow.window_start]
            )
            .returning(OrgBudgetWindow.org_id)
        )
        if inserted is None:
            continue
        if window_start == month_window(datetime.now(UTC))[0]:
            flipped = True
        audit.record(
            session,
            actor_type=ActorType.SYSTEM,
            actor_id=f"usage:{gateway_id}",
            action="budget.hard_exceeded",
            resource_type=ScopeType.ORG.value,
            resource_id=org_id,
            org_id=org_id,
            # Deliberately not attached to the team whose batch happened to cross it.
            team_id=None,
            details={
                "window_start": window_start.isoformat(),
                "spent_nano_usd": spent,
                "budget_nano_usd": budget,
            },
        )
    return flipped
