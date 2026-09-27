"""The simulator: stand-ins for PCANBasic.dll, the OBDLink serial port, and the truck.

It plugs in underneath the safety core, so tests and simulator runs exercise
the same code that runs at the truck. Every CAN ID, local ID, and payload in
here is fictional until real captures replace it.
"""
