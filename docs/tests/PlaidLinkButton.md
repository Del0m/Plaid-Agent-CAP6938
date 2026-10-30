# Plaid Link and dashboard review checks

This milestone has no frontend unit-test runner yet. Backend regression tests cover response shapes, background sync state, retry visibility, and account grouping. Run `npm run lint` and `npm run build` from `frontend/` for static validation.

Manual UI checks for the review fixes:

1. Link a Sandbox bank. The response returns after storing the connection; the dashboard shows syncing and polls for accounts instead of waiting for the full bank history inside the exchange request.
2. Finish linking, then choose **Link another account** without refreshing. A new link token is fetched and the button stays disabled until it is available.
3. Close Plaid Link, then reopen it. Exiting renews the token as well.
4. Cause a balance or liability sync failure after transactions have loaded. Refresh the page: the retry card remains. A successful retry removes it.
5. Check that account chart bars and totals use the same server-supplied asset/debt classification, including legacy brokerage accounts.

The API's `syncing` flag indicates queued/in-progress work, not durable job scheduling. If the API stops during an initial sync, the persisted item remains visible with Retry sync. Polling is bounded to two minutes; the user can still retry afterwards. Sync locks assume one API worker.
