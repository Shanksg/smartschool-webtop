#!/usr/bin/env python3
"""Test whether loginByBio can mint webTokens unattended.

RESULT on the account this was built against (2026-09-03): NOT AVAILABLE.
The login page renders no remember-me checkbox, and the browser reported
cookieKeys={"uniqueId":true,"deviceId":false,"bioLogin":false,
"SavedUser":false} with an empty IndexedDB - no credential exists to replay.
Kept as a diagnostic: other institutions may provision it, and this script
tells you in one run whether yours does.

If it works, the manual daily paste goes away: one browser login with
"remember me" produces a long-lived credential (365 days) that the monitor
can replay to get fresh 9-hour tokens forever.

Setup
-----
1. Log in at https://webtop.smartschool.co.il with **remember me** checked.
2. In the DevTools Console on that tab, run the snippet printed by:
       python3 bio_test.py --snippet
3. Save the JSON it prints to config/bio_credentials.json
4. Run:  python3 bio_test.py

Read-only apart from writing a fresh token to config/token.txt on success.
"""

import json
import sys
from pathlib import Path

from loguru import logger

from smartschool.client import WebtopClient
from smartschool.config import Paths
from smartschool.exceptions import ApiError, RequestFailed, TokenExpired

SEP = "=" * 72

SNIPPET = r"""
(async () => {
  const db = await new Promise((res, rej) => {
    const r = indexedDB.open('WebTop', 1);
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  const tx = db.transaction('storage', 'readonly').objectStore('storage');
  const all = await new Promise(res => { const q = tx.getAll(); q.onsuccess = () => res(q.result); });
  const out = {};
  for (const k of ['bioLogin','deviceId','uniqueId','SavedUser']) {
    const row = all.find(r => r.key === k);
    out[k] = row ? row.value : null;
    if (row && row.expireDate) out[k + '_expireDate'] = row.expireDate;
  }
  out.selectedUser = localStorage.getItem('selectedUser');
  out.wt_uid = localStorage.getItem('wt_uid');
  out.isMobile = false;
  console.log(JSON.stringify(out, null, 2));
})();
"""


def _redact(v, keep=10):
    s = "" if v is None else str(v)
    return f"{s[:keep]}...({len(s)} chars)" if len(s) > keep * 2 else s or "(empty)"


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    if "--snippet" in sys.argv:
        print("Run this in the DevTools Console on the WebTop tab,")
        print("then save the JSON output to config/bio_credentials.json:\n")
        print(SNIPPET)
        return 0

    paths = Paths()
    cred_file = paths.config_dir / "bio_credentials.json"

    print(SEP)
    print("loginByBio - can we mint tokens without a captcha?")
    print(SEP)

    if not cred_file.exists():
        print(f"\nFAIL: {cred_file} not found.")
        print("Run `python3 bio_test.py --snippet` for the extraction snippet.")
        return 2

    try:
        creds = json.loads(cred_file.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"\nFAIL: {cred_file} is not valid JSON: {e}")
        return 2

    bio = creds.get("bioLogin")
    device_id = creds.get("deviceId")
    unique_id = creds.get("uniqueId") or creds.get("wt_uid")
    selected_user = creds.get("selectedUser") or creds.get("SavedUser") or ""
    is_mobile = bool(creds.get("isMobile"))

    print("\nCredentials loaded:")
    for label, val in [
        ("bioLogin", bio),
        ("deviceId", device_id),
        ("uniqueId", unique_id),
        ("selectedUser", selected_user),
    ]:
        print(f"   {label:14} {_redact(val)}")
    for k in creds:
        if k.endswith("_expireDate"):
            print(f"   {k:14} {creds[k]}   <-- how long the credential itself lasts")

    if not bio:
        print("\nFAIL: bioLogin is null/missing.")
        print("      Log in again with 'remember me' checked - that is what")
        print("      creates it - then re-extract.")
        return 3
    if not device_id:
        print("\nWARNING: deviceId missing; trying anyway.")

    # A token is not needed to call loginByBio - that is the whole point - but
    # the client wants one, so start from whatever we have (possibly expired).
    existing = ""
    if paths.token_file.exists():
        existing = paths.token_file.read_text(encoding="utf-8").strip()

    ok = False
    for mode, label in (("2", "remember-me path (loadRememberMe)"),
                        ("1", "biometric path")):
        print(f"\n{SEP}\nparam1={mode}: {label}\n{SEP}")
        client = WebtopClient(existing)
        try:
            token = client.login_by_bio(
                bio_login=bio,
                device_id=device_id or "",
                selected_user=selected_user,
                unique_id=unique_id or "",
                is_mobile=is_mobile,
                mode=mode,
            )
        except ApiError as e:
            print(f"   FAIL - status=false, errorDescription={e.error_description!r}")
            client.close()
            continue
        except TokenExpired:
            print("   FAIL - HTTP 401 (the endpoint wants a live session too)")
            client.close()
            continue
        except RequestFailed as e:
            print(f"   FAIL - {str(e)[:160]}")
            client.close()
            continue

        if not token:
            print("   FAIL - responded status=true but no token")
            client.close()
            continue

        print(f"   PASS - got a token, {len(token)} chars: {_redact(token, 14)}")
        print(f"   validating it independently...")
        probe = WebtopClient(token)
        alive = probe.check_token()
        print(f"   CheckToken says valid: {alive}")
        if alive:
            students = probe.get_students()
            print(f"   InitDashboard: {len(students)} student(s)")
            paths.config_dir.mkdir(parents=True, exist_ok=True)
            paths.token_file.write_text(token, encoding="utf-8")
            print(f"   wrote the fresh token to {paths.token_file}")
            ok = True
        probe.close()
        client.close()
        if ok:
            break

    print(f"\n{SEP}\nVERDICT\n{SEP}")
    if ok:
        print("  loginByBio WORKS -> unattended token renewal is possible.")
        print("  The monitor can mint its own tokens; no more daily pasting.")
    else:
        print("  loginByBio did not yield a usable token.")
        print("  Most likely the credential is bound to the browser (WebAuthn /")
        print("  device fingerprint) and cannot be replayed from Python.")
        print("  Fallback stays: one paste per school day, with a warning")
        print("  TOKEN_WARN_MINUTES before expiry.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
