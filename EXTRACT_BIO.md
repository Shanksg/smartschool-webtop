# Set up unattended token renewal (do this once)

The monitor can renew its own `webToken` from a `bioLogin` credential using
`user/loginByBio` — no password, no captcha. The credential lasts ~1 year, so
one browser login keeps the monitor running for a year with no manual pasting.

Verified working 2026-09-03: replaying a browser-registered `bioLogin` with
`param1="2"`, `param2=""` (empty deviceId) and `param5="true"` (isMobile)
returned a valid token.

## Steps

1. Log in to <https://webtop.smartschool.co.il> in a browser (OTP works and
   needs no captcha). Setting the remember-me cookie first helps the browser
   register a `bioLogin`:
   ```js
   document.cookie = "saveRememberMe=1; path=/; domain=.smartschool.co.il"
   ```
   (run in the DevTools Console, then log in right away).

2. After you land on the dashboard, capture the `bioLogin` the browser
   registered. In the DevTools **Network** tab, find the `loginByBio` request
   and copy its JSON body — it looks like:
   ```json
   {"id": "U2FsdGVkX1...", "param1": "2", "param2": "",
    "param3": "<selectedUser>", "param4": "<uniqueId>", "param5": "true"}
   ```
   (If you do not see `loginByBio`, reload the page while logged in — the app
   calls it on load once a `bioLogin` exists.)

3. Save those into `config/bio_credentials.json` (this file is gitignored):
   ```json
   {
     "bioLogin":     "<id from the request>",
     "deviceId":     "",
     "uniqueId":     "<param4>",
     "selectedUser": "<param3>",
     "isMobile":     true
   }
   ```

4. Verify it works:
   ```bash
   python3 bio_test.py
   ```
   `SUCCESS` means the monitor can now renew itself. From then on you never
   paste a token by hand — `config/token.txt` becomes optional (a convenient
   way to seed the very first token, but the monitor will mint its own).

## When it stops working

The `bioLogin` credential is good for ~1 year. If renewal ever fails (the
credential was revoked, or a year passed) you get the `token_status = expired`
notification — redo the steps above. Everything else is automatic.
