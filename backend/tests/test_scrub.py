import pytest

from app.privacy.scrub import scrub_account_name, scrub_merchant, scrub_text


def test_emails():
    assert scrub_text("pay jane.doe@example.com now") == "pay [email] now"


@pytest.mark.parametrize("phone", ["(407) 555-1234", "407-555-1234", "+1 407 555 1234"])
def test_phones(phone):
    assert scrub_text(phone) == "[phone]"


# 4+ digits look like card/account numbers or masks; short numbers are usually part of a name
def test_digit_runs():
    assert scrub_text("card 4111111111111111") == "card [number]"
    assert scrub_text("acct 4321") == "acct [number]"
    assert scrub_text("Route 66") == "Route 66"


@pytest.mark.parametrize("raw, clean", [
    ("Zelle to Jane Doe", "Zelle to [person]"),
    ("Venmo from Bob", "Venmo from [person]"),
    ("PayPal to Jane Doe Smith", "PayPal to [person]"),
    ("CASH APP TO JANE", "CASH APP TO [person]"),
    ("Zelle payment to Jane Doe", "Zelle payment to [person]"),
    ("Apple Cash to Jane", "Apple Cash to [person]"),
    ("Zelle to Jane Doe 12345678", "Zelle to [person] [number]"),
])
def test_p2p_counterparties(raw, clean):
    assert scrub_merchant(raw) == clean


@pytest.mark.parametrize("raw, clean", [
    ("Jane's Checking", "[person]'s Checking"),
    ("JANE DOE'S CHECKING", "[person]'S CHECKING"),
    ("José's Checking", "[person]'s Checking"),
    ("jane's checking", "[person]'s checking"),
    ("Jane & Bob's Joint", "[person]'s Joint"),
    ("Mary Jane Watson's Savings", "[person]'s Savings"),
    ("Jane’s Checking", "[person]’s Checking"),
])
def test_account_possessive(raw, clean):
    assert scrub_account_name(raw) == clean


# a brand possessive in a merchant name is not a person
def test_merchant_keeps_brand_possessive():
    assert scrub_merchant("McDonald's") == "McDonald's"


def test_harmless_strings_survive():
    assert scrub_merchant(None) is None
    assert scrub_account_name(None) is None
    assert scrub_merchant("") == ""
    assert scrub_merchant("7-Eleven") == "7-Eleven"
    assert scrub_merchant("Store #12345") == "Store #[number]"


# the exact account name from the gateway's done-when test
def test_done_when_account_name():
    out = scrub_account_name("Jane Doe's Checking ••4321")

    assert "Jane" not in out and "Doe" not in out and "4321" not in out


# review on #6: names a word-based match missed. a bare apostrophe after s, hyphens and O' prefixes
@pytest.mark.parametrize("raw, clean", [
    ("James' Checking", "[person]' Checking"),
    ("Mary-Jane's Savings", "[person]'s Savings"),
    ("O'Brien's Checking", "[person]'s Checking"),
    ("THE JONES' JOINT", "[person]' JOINT"),
])
def test_account_possessive_unusual_names(raw, clean):
    assert scrub_account_name(raw) == clean


# review on #6: the counterparty is everything after the service, even past a scrubbed token,
# and with no to/from (bank exports often write "ZELLE JANE DOE" or "Venmo *JaneDoe")
@pytest.mark.parametrize("raw, clean", [
    ("Zelle to Jane 1234 Doe", "Zelle to [person] [number]"),
    ("Zelle to Jane jane@x.com Doe", "Zelle to [person] [email]"),
    ("ZELLE JANE DOE", "ZELLE [person]"),
    ("Venmo *JaneDoe", "Venmo *[person]"),
])
def test_p2p_whole_counterparty(raw, clean):
    assert scrub_merchant(raw) == clean
