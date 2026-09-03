# Home Assistant Integration

How to get SmartSchool homework and school messages into Home Assistant.
For general setup and how
authentication works, see [`README.md`](README.md) first.

There are two independent paths, and you can run both at once:

| Path | What you get | Config needed in HA |
|---|---|---|
| **MQTT discovery** (recommended) | Sensors appear automatically as a device | none |
| **Apprise webhook** | A push notification for new homework or messages | one automation |

---

## Path 1: MQTT discovery

The monitor publishes Home Assistant [MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
messages, so **you do not write any sensor YAML**. Point it at your broker and
the entities create themselves.

### 1. Requirements

- An MQTT broker (the *Mosquitto broker* add-on is easiest)
- The **MQTT integration** added in HA: *Settings → Devices & Services → Add
  Integration → MQTT*

### 2. Configure the monitor

In your `.env`:

```bash
MQTT_BROKER=192.168.1.100     # your broker's IP or hostname
MQTT_PORT=1883
MQTT_USER=smartschool
MQTT_PASS=your-broker-password
```

Then run it (see `README.md`). On the first check you should see:

```
MQTT connected to 192.168.1.100:1883
Published MQTT discovery for <student>
Published MQTT state for <student>: N item(s) today
Published MQTT discovery for message inbox
Published MQTT message state: N total, M unread
```

### 3. What appears in Home Assistant

**One device per student**, named `SmartSchool - <student name>`:

| Sensor | Value |
|---|---|
| Homework Count | assignments due **today** |
| Homework This Week | everything in the six-day window the API returns |
| Homework Upcoming | today and later |
| Homework Details | rendered text list; falls back to the week when nothing is due today |
| Last Check | timestamp of the last successful run |
| Token Status | `ok` or `expired` |

**Plus one device for the message inbox**, named `SmartSchool - Messages`. The
inbox belongs to the account rather than a child, so it is not attached to a
student:

| Sensor | Value |
|---|---|
| Messages Unread | unread count |
| Messages Total | messages on the current inbox page |
| Latest Message | subject of the newest message |
| Messages Details | rendered list; `*` marks unread |
| Messages Last Check | timestamp of the last inbox poll |

Set `MESSAGES_ENABLED=0` in `.env` to turn inbox monitoring off.

Entity IDs are derived by HA from the sensor names, e.g.
`sensor.smartschool_<student>_homework_count` and
`sensor.smartschool_messages_unread`. Hebrew names get slugified, so check
*Developer Tools → States* for the exact IDs rather than guessing.

### 4. Topics (only if you want to debug)

```
homeassistant/sensor/smartschool_student_<id>_<key>/config   # per-student discovery
smartschool/student_<id>/state                               # per-student state
homeassistant/sensor/smartschool_inbox_<key>/config          # inbox discovery
smartschool/inbox/state                                      # inbox state
```

All retained.

`<id>` is a short hash of the student's name, so Hebrew names stay valid in a
topic. To print yours:

```bash
python3 -c "from smartschool.notifiers import device_id_for; print(device_id_for('YOUR STUDENT NAME'))"
```

State payload:

```json
{
  "count": 0,
  "count_week": 2,
  "count_upcoming": 0,
  "details": "No homework today (2026-09-03). This week:\n\n1. ...",
  "last_check": "2026-09-03T14:06:32.483000",
  "token_status": "ok"
}
```

Retained, so HA repopulates after a restart without waiting for the next check.

---

## Path 2: Apprise notifications

Set `NOTIFIERS` in `.env` to one or more comma-separated
[Apprise](https://github.com/caronc/apprise) URLs.

### Webhook into Home Assistant

Add a webhook automation in `configuration.yaml`:

```yaml
automation:
  - alias: "SmartSchool homework webhook"
    trigger:
      - platform: webhook
        webhook_id: smartschool_homework
        allowed_methods: [POST]
        local_only: false          # only if calling from outside your LAN
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "{{ trigger.json.title }}"
          message: "{{ trigger.json.message }}"
```

Then point the monitor at it:

```bash
# local HA
NOTIFIERS=json://192.168.1.100:8123/api/webhook/smartschool_homework

# via Nabu Casa (https)
NOTIFIERS=jsons://your-instance.ui.nabu.casa/api/webhook/smartschool_homework
```

The monitor POSTs `{"title": ..., "message": ...}`, which is what
`trigger.json.title` / `.message` read above.

### Other channels

```bash
NOTIFIERS=tgram://<bottoken>/<ChatID>                    # Telegram
NOTIFIERS=discord://<webhook_id>/<webhook_token>         # Discord
NOTIFIERS=mqtt://user:pass@192.168.1.100:1883/smartschool/homework
```

Multiple targets are comma-separated and all receive every notification.

> Anything in `NOTIFIERS` is a credential. Keep it in `.env` (gitignored) — not
> in `docker-compose.yml`, and not exported in your shell profile.

---

## Automations worth having

### Alert when the token expires

This is the one automation you genuinely need. The monitor cannot log in by
itself — SmartSchool requires a reCAPTCHA checkbox — so when the token dies it
needs you.

```yaml
automation:
  - alias: "SmartSchool token expired"
    trigger:
      - platform: state
        entity_id: sensor.smartschool_<student>_token_status
        to: "expired"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "SmartSchool token expired"
          message: "Paste a fresh webToken into config/token.txt"
```

To refresh: log in at <https://webtop.smartschool.co.il>, then DevTools (F12) →
*Application* → *Cookies* → copy `webToken` into `config/token.txt`. A running
monitor picks it up within one cycle — no restart.

### Warn if checks stop arriving

Catches a crashed or wedged monitor, which is otherwise silent.

```yaml
automation:
  - alias: "SmartSchool monitor is stale"
    trigger:
      - platform: template
        value_template: >
          {{ (as_timestamp(now())
              - as_timestamp(states('sensor.smartschool_<student>_last_check'), 0)) > 86400 }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          message: "No SmartSchool check in over 24h"
```

### Alert on a new school message

```yaml
automation:
  - alias: "SmartSchool new message"
    trigger:
      - platform: state
        entity_id: sensor.smartschool_latest_message
    condition:
      # ignore the empty-inbox placeholder and HA restarts
      - condition: template
        value_template: >
          {{ trigger.to_state.state not in ['No messages', 'unknown', 'unavailable']
             and trigger.from_state.state != trigger.to_state.state }}
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "New school message"
          message: "{{ states('sensor.smartschool_latest_message') }}"
```

The monitor already pushes an Apprise notification for new messages, so use
this only if you want a separate HA-side alert (a different device, a
different priority) rather than a duplicate.

### Announce new homework on a speaker

```yaml
automation:
  - alias: "Announce new homework"
    trigger:
      - platform: state
        entity_id: sensor.smartschool_<student>_homework_count
    condition:
      - condition: numeric_state
        entity_id: sensor.smartschool_<student>_homework_count
        above: 0
    action:
      - service: tts.google_translate_say
        data:
          entity_id: media_player.living_room
          language: "iw"
          message: "יש שיעורי בית חדשים"
```

---

## Dashboard card

```yaml
type: vertical-stack
cards:
  - type: entities
    title: SmartSchool
    entities:
      - entity: sensor.smartschool_<student>_homework_count
        name: Due today
      - entity: sensor.smartschool_<student>_homework_this_week
        name: This week
      - entity: sensor.smartschool_<student>_last_check
        name: Last check
      - entity: sensor.smartschool_<student>_token_status
        name: Token
  - type: markdown
    content: >
      {{ states('sensor.smartschool_<student>_homework_details') }}
```

The details sensor holds multi-line text, so a `markdown` card renders it far
better than an `entities` row.

---

## Running it as a service

### macOS (launchd)

`~/Library/LaunchAgents/com.smartschool.monitor.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.smartschool.monitor</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/path/to/webtop-smartschool/run.py</string>
    </array>
    <key>WorkingDirectory</key><string>/path/to/webtop-smartschool</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/path/to/webtop-smartschool/logs/launchd.out</string>
    <key>StandardErrorPath</key><string>/path/to/webtop-smartschool/logs/launchd.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.smartschool.monitor.plist
```

launchd does **not** read `.env`, so add an `EnvironmentVariables` dict, or run
via Docker instead.

### Linux (systemd)

```ini
[Unit]
Description=SmartSchool Homework Monitor
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/webtop-smartschool
EnvironmentFile=/opt/webtop-smartschool/.env
ExecStart=/usr/bin/python3 run.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### Docker

Simplest of the three, since compose reads `.env` for you:

```bash
docker compose up -d --build
docker compose logs -f
```

---

## Troubleshooting

**No entities appear in HA**
Confirm the log says `MQTT connected`. Check the MQTT integration is installed
in HA, and that discovery messages are arriving:

```bash
mosquitto_sub -h 192.168.1.100 -u smartschool -P '<pass>' \
  -t 'homeassistant/sensor/smartschool_#' -v
```

**Notifications never arrive**
The log distinguishes the two cases: `Notification delivered` versus
`Apprise reported delivery failure`. On failure, check the `NOTIFIERS` URL and
that the webhook automation exists with a matching `webhook_id`.

**`Homework Count` is 0 but there is homework**
Expected: `count` is *today only*. Use `Homework This Week` or
`Homework Upcoming` for the fuller picture.

**`token_status` is `expired`**
Paste a fresh `webToken` into `config/token.txt` (see above).

**Homework or messages repeat, or are missing**
Check the state files — homework and messages are tracked separately:

```bash
cat config/homework_state.json
cat config/messages_state.json
```

Deleting it makes everything look new again. The first run after deleting seeds
silently by default (`SEED_QUIETLY=1`) so you are not flooded.

**Verify the API directly**

```bash
python3 token_test.py
```

Read-only — sends no notifications and publishes nothing.
