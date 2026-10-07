# Pentair pump (RS-485 over TCP) for Home Assistant

A small custom integration that talks directly to a Pentair IntelliFlo / WhisperFlo
variable-speed pump over RS-485, through a serial-to-TCP bridge (an Elfin EW11 here).
Built for a pool whose controller is a Compool, which can switch the pump's power but
can't set its speed, and which no existing Home Assistant integration speaks to.

## What it does

- Polls the pump every 10 s (or straight after a change): speed, watts, running, error.
- **Control** select: *Pump keypad schedule* (read-only, the pump runs its own programs)
  or *Home Assistant* (holds remote control and runs the pump at `number` target speed /
  `switch` run).
- **Speed floor**: `min_speed_entity` is a sensor giving the minimum RPM right now
  (0 = none); the pump is held at or above it, taking remote control even in keypad mode.
  Keep the rules in a template sensor so they reload without a restart.
- `pentair_pump.ensure_speed` raises the pump to `heat_min_rpm` and waits for it.

## Configuration (configuration.yaml)

```yaml
pentair_pump:
  host: 192.168.20.8          # the RS-485 bridge
  port: 9801
  address: 0x60               # pump address 1 at the keypad
  heat_min_rpm: 2800
  power_entities: [switch.pool_pool, switch.pool_spa]   # circuits that power the drive
  min_speed_entity: sensor.pool_pump_minimum_speed
  packet_log: true            # default
```

## Logs

- `config/pentair_pump_packets.log` (rotating, 5 MB x 3): every frame sent and received,
  decoded and in hex, in the spirit of nodejs-poolController's packet log.
- Home Assistant log, INFO: connections, taking/releasing remote control, command changes
  with their reason, missed replies and recovery. Set the logger
  `custom_components.pentair_pump` to `debug` to see the packets there too.
- `sensor.pool_pump_command`: what's being sent and why; its history is a timeline of commands.

## Protocol notes

Frame `FF 00 FF A5 00 <dst> <src> <action> <len> <data> <sum hi> <sum lo>`, checksum = byte
sum from `A5`. We act as remote controller `0x21`. Status (`0x07`) answers without remote
control; changing speed needs `0x04 FF` (remote), `0x06 0A/04` (run/stop) and
`0x01 02 C4 <rpm hi> <rpm lo>`, refreshed every cycle. `0x04 00` hands the pump back to its keypad.
Only one master may talk to the pump: don't run nodejs-poolController against the same bus.

Changes to this code or to the `pentair_pump:` YAML need a Home Assistant restart.
