import meshtastic
import meshtastic.serial_interface
from meshtastic.protobuf import admin_pb2

iface = meshtastic.serial_interface.SerialInterface(devPath="/dev/ttyUSB0")

p = admin_pb2.AdminMessage()
p.factory_reset_config = 1  # int instead of bool
iface.localNode._sendAdmin(p)
iface.close()