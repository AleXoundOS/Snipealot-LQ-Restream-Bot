

while True:
    time.sleep(7*60)
    if not conn.connection.is_connected() or conn.connection.server != TWITCH_IRC_SERVER["address"]:
        print("connecting to %s server" % TWITCH_IRC_SERVER["address"])
        conn.connect( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                    , conn.nickname, conn.password
                    )
        debug_send("irc_check addon switches to twitch server")
