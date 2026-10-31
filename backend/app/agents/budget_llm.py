import logging
import re
from typing import Annotated, Literal, Optional, Sequence

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from ollama import ResponseError
from pydantic import BaseModel, StringConstraints, ValidationError

# the budget graph's two llm calls (spec: tpc-19): name an overspend's cause, then pick what to do.
# the facts come from the privacy gateway as a json string; the model returns json that pydantic checks,
# and the graph falls back to a plain alert when it can't

logger = logging.getLogger(__name__)

DIAGNOSE_PROMPT = (
    "You review one overspent budget category for a personal finance app. The facts below were computed "
    "from the user's transactions. Classify the cause as exactly one of these, checking them in this order:\n"
    "1. recurring: a merchant whose pattern is started_recently (it began charging since the budget was set "
    "and now charges every month) explains much of the spending, at about its typical amount.\n"
    "2. one_time: otherwise, the largest purchase explains most of the spending (share_of_spent above 0.5) "
    "and its merchant's pattern is new or occasional, or it is far above what that merchant usually comes to (times_typical "
    "of 2 or more). A big purchase at a regular shop is one_time, not recurring.\n"
    "3. seasonal: otherwise, spending spread across several purchases with no single one dominating, in a "
    "month with typical seasonal spending (holidays in November and December, back-to-school in August and "
    "September, summer travel). Use the month.\n"
    "Use only the facts given and quote numbers exactly as they appear. Answer in JSON with cause and a "
    "one-sentence evidence."
)

DECIDE_PROMPT = (
    "You decide what a budgeting app does about one overspent category.\n"
    "First check memory, before anything else. If there are earlier alerts for this category this month and "
    "this flag's severity is not higher than the highest earlier severity (high is higher than medium), the user "
    "was already told: choose no_op and say in the message that nothing new has happened. Never choose "
    "adjust_next_budget if past_advice has a dismissed adjust_next_budget.\n"
    "Otherwise choose exactly one action:\n"
    "- adjust_next_budget: only for a recurring cause, when the higher spending will continue; propose next "
    "month's limit as given.\n"
    "- alert: tell the user about an unusual purchase or burst (usually a one_time cause).\n"
    "- suggest_cuts: flexible spending running hot without one cause; suggest where to spend less.\n"
    "- no_op: only when the overspend is trivial.\n"
    "Write message to the user in at most 2 plain sentences. Use only numbers given below, and mention the "
    "proposed limit if you choose adjust_next_budget. Don't give financial advice beyond the budget. "
    "Answer in JSON with action and message."
)

# ollama not running, timing out or refusing the request; anything else is a bug and still fails the run
UNREACHABLE = (httpx.HTTPError, ConnectionError, TimeoutError, ResponseError)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


# whitespace-only text counts as empty: a blank alert must never reach the user
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Diagnosis(BaseModel):
    cause: Literal["one_time", "seasonal", "recurring"]
    evidence: Text


class Action(BaseModel):
    action: Literal["alert", "suggest_cuts", "adjust_next_budget", "no_op"]
    message: Text


# ollama's `format` constrains decoding to the schema; pydantic still checks the reply, and a bad reply gets
# one more try. a model that can't be reached (down, timed out) gives None at once, like a bad reply, so the
# graph still writes its plain fallback alert: an outage must not hide an overspend
def invoke_structured(model: BaseChatModel, schema: type[BaseModel], messages: list,
                      retries: int = 1) -> Optional[BaseModel]:
    bound = model.bind(format=schema.model_json_schema())
    for _ in range(retries + 1):
        try:
            text = str(bound.invoke(messages).content)
        except UNREACHABLE as exc:
            logger.warning("budget llm unreachable: %s", type(exc).__name__)
            return None
        fenced = _FENCE.match(text)
        try:
            return schema.model_validate_json(fenced.group(1) if fenced else text)
        except ValidationError:
            continue
    return None


# the memory the decide rule turns on, summed up by code so a small model doesn't have to dig it out of the json
def _earlier(alerts: Sequence[dict]) -> str:
    if not alerts:
        return "none"
    top = max((a["severity"] for a in alerts), key=lambda sev: _RANK.get(sev, 0))
    return f"{len(alerts)}, highest severity {top}"


def diagnose_messages(context: str) -> list:
    return [SystemMessage(DIAGNOSE_PROMPT), HumanMessage(f"Facts:\n{context}")]


_RANK = {"medium": 1, "high": 2}


def decide_messages(context: str, diagnosis: Optional[Diagnosis], projected_cents: int, proposed_cents: int,
                    severity: str, earlier: Sequence[dict] = ()) -> list:
    cause = f"{diagnosis.cause}: {diagnosis.evidence}" if diagnosis else "unknown (the cause could not be determined)"
    # a holiday month or a one-off says nothing about next month, so only a recurring cause gets a proposal
    if diagnosis and diagnosis.cause == "recurring":
        proposal = f"${proposed_cents / 100:,.2f}"
    else:
        proposal = "none: adjust_next_budget is only for a recurring cause"
    return [SystemMessage(DECIDE_PROMPT), HumanMessage(
        f"Facts:\n{context}\n\nDiagnosis: {cause}\nSeverity of this flag: {severity}\n"
        f"Earlier alerts for this category this month: {_earlier(earlier)}\n"
        f"Projected spending by month end: ${projected_cents / 100:,.2f}\n"
        f"Proposed limit for next month: {proposal}")]
