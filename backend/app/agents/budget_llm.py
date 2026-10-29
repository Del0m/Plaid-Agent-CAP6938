import re
from typing import Literal, Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

# the budget graph's two llm calls (spec: tpc-19): name an overspend's cause, then pick what to do.
# the facts come from the privacy gateway as a json string; the model returns json that pydantic checks,
# and the graph falls back to a plain alert when it can't

DIAGNOSE_PROMPT = (
    "You review one overspent budget category for a personal finance app. The facts below were computed "
    "from the user's transactions. Classify the cause as exactly one of:\n"
    "- one_time: one purchase, or a short burst, at merchants not seen in earlier months "
    "(months_seen_before is 0), that explains most of the spending (a high share_of_spent).\n"
    "- recurring: a charge that repeats every month at about the same amount and is new or larger since the "
    "budget was set (months_seen_before of 1 or 2, a typical amount close to this month's).\n"
    "- seasonal: spending tied to the time of year, such as holidays, back-to-school or summer travel, "
    "spread across several purchases with no single one dominating. Use the month.\n"
    "Use only the facts given and quote numbers exactly as they appear. Answer in JSON with cause and a "
    "one-sentence evidence."
)

DECIDE_PROMPT = (
    "You decide what a budgeting app does about one overspent category. Choose exactly one action:\n"
    "- adjust_next_budget: the higher spending will continue (usually a recurring cause); propose next "
    "month's limit as given.\n"
    "- alert: tell the user about an unusual purchase or burst (usually a one_time cause).\n"
    "- suggest_cuts: flexible spending running hot without one cause; suggest where to spend less.\n"
    "- no_op: only when the overspend is trivial.\n"
    "Write message to the user in at most 2 plain sentences. Use only numbers given below, and mention the "
    "proposed limit if you choose adjust_next_budget. Don't give financial advice beyond the budget. "
    "Answer in JSON with action and message."
)

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class Diagnosis(BaseModel):
    cause: Literal["one_time", "seasonal", "recurring"]
    evidence: str = Field(min_length=1)


class Action(BaseModel):
    action: Literal["alert", "suggest_cuts", "adjust_next_budget", "no_op"]
    message: str = Field(min_length=1)


# ollama's `format` constrains decoding to the schema; pydantic still checks the reply, and a bad reply gets
# one more try. connection errors propagate: the run fails and the next daily run retries
def invoke_structured(model: BaseChatModel, schema: type[BaseModel], messages: list,
                      retries: int = 1) -> Optional[BaseModel]:
    bound = model.bind(format=schema.model_json_schema())
    for _ in range(retries + 1):
        text = str(bound.invoke(messages).content)
        fenced = _FENCE.match(text)
        try:
            return schema.model_validate_json(fenced.group(1) if fenced else text)
        except ValidationError:
            continue
    return None


def diagnose_messages(context: str) -> list:
    return [SystemMessage(DIAGNOSE_PROMPT), HumanMessage(f"Facts:\n{context}")]


def decide_messages(context: str, diagnosis: Optional[Diagnosis], projected_cents: int, proposed_cents: int) -> list:
    cause = f"{diagnosis.cause}: {diagnosis.evidence}" if diagnosis else "unknown (the cause could not be determined)"
    return [SystemMessage(DECIDE_PROMPT), HumanMessage(
        f"Facts:\n{context}\n\nDiagnosis: {cause}\n"
        f"Projected spending by month end: ${projected_cents / 100:,.2f}\n"
        f"Proposed limit for next month: ${proposed_cents / 100:,.2f}")]
