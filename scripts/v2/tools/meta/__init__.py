"""Meta tools — entity resolution and planning helpers.

These are v4 additions designed for the LLM planner. They return
normalized shapes (e.g. `{pg_id, title, confidence}`) that PlanSpec
`$sN.field` references can target without navigating nested lists.
"""
from scripts.v2.tools.meta import resolve_entity  # noqa: F401
