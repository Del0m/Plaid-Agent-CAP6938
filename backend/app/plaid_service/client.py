import httpx
from .. import config

# the only module that talks to plaid (architecture.md section 4.2)

# raised when plaid returns an error; carries plaid's error fields, never the request body
class PlaidError(Exception):
    def __init__(self, status_code: int, error_code: str, error_message: str):
        super().__init__(f"{error_code}: {error_message}")
        self.status_code = status_code
        self.error_code = error_code
        self.error_message = error_message

# POST to plaid with our credentials added; the body holds the secret, so never log it
def plaid_post(path: str, body: dict) -> dict:
    if not config.PLAID_CLIENT_ID or not config.PLAID_SECRET:
        raise RuntimeError("PLAID_CLIENT_ID / PLAID_SECRET are not set in .env")

    resp = httpx.post(
        f"https://{config.PLAID_ENV}.plaid.com{path}",
        json={"client_id": config.PLAID_CLIENT_ID, "secret": config.PLAID_SECRET, **body},
        timeout=30,
    )
    try:
        data = resp.json()
    except ValueError:
        raise PlaidError(resp.status_code, "NON_JSON_RESPONSE", f"HTTP {resp.status_code} from {path}")

    if resp.status_code != 200:
        raise PlaidError(resp.status_code, data.get("error_code"), data.get("error_message"))
    return data

# link token for the frontend's Link widget
# client_user_id must be a stable random id, never an email (architecture.md section 7)
def create_link_token(client_user_id: str) -> str:
    body = {
        "client_name": "Plaid Agent",
        "language": "en",
        "country_codes": ["US"],
        "user": {"client_user_id": client_user_id},
        # link only shows banks supporting every entry in products, so liabilities
        # goes here instead: enabled where the bank has it, without hiding banks that don't
        "products": ["transactions"],
        "required_if_supported_products": ["liabilities"],
        # fixed when the item is created; the initial budget needs ~90 days (section 6.3)
        "transactions": {"days_requested": 90},
    }
    # items only get SYNC_UPDATES_AVAILABLE webhooks if a URL is set when they're created
    if config.PLAID_WEBHOOK_URL:
        body["webhook"] = config.PLAID_WEBHOOK_URL
    data = plaid_post("/link/token/create", body)
    return data["link_token"]

# trade the short-lived public_token from Link for the permanent access_token
def exchange_public_token(public_token: str) -> tuple[str, str]:
    data = plaid_post("/item/public_token/exchange", {"public_token": public_token})
    return data["access_token"], data["item_id"]

# sandbox only: get a public_token without the Link UI (First Platypus Bank by default)
def sandbox_public_token(institution_id: str = "ins_109508") -> str:
    options = {"transactions": {"days_requested": 90}}
    if config.PLAID_WEBHOOK_URL:
        options["webhook"] = config.PLAID_WEBHOOK_URL
    data = plaid_post("/sandbox/public_token/create", {
        "institution_id": institution_id,
        "initial_products": ["transactions", "liabilities"],
        "options": options,
    })
    return data["public_token"]
