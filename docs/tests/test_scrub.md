# Tests: `backend/tests/test_scrub.py`

These tests cover `app/privacy/scrub.py`, the regex pass the privacy gateway runs over the only free-text fields it sends to the LLM: account names and merchant names (architecture section 7, step 3). Run them from `backend/`:

```bash
../venv/bin/python -m pytest tests/test_scrub.py -v
```

These are pure functions, so there are no fixtures or helpers.

---

## `test_emails`

**What it does:** Checks that `scrub_text` turns `jane.doe@example.com` inside a sentence into `[email]` and leaves the rest of the sentence alone.

**Why it's there:** Email is on the "never sent" list. It can turn up in merchant strings for online payments and in the labels users give their own accounts.

## `test_phones`

**What it does:** Parametrized over `(407) 555-1234`, `407-555-1234` and `+1 407 555 1234`. Each should become exactly `[phone]`.

**Why it's there:** Phone numbers are on the "never sent" list and show up in merchant strings ("SQ *JOE 407-555-1234"). The phone pattern runs before the digit-run pattern; otherwise `1234` would be replaced on its own, leaving `(407) 555-[number]`, which is still most of a phone number.

## `test_digit_runs`

**What it does:** A 16-digit card number and a 4-digit account suffix both become `[number]`, but `Route 66` is left alone.

**Why it's there:** Account and card numbers, and masks, never go to the LLM. Four digits is the cutoff because masks are 4 digits, while 1 to 3 digit numbers are usually part of a business name.

## `test_p2p_counterparties`

**What it does:** Parametrized over Zelle, Venmo, PayPal and Cash App (in capitals). The word or two after "to" or "from" becomes `[person]`, so "PayPal to Jane Doe Smith" keeps only "Smith".

**Why it's there:** A person-to-person transfer's merchant string is a person's name. Section 7 calls out "Zelle to FIRSTNAME" by name. The pattern takes at most two words so it can't swallow the rest of a long string. The PayPal case documents that limit, and a three-word name leaks its last word.

## `test_account_possessive`

**What it does:** Checks that `scrub_account_name` turns "Jane's Checking" into "[person]'s Checking".

**Why it's there:** Users label accounts with their own or a family member's name. Those labels are their own, so a capitalized possessive there is a person.

## `test_merchant_keeps_brand_possessive`

**What it does:** Checks that `scrub_merchant` leaves "McDonald's" unchanged.

**Why it's there:** The possessive rule would wreck merchant names ("[person]'s"), so it's limited to account names. The model needs real merchant names to give advice like "cancel Hulu" (section 7 allows merchant names).

## `test_harmless_strings_survive`

**What it does:**
- `None` passes through every function.
- `""` stays `""`.
- "7-Eleven" is left alone.
- "Store #12345" becomes "Store #[number]".

**Why it's there:** Merchant names are often missing (`None`) or contain a short number. A scrub that crashed on `None` or mangled ordinary names would break the gateway or confuse the model.

## `test_done_when_account_name`

**What it does:** Scrubs "Jane Doe's Checking ••4321" and checks that "Jane", "Doe" and "4321" are all gone.

**Why it's there:** This is the account name the gateway's done-when test uses. Pinning it here means a scrub regression fails close to its cause, not only in the end-to-end gateway test.

---

## Not covered

- **Names that don't follow a pattern.** A merchant string like "Jane Doe Photography" or an account named "Mom" passes through unchanged. Regex can't recognize arbitrary names, which is why the gateway's allowlist, not this scrub, is the main defense.
- **Non-US phone formats and names that aren't title case.** Out of scope for Sandbox data. Add cases if real data shows them.
