"""ENTITY-SENSOR-MIB (RFC 3433, 1.3.6.1.2.1.99): chassis temperature.

**This table is usually absent, and that is correct.** The Firewalla Switch SE
is fanless and reports ``temperature: 0`` with ``fanStatus: "none"`` -- it has
no temperature instrumentation. Publishing a literal 0 degrees C would trip
low-temperature alarms on most NMSes and put a permanently flat, meaningless
line on the dashboard.

So the sensor is instantiated only when the API reports a genuinely positive
reading, which would be a future model that actually has a thermal sensor. On
the SE the whole subtree simply does not exist.
"""

from __future__ import annotations

import time

from pysnmp.proto import rfc1902

from . import SwitchContext
from .entity import CHASSIS_IDX

ENT_SENSOR_ENTRY = (1, 3, 6, 1, 2, 1, 99, 1, 1, 1)

SENSOR_TYPE_CELSIUS = 8
SENSOR_SCALE_UNITS = 9
SENSOR_STATUS_OK = 1


def build(tree, ctx: SwitchContext) -> None:
    if ctx.switch.temperature_c is None:
        return  # no thermal instrumentation on this model -- omit entirely

    idx = CHASSIS_IDX
    tree.set(ENT_SENSOR_ENTRY + (1, idx), lambda: rfc1902.Integer(SENSOR_TYPE_CELSIUS))
    tree.set(ENT_SENSOR_ENTRY + (2, idx), lambda: rfc1902.Integer(SENSOR_SCALE_UNITS))
    # Precision 1 -> entPhySensorValue is tenths of a degree.
    tree.set(ENT_SENSOR_ENTRY + (3, idx), lambda: rfc1902.Integer(1))
    tree.set(
        ENT_SENSOR_ENTRY + (4, idx),
        lambda: rfc1902.Integer(int(round((ctx.switch.temperature_c or 0.0) * 10))),
    )
    tree.set(ENT_SENSOR_ENTRY + (5, idx), lambda: rfc1902.Integer(SENSOR_STATUS_OK))
    tree.set(ENT_SENSOR_ENTRY + (6, idx), lambda: rfc1902.OctetString(b"C"))
    tree.set(
        ENT_SENSOR_ENTRY + (7, idx),
        lambda: rfc1902.TimeTicks(ctx.switch.uptime_ticks),
    )
    tree.set(ENT_SENSOR_ENTRY + (8, idx), lambda: rfc1902.Integer(0))
