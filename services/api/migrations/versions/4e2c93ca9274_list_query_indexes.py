"""composite indexes for the hot list and stats queries (ADR 0006 WS-D)

Revision ID: 4e2c93ca9274
Revises: d834b5178d61
Create Date: 2026-10-19 01:00:00.000000

Each index matches a query's equality columns followed by its sort/range column:

- leads: the "recently updated" and "last contacted" grid sorts.
- calls: a lead's call history (org, lead, newest first) and a campaign's progress
  counters (org, campaign, state); they supersede the two-column org+lead and
  org+campaign indexes.
- campaigns: the campaign list (org, newest first).
- campaign_leads: a campaign's lead list (campaign, created_at order).
- tasks: open/overdue counters and the status-filtered task list (org, status, due).

Activities are already covered by ix_activities_lead_time (org, lead, occurred_at).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "4e2c93ca9274"
down_revision: str | Sequence[str] | None = "d834b5178d61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_leads_org_updated", "leads", ["organization_id", "updated_at"])
    op.create_index(
        "ix_leads_org_last_contacted", "leads", ["organization_id", "last_contacted_at"]
    )
    op.create_index(
        "ix_calls_org_lead_created", "calls", ["organization_id", "lead_id", "created_at"]
    )
    op.create_index(
        "ix_calls_org_campaign_state", "calls", ["organization_id", "campaign_id", "state"]
    )
    op.drop_index("ix_calls_org_lead", table_name="calls")
    op.drop_index("ix_calls_org_campaign", table_name="calls")
    op.create_index("ix_campaigns_org_created", "campaigns", ["organization_id", "created_at"])
    op.create_index(
        "ix_campaign_leads_campaign_created", "campaign_leads", ["campaign_id", "created_at"]
    )
    op.create_index("ix_tasks_org_status_due", "tasks", ["organization_id", "status", "due_at"])


def downgrade() -> None:
    op.drop_index("ix_tasks_org_status_due", table_name="tasks")
    op.drop_index("ix_campaign_leads_campaign_created", table_name="campaign_leads")
    op.drop_index("ix_campaigns_org_created", table_name="campaigns")
    op.create_index("ix_calls_org_campaign", "calls", ["organization_id", "campaign_id"])
    op.create_index("ix_calls_org_lead", "calls", ["organization_id", "lead_id"])
    op.drop_index("ix_calls_org_campaign_state", table_name="calls")
    op.drop_index("ix_calls_org_lead_created", table_name="calls")
    op.drop_index("ix_leads_org_last_contacted", table_name="leads")
    op.drop_index("ix_leads_org_updated", table_name="leads")
