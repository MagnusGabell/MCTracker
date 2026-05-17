# MCTracker
Minecraft location tracker. Map/trcking and tagging solution for your own minecraft server.

1. Track your location in Overworld, Nether and The End.
2. Show the bioms you have visited
3. Tag location to your liking to be able to revisit.

Install and run the latest minecraft server on Linux. (I run Ubuntu)
Install below requirements.
Modify the server.properties
Run the python script mc-tracker.py

Open a browser and enter the http://<server-ip>:8765. This will show your location in minecraft.

"""
Minecraft Player Tracker
========================
Polls your Minecraft server via RCON every 3 seconds, saves player positions
to players.json, and serves a live map at http://<server-ip>:8765
 
Requirements:
    pip install flask
    pip install Pillow nbtlib numpy   # optional — enables biome/terrain base layer
 
Server setup (server.properties):
    enable-rcon=true
    rcon.password=yourpassword
    rcon.port=25575
"""
