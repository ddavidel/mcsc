"""Stand-in for a Minecraft server process, used by the tests.

Prints a startup log, echoes commands, exits on "stop".
Pass "--ignore-stop" to simulate a hung server.
"""

import sys

ignore_stop = "--ignore-stop" in sys.argv

print("Starting minecraft server version 1.21.4", flush=True)
print('[Server thread/INFO]: Done (1.234s)! For help, type "help"', flush=True)

for line in sys.stdin:
    command = line.strip()
    print(f"> {command}", flush=True)
    if command == "stop" and not ignore_stop:
        print("Stopping the server", flush=True)
        sys.exit(0)
