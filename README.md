# Pentair pump (RS-485 over TCP) for Home Assistant

Control a **Pentair IntelliFlo / WhisperFlo variable-speed pump directly from Home Assistant**,
with no Pentair automation controller, over a cheap RS-485-to-Wi-Fi/Ethernet bridge such as an
Elfin EW11.

## Why this exists

Pentair VS pumps are everywhere, but Home Assistant can only talk to them **through a Pentair
controller**:

| Option | What it needs | Fits a pump on its own? |
|---|---|---|
| ScreenLogic / IntelliCenter integrations (built in) | a Pentair EasyTouch / IntelliTouch / IntelliCenter controller | No |
| [nodejs-poolController](https://github.com/tagyoureit/nodejs-poolController) (njsPC) | a separate Node service, plus MQTT or a HACS bridge into HA | Yes, but it's a whole second pool controller to run and configure |
| **This integration** | an RS-485 bridge on the pump's COM port | **Yes. It's one small integration inside HA** |

It was written for a pool run by a **Compool** controller. The Compool switches the pump's
power, but it can't set a variable-speed pump's RPM, and no existing integration speaks to the
pump itself. So the pump just ran its own keypad programs, invisible to Home Assistant.

That's a common situation: a Pentair VS pump added to a pool whose automation is Compool,
Jandy, Hayward or a plain timer. In that setup you usually want three things:

- **See** the pump: RPM, watts, running or stopped, error codes.
- **Schedule** it from Home Assistant, e.g. around time-of-use electricity rates.
- **Tie its speed to everything else HA knows about.** The pump should be at ≥ 2800 rpm while the
  heater is actually firing, ≥ 1800 while a water feature runs, and so on, even if that state
  comes from a different integration.

njsPC can drive the pump too, and it's mature and excellent. But if your controller isn't Pentair,
njsPC only runs the pump, and the rules about the heater, water features and so on still live in
HA. You'd be running and syncing two systems to control one motor. This integration keeps it
in one place. If you have a Pentair controller, or want njsPC's dashPanel UI, use njsPC or the
official integrations instead. Only one thing may talk to the pump at a time.

## What it does

- Polls the pump every 5 s (and straight after any change) for speed, watts, state, error,
  drive state and clock.
- **Two control modes** (`select`):
  - *Pump keypad schedule:* read-only. The pump runs its own programs.
  - *Home Assistant:* holds the pump's remote control and runs it at the target speed (`number`)
    with run/stop (`switch`). Drive it from a `schedule` helper, automations or a dashboard slider.
- **Speed floor:** point `min_speed_entity` at a sensor that gives the minimum RPM right now
  (0 = none). The pump is held at or above it, taking control even in keypad mode. Keep the
  rules in a template sensor and they reload without a restart, for example:

  ```yaml
  template:
    - sensor:
        - name: Pool pump minimum speed
          unit_of_measurement: rpm
          state: >-
            {{ 2800 if is_state('binary_sensor.heater_firing', 'on') or is_state('switch.cleaner', 'on')
               else 1800 if is_state('switch.waterfall', 'on') else 0 }}
          attributes:
            reasons: >-
              {{ [is_state('binary_sensor.heater_firing', 'on') and 'heater firing',
                  is_state('switch.cleaner', 'on') and 'cleaner',
                  is_state('switch.waterfall', 'on') and 'waterfall'] | select | list }}
  ```

- **Power awareness:** if your controller switches the pump's power, list those circuits in
  `power_entities`. The pump then shows "no power" instead of a fault when they're off.
- **Hand-back:** leaving Home Assistant control, or shutting HA down, releases the pump to its
  keypad, so it falls back to its own programs.
- **Service `pentair_pump.ensure_speed`:** raises the pump to `heat_min_rpm` and waits until it
  gets there, e.g. before turning a heater on.

### Entities

| Entity | |
|---|---|
| `sensor.pool_pump_speed` | RPM (0 when stopped or unpowered) |
| `sensor.pool_pump_power` | watts |
| `sensor.pool_pump_status` | running / stopped / no power / offline / error N, with diagnostics as attributes |
| `sensor.pool_pump_command` | what HA is sending and why. Changes only when the command does, so its history is a command timeline |
| `number.pool_pump_target_speed` | Home Assistant's speed. Setting it starts the pump at that speed |
| `switch.pool_pump` | run / stop under Home Assistant control |
| `select.pool_pump_control` | Pump keypad schedule / Home Assistant |
| `binary_sensor.pool_pump_connected` | the bridge and pump are answering |
| `binary_sensor.pool_pump_heat_interlock` | a speed floor is active ("high-speed hold") |

## Hardware

- Any RS-485 ⇄ TCP bridge in **transparent TCP-server** mode at **9600 8N1**. An Elfin EW11 works
  well; set its socket to TCP Server and note the port.
- Wire the bridge's RS-485 A/B to the pump drive's COM port data pair. Check your drive's manual:
  cable colours vary between drives, and swapping A/B is harmless if it doesn't answer.
- On the pump keypad: address 1, and "Ext. Control Only" off.

## Configuration

```yaml
pentair_pump:
  host: 192.168.20.8          # the RS-485 bridge
  port: 9801
  address: 0x60               # pump address 1 at the keypad = 0x60
  heat_min_rpm: 2800          # used by ensure_speed and heat_entities
  power_entities: [switch.pool_pool, switch.pool_spa]   # optional
  min_speed_entity: sensor.pool_pump_minimum_speed      # optional
  packet_log: true            # default
```

Changes to this YAML, or to the integration's code, need a Home Assistant restart. The speed
rules live in your template sensor, so they don't.

## Logs (modelled on njsPC)

- **Packet log:** `config/pentair_pump_packets.log`, rotating at 5 MB × 3. Every frame sent and
  received, decoded and in hex:

  ```
  21:51:45.352 TX HA→pump1 set speed 1500 rpm | ff 00 ff a5 00 60 21 01 04 02 c4 05 dc 02 d2
  21:51:45.401 RX pump1→HA ack set → 1500 | ff 00 ff a5 00 21 60 01 02 05 dc 02 0a
  21:51:46.472 RX pump1→HA status running 1500 rpm 124 W err 0 drive 2 clock 21:58 | ff 00 ff a5 …
  ```

- **HA log, at `info`:** connections, taking or releasing remote control, and every command
  change with its reason (`Run 2800 rpm (minimum 2800 rpm for heater firing) — was …`).

  ```yaml
  logger:
    logs:
      custom_components.pentair_pump: info
  ```

## Protocol

Frame: `FF 00 FF A5 00 <dst> <src> <action> <len> <data…> <sum hi> <sum lo>`, where the
checksum is the byte sum from `A5`. We act as remote controller `0x21`, as njsPC does.

| Action | Data | Meaning | Pump's reply |
|---|---|---|---|
| `0x04` | `FF` / `00` | take remote control / release to keypad | mirrors the request |
| `0x06` | `0A` / `04` | run / stop | mirrors the request |
| `0x01` | `02 C4 hi lo` | set speed (RPM register `0x02C4`) | echoes the RPM |
| `0x07` | — | status request; works without remote control | 15 bytes: run, mode, drive state, watts, RPM, …, error, …, clock |

Each cycle under HA control sends `04 FF`, `06`, `01` (each with 1 s timeout, 2 tries, and only
an acknowledging reply accepted), waits 1 s for the drive to settle, then reads status. The
timings and acknowledgement rules follow njsPC's implementation, which this was checked
against.

## Limitations

- One pump, variable-speed (VS) only. VF/VSF flow control isn't implemented.
- YAML configuration only (no config flow). Entity names are fixed ("Pool pump …").
- **Only one master on the bus.** Don't run njsPC, a Pentair controller or another script
  against the same pump.

Not affiliated with or endorsed by Pentair. Protocol knowledge comes from the community,
especially nodejs-poolController.
