# MCTracker
Minecraft location tracker. Map/tracking and tagging solution for your own minecraft server.

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
Suggested installation procedure
================================
1. Run a linux distribution in WSL or on a physical machine. For physical machine, install linux dist with rufus and the ISO-image for ubuntu desktop or without GUI
 - For windows subsystem for linux -Install WSL via windows "Turn Windows feature on".
 - Once WSL is installed, install linux (ubuntu) via Windows store.
 - Start the terminal with ubuntu linux.
2. When linux is running make sure python is installed. Run this to see if you have python installed: python --version. If version is not displayed go with below:
 - sudo apt-get update
 - sudo apt-get install python
3. Install the requirements for the MC-tracker. (Might need to install pip aswell. Follow pip installation instructions. (google it)
   - pip install flask
   - pip install Pillow nbtlib numpy
4. create a folder, for example mc-tracker so your filepath is: /home/<username>/mc-tracker
5. git clone this repo to the folder
6. start the minecraft server on the same machine "java -Xmx1024M -Xms1024M -jar server.jar" (important is the path to the world-folder)
7. start the mc-tracker. It can start before the server. The important thing is the filepath to where your minecraft server is and that enable-rcon=true is set in the server.property file of minecraft
   - run with: "python3 mc-tracker.py --world /path-to-your-minecraft-world/"
8. Go back to your game machine.  Start minecraft and join the world.
9. Open a browser and enter the IP to your minecraft server including the port to access MC-tracker: http://192.168.0.XXX:8765 (xxx is your server ip)
10. Not the port 8765 is not the rcon port its hard coded in the python program.
11. enjoy. The terrain with bioms will generate over time once you enter them. 
