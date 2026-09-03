# SmartSchool Homework Monitor

Monitors SmartSchool (Webtop) for new **homework** and new **school messages**,
pushing both to Home Assistant over MQTT plus any Apprise channel (Telegram,
Discord, email, webhooks).

## How authentication works — read this first

**SmartSchool cannot be logged into programmatically.** Login is gated behind a
reCAPTCHA "I'm not a robot" checkbox, verified server-side. Username and
password alone are rejected before the credentials are even checked, so no
script — this one, or any library — can mint a token unattended.

What *does* work: every data endpoint authenticates with a single `webToken`
cookie and needs no captcha.

**Best: unattended renewal.** The monitor can mint its own tokens from a
`bioLogin` credential via `user/loginByBio` (no password, no captcha). The
credential lasts ~1 year, so one browser login keeps the monitor running for a
year with **zero** manual pasting. Set it up once — see
[EXTRACT_BIO.md](EXTRACT_BIO.md). This is the recommended path.

**Fallback: paste a token.** Without a `bioLogin`, paste a `webToken` into
`config/token.txt` (9 hours, or ~6 months with the remember-me trick — see
[Getting a long-lived token](#getting-a-long-lived-token-6-months)). If it dies
you get a Home Assistant notification; write a new value and the daemon picks it
up within one cycle, no restart.

### Token lifetime: 9 hours, or ~6 months with remember-me

A plain `webToken` lasts **9 hours** (issued `15:46:06`, `Expires 00:46:06` —
observed directly). One paste covers a school day.

**But if `saveRememberMe=1` is set before you log in, the token lasts ~6
months** (measured: `Expires 2027-03-02`, ~180 days). This account's login page
does not render a remember-me checkbox, so set the cookie by hand — see
[Getting a long-lived token](#getting-a-long-lived-token-6-months). One paste
then covers half a year, which is as close to set-and-forget as this gets.

The token **cannot be extended**:

- No endpoint (`CheckToken`, `CheckBackgroundToken`, `InitDashboard`,
  `GetMenuCounters`, `RefreshPushTokens`, `getInput`) ever returns a
  `Set-Cookie` with a new `webToken`. `TOKEN_ROTATE_MINUTES` is a *health
  check*, not a refresh — polling more often detects death sooner, it does not
  postpone it.
- `user/ChangeUser` does mint a fresh token from the current cookie, but it
  refused every call after the first and appears to create a *competing*
  session: the original token died early while a second one existed. The
  monitor deliberately never calls it.

So the monitor predicts expiry from `obtained_at + TOKEN_TTL_HOURS` and warns
`TOKEN_WARN_MINUTES` ahead of time, letting you swap the token before a check
fails. The prediction is an **upper bound** — a token already part-way through
its life when pasted will die sooner — so a `401` is still treated as the
authoritative signal.

### Automated renewal (solved 2026-09-03)

The monitor **can** renew itself, via `user/loginByBio` replaying a
browser-registered `bioLogin` credential — verified end to end. Set it up with
[EXTRACT_BIO.md](EXTRACT_BIO.md). What does *not* work, for the record:

| Avenue | Result |
|---|---|
| `LoginByUserNameAndPassword` | reCAPTCHA checkbox, server-verified. Rejects before checking the password. |
| `CheckBackgroundToken` / any endpoint | Never returns `Set-Cookie`. Nothing to refresh. |
| `user/ChangeUser` | Mints a token, but rate-limits and creates a *competing* session that kills the original. |
| Self-registered `writeBio` credential | `writeBio` registers and `isBioExist` confirms it, but `loginByBio` rejects a *self*-registered credential. Only a **browser-registered** `bioLogin` is accepted — which is exactly what [EXTRACT_BIO.md](EXTRACT_BIO.md) captures. The key params were `param5="true"` (isMobile) and an empty `deviceId`. |

Untried: `user/LoginByOneTimePassword` (the SMS/email login button) — no longer
needed now that renewal works.

### One session per account

The web `webToken` is a single session, and re-authenticating this account
elsewhere can invalidate it. A browser/web re-login definitely does (an OTP
re-login killed it during development). Whether the iPhone app does is
**unclear**: the token survived immediately after an app login, but was found
dead hours later with no obvious cause — so treat any re-login on this account
as potentially session-ending. A *different* account never matters.

If the token dies you get the `token_status = expired` notification; redo the
long-lived-token steps. This is also why the monitor never calls `ChangeUser`:
minting a second web token for the same account would kill the one it is using.

## Getting a long-lived token (~6 months)

Do this once. It makes the `webToken` last ~6 months instead of 9 hours.

1. Go to <https://webtop.smartschool.co.il> (log out first if you are in).
2. Open the DevTools Console and run:
   ```js
   document.cookie = "saveRememberMe=1; path=/; domain=.smartschool.co.il"
   ```
   (Chrome makes you type `allow pasting` first.) The cookie has a ~10-minute
   life, so log in right after.
3. Log in — the OTP (SMS/email) button works and needs no captcha.
4. DevTools → Application → Cookies → copy the **`webToken`** value into the
   first line of `config/token.txt`.
5. In the same cookie view, copy the **`webToken` Expires** date onto the
   **second line** of `config/token.txt` (so the monitor predicts expiry
   correctly instead of assuming 9 hours):
   ```
   <the long webToken value>
   2027-03-02T14:03:26.466Z
   ```

The expiry line is optional — without it the monitor assumes 9 hours and warns
early, which is merely conservative, not wrong.

> **One session per account.** Do not log in again elsewhere (phone, another
> browser) while the monitor runs — it invalidates the token. If you must,
> just repeat the steps above.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in NOTIFIERS / MQTT_*
# create config/token.txt as described in
# "Getting a long-lived token" above
```

### Verify the token and the API

```bash
python3 token_test.py
```

This is read-only — it sends no notifications and publishes nothing. It reports
token liveness, the students and class params discovered at runtime, whether
the token rotates, and an A/B of both homework endpoints.

Verified results on 2026-09-03 with a fresh token:

| Check | Result |
|---|---|
| `CheckToken` | token valid |
| `InitDashboard` | 1 student, `classCode=4`, `classNum=1` |
| `CheckBackgroundToken` | responds `status=true`, no rotation |
| `PupilCard/GetPupilLessonsAndHomework` | **works** — 6 days, real homework |
| `dashboard/GetHomeWork` | works, but today only and no per-row dates |

**The February `"view is blocked"` was never a block.** It was a stale request:
`config.yaml` had `classCode=3`, `studyYear=2026` and an old encrypted
`studentID` frozen into it, while the student had moved up a grade. The same
call succeeds with runtime-discovered params. Nothing school-year specific is
hardcoded any more.

### Run

```bash
python3 run.py                 # local process
docker compose up -d --build   # container
```

## Configuration

All runtime settings are environment variables (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `NOTIFIERS` | — | Comma-separated Apprise URLs |
| `MQTT_BROKER` / `MQTT_PORT` | — / `1883` | Home Assistant MQTT discovery |
| `MQTT_USER` / `MQTT_PASS` | — | MQTT credentials |
| `SCHEDULES` | `12:00,16:00,20:00` | Homework check times |
| `TOKEN_ROTATE_MINUTES` | `20` | Session keep-alive interval (stay under 30) |
| `SEED_QUIETLY` | `1` | First sighting records existing homework/messages without notifying |
| `MESSAGES_ENABLED` | `1` | Also monitor the school message inbox |
| `TOKEN_TTL_HOURS` | `9` | Assumed lifetime when `token.txt` has no expiry line (a remember-me token is ~6 months; record its real expiry instead) |
| `TOKEN_WARN_MINUTES` | `45` | Warn this far before the predicted expiry |
| `NO_HOMEWORK_VALUES` | (see below) | Values a teacher types to mean "no homework" |
| `VERIFY_TLS` | `1` | Set `0` only behind a TLS-intercepting proxy |

### Notification behaviour

Any newly detected assignment notifies, **whatever its date**. The endpoint
returns a six-day window (past, today and tomorrow), so a today-only filter —
what this repo used to do — dropped most of it: real homework entered for
yesterday or tomorrow never reached you.

Teachers often type `אין` ("none") instead of leaving the field blank. Those
are treated as *no homework*: `אין`, `אין שיעורי בית`, `לא הוזן`, `ללא`,
`none`, `n/a`, `-`. Override the list with `NO_HOMEWORK_VALUES`.

The first time a student is seen, the visible week is recorded silently so you
do not get a burst of "new" homework that is simply pre-existing.

`config/config.yaml` is **optional**. Students and their class codes are
discovered at runtime from `dashboard/InitDashboard`, so nothing school-year
specific is hardcoded. Use it only to override a display name:

```yaml
students:
  - name: "Display Name"
    student_id: "<encrypted id from token_test.py>"
```

## Message inbox

Alongside homework, the monitor polls `messageBox/GetMessagesInbox` and
notifies about messages it has not seen before. Set `MESSAGES_ENABLED=0` to
turn it off.

Notable details:

- Identity is subject + sender + timestamp, deliberately **excluding** the read
  flag — marking a message read in the browser must not make it look new.
- The inbox is account-level, not per-student, so it gets its own Home
  Assistant device (`SmartSchool - Messages`) rather than hanging off a child.
- An inbox failure never costs you the homework check; they are independent.

## Home Assistant entities

Setup details, automations and a dashboard card are in
[`HOME_ASSISTANT_SETUP.md`](HOME_ASSISTANT_SETUP.md).

Per student, via MQTT discovery:

| Sensor | Meaning |
|---|---|
| Homework Count | due today |
| Homework This Week | everything in the six-day window |
| Homework Upcoming | today and later |
| Homework Details | rendered list; falls back to the week when nothing is due today |
| Last Check | timestamp of the last successful run |
| Token Status | `ok` / `expired` — alert on this to know when to re-paste |
| Token Hours Left | predicted hours until expiry |

Plus a `SmartSchool - Messages` device:

| Sensor | Meaning |
|---|---|
| Messages Unread | unread count |
| Messages Total | messages in the inbox page |
| Latest Message | newest subject |
| Messages Details | rendered list, `*` marks unread |
| Messages Last Check | timestamp of the last inbox poll |

## Layout

```
smartschool/
  client.py      Webtop API client (cookie auth, rotation, endpoint map)
  session.py     token load/save/rotate (hand-pasted vs cached)
  homework.py    extractors for both homework endpoint shapes
  state.py       seen-item tracking (homework and messages)
  notifiers.py   Apprise + Home Assistant MQTT
  monitor.py     scheduler daemon
  config.py      paths + settings
tests/           133 offline tests, no network
token_test.py    live token/API diagnostics
bio_test.py      probes the loginByBio renewal path (closed; see notes)
legacy/          superseded scripts, kept for reference
```

The endpoint map was derived from the SmartSchool web client, reimplemented
synchronously and extended with token renewal and session keep-alive. Password
login is deliberately not used, for the captcha reason above.

## `legacy/`

Kept for reference, not used at runtime:

- `legacy/autologin/` — seven browser-automation login scripts. These cannot
  work: the captcha is mandatory and server-verified.
- `legacy/smartschool_monitor.py`, `legacy/smartschool_monitor_v2.py` — the
  previous single-file monitors, superseded by the `smartschool/` package.
- `legacy/docs/` — earlier overlapping docs, superseded by this README.

## Testing

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

Offline and hermetic — all HTTP is stubbed.
