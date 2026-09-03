#!/usr/bin/env python3
"""Phase 3 diagnostics - run this right after pasting a fresh webToken.

    python3 token_test.py

Answers, in order:
  1. Is the token accepted at all?
  2. Does InitDashboard give us runtime student params (killing the frozen
     studyYear/periodID)?
  3. ROTATION: does CheckBackgroundToken hand back a fresh webToken?
     -> this decides "log in once, ever" vs "log in every day"
  4. Which homework endpoint actually returns data: dashboard/GetHomeWork
     or the PupilCard one?

Read-only. Sends no notifications and publishes nothing to MQTT.
"""

import json
import sys
from pathlib import Path

from loguru import logger

from smartschool.client import WebtopClient
from smartschool.config import Paths
from smartschool.exceptions import ApiError, RequestFailed, TokenExpired, TokenMissing
from smartschool.homework import extract
from smartschool.session import TokenStore

SEP = "=" * 72


def _redact(token: str) -> str:
    return f"{token[:12]}...({len(token)} chars)"


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    paths = Paths()
    store = TokenStore(paths.config_dir)

    print(SEP)
    print("SmartSchool token diagnostics")
    print(SEP)

    try:
        state = store.load()
    except TokenMissing as e:
        print(f"\nFAIL: {e}")
        return 2

    print(f"\nToken: {_redact(state.token)}")
    print(f"Obtained: {state.obtained_at or 'unknown'}   Rotations so far: {state.rotated_count}")

    client = WebtopClient(state.token)
    results = {}

    # ---------------------------------------------------------------- 1
    print(f"\n{SEP}\n1. Token liveness (dashboard/CheckToken)\n{SEP}")
    try:
        alive = client.check_token()
        print(f"   {'PASS' if alive else 'FAIL'} - CheckToken says token is {'valid' if alive else 'not valid'}")
        results["check_token"] = alive
    except Exception as e:
        print(f"   ERROR: {e}")
        results["check_token"] = False

    # ---------------------------------------------------------------- 2
    print(f"\n{SEP}\n2. Student discovery (dashboard/InitDashboard)\n{SEP}")
    students = []
    try:
        body = client.init_dashboard()
        payload = body.get("data")
        print(f"   PASS - InitDashboard returned data of type {type(payload).__name__}")
        if isinstance(payload, dict):
            print(f"   top-level keys: {list(payload.keys())[:15]}")
        students = client.get_students()
        print(f"   Parsed {len(students)} student(s):")
        for s in students:
            print(
                f"     - name={s.name!r} classCode={s.class_code} "
                f"classNumber={s.class_number} studyYear={s.study_year} "
                f"id={_redact(s.student_id) if s.student_id else None}"
            )
        if not students:
            snippet = json.dumps(payload, ensure_ascii=False)[:700]
            print(f"   NOTE: no students parsed. Raw payload snippet:\n   {snippet}")
        results["students"] = len(students)
    except TokenExpired as e:
        print(f"   FAIL - token expired: {e}")
        return 3
    except (ApiError, RequestFailed) as e:
        print(f"   FAIL - {e}")
        results["students"] = 0

    # ---------------------------------------------------------------- 3
    print(f"\n{SEP}\n3. TOKEN ROTATION (dashboard/CheckBackgroundToken)   <-- the decisive test\n{SEP}")
    try:
        before = client.token
        rot = client.refresh_token()
        print(f"   endpoint ok        : {rot.ok}")
        print(f"   token ROTATED      : {rot.rotated}")
        print(f"   cookie expires     : {rot.expires or 'not sent'}")
        if rot.set_cookie:
            print(f"   Set-Cookie (trunc) : {rot.set_cookie[:200]}")
        else:
            print("   Set-Cookie         : (none returned)")
        if rot.rotated:
            print(f"   old token: {_redact(before)}")
            print(f"   new token: {_redact(rot.new_token)}")
            print("\n   ==> SLIDING SESSION CONFIRMED.")
            print("       One manual login is enough; the daemon can rotate indefinitely.")
        else:
            print("\n   ==> No rotation observed on this call.")
            print("       Either the server rotates only near expiry, or the token has a")
            print("       hard absolute lifetime. Re-run this closer to expiry to confirm.")
        results["rotated"] = rot.rotated
    except TokenExpired as e:
        print(f"   FAIL - token expired: {e}")
        results["rotated"] = False
    except Exception as e:
        print(f"   ERROR: {e}")
        results["rotated"] = False

    # ---------------------------------------------------------------- 4
    print(f"\n{SEP}\n4. Homework endpoint A/B\n{SEP}")
    if not students:
        print("   SKIPPED - no students discovered, cannot build a request")
    else:
        student = students[0]
        for source, call in (
            ("dashboard", lambda: client.get_homework(student)),
            (
                "pupilcard  (this repo)",
                lambda: client.get_homework_pupilcard(
                    {
                        "classCode": student.class_code,
                        "moduleID": 11,
                        "studentID": student.student_id,
                        "studentName": student.name,
                        "studyYear": student.study_year,
                        "viewType": 0,
                        "weekIndex": 0,
                    }
                ),
            ),
        ):
            key = source.split()[0]
            print(f"\n   --- {source} ---")
            try:
                body = call()
                items = extract(body, source=key)
                print(f"   PASS - status=true, extracted {len(items)} homework item(s)")
                for it in items[:5]:
                    print(f"     * [{it.date[:10]}] {it.subject} / {it.teacher}: {it.homework[:70]}")
                if not items:
                    print(f"   raw data snippet: {json.dumps(body.get('data'), ensure_ascii=False)[:500]}")
                results[f"hw_{key}"] = len(items)
            except ApiError as e:
                print(f"   FAIL - status=false, errorDescription={e.error_description!r}")
                results[f"hw_{key}"] = f"blocked: {e.error_description}"
            except TokenExpired as e:
                print(f"   FAIL - token expired: {e}")
                results[f"hw_{key}"] = "expired"
            except RequestFailed as e:
                print(f"   FAIL - {e}")
                results[f"hw_{key}"] = "failed"

    # ---------------------------------------------------------------- 5
    print(f"\n{SEP}\n5. Message inbox (messageBox/GetMessagesInbox)\n{SEP}")
    try:
        messages = client.get_messages_inbox()
        unread = [m for m in messages if not m.has_read]
        print(f"   PASS - {len(messages)} message(s), {len(unread)} unread")
        for m in messages[:5]:
            flag = " " if m.has_read else "*"
            clip = " [attachment]" if m.has_files else ""
            print(f"     {flag} [{(m.sent_at or '')[:16]}] {m.sender}: {m.subject[:50]}{clip}")
        results["messages"] = len(messages)
    except TokenExpired as e:
        print(f"   FAIL - token expired: {e}")
        results["messages"] = "expired"
    except (ApiError, RequestFailed) as e:
        print(f"   FAIL - {e}")
        results["messages"] = "failed"

    # ---------------------------------------------------------------- summary
    print(f"\n{SEP}\nSUMMARY\n{SEP}")
    for k, v in results.items():
        print(f"   {k:24} {v}")

    if results.get("rotated"):
        print("\n   Verdict: rotation works -> set-and-forget is achievable.")
    elif results.get("check_token"):
        print("\n   Verdict: token valid but no rotation seen yet.")
        print("   Next: re-run in ~20 min and again near the 24h mark to see if the")
        print("   server slides the session before expiry.")
    else:
        print("\n   Verdict: token not accepted. Paste a fresh one and re-run.")

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
