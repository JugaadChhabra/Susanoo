"""Translate modules (Haiku): data already in state, said in plain English. [PRD › Non-technical users]

- `agent_card`: what the agent does, can do and will not do.
- `report_card`: "Caught 9 of 10 problems, 1 false alarm. Last week: 6 of 10." (pure code).
- `feedback_to_scenario`: thumbs down + "it should have..." becomes a Scenario that enters the normal loop.
"""

from susanoo.translate.agent_card import AgentCard, agent_card, fallback_card
from susanoo.translate.feedback_to_scenario import FeedbackRejected, feedback_to_scenario
from susanoo.translate.report_card import ReportCard, SetScore, previous_label, report_card, score_from_results

__all__ = [
    "AgentCard",
    "FeedbackRejected",
    "ReportCard",
    "SetScore",
    "agent_card",
    "fallback_card",
    "feedback_to_scenario",
    "previous_label",
    "report_card",
    "score_from_results",
]
