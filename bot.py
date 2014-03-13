#    Python IRC bot for restreaming afreeca Brood War streamers to twitch.
#    Copyright (C) 2014 Tomokhov Alexander <alexoundos@ya.ru>
#
#    This program is free software; you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation; either version 3 of the License, or
#    (at your option) any later version.
#  
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#  
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.


import json
import irc
from irc.client import SimpleIRCClient
import sys
import logging
import re
import os
from multiprocessing import Process, Manager, active_children
from subprocess import Popen
import time
import requests
from lxml import etree
from datetime import datetime
import random
import stat
import atexit
import psutil
import signal

VERSION = "2.1.6"
ACTIVE_BOTS = 4


TWITCH_POLL_INTERVAL = 14*60 # seconds
ONLINE_LIST_INTERVAL = 3 # minutes
SUPERVISOR_INTERVAL = 3 # seconds

TWITCH_KRAKEN_API = "https://api.twitch.tv/kraken"
TWITCH_V3_HEADER = { "Accept": "application/vnd.twitchtv.v3+json" }

# some enums for afreeca_database
NICKNAME_ = 0
RACE_ = 1


STREAM = []
TL_API = None
DEFILER_API = None
TWITCH_IRC_SERVER = None
FALLBACK_IRC_SERVER = None
RTMP_SERVER = None
LIVESTREAMER_OPTIONS = None
FFMPEG_OPTIONS = None
RETRY_COUNT = None
AUTOSWITCH_START_DELAY = None
PV_DEVNULL_INTERVAL = 3
PV_PIPE_INTERVAL = 7
VOTE_TIMER = None
DEBUG = [] # "chat,stdout,logfile"
TEST = False

manager = Manager()
pids = {}
mpids = {}
toggles = {}
status = {}
onstream_responses = []
votes = {}
voted_users = []
addons = manager.dict({})
dummy_videos = manager.list([])

_stream_id = None
_stream_pipe = None
_stream_pipel = None
_stream_rate_file = None

conn = None

all_commands = None
afreeca_database = None
modlist = None
help_for_commands = None
forbidden_players = None



def load_settings(filename):
    global STREAM, TWITCH_IRC_SERVER, RTMP_SERVER, LIVESTREAMER_OPTIONS, FFMPEG_OPTIONS, RETRY_COUNT, TEST
    global AUTOSWITCH_START_DELAY, VOTE_TIMER, DEBUG, FALLBACK_IRC_SERVER, TL_API, DEFILER_API
    global PV_DEVNULL_INTERVAL, PV_PIPE_INTERVAL
    
    with open(filename, 'r') as hF:
        settings = json.load(hF)
    
    TWITCH_IRC_SERVER = settings["TWITCH_IRC_SERVER"]
    FALLBACK_IRC_SERVER = settings["FALLBACK_IRC_SERVER"]
    RTMP_SERVER = settings["RTMP_SERVER"]
    LIVESTREAMER_OPTIONS = settings["LIVESTREAMER_OPTIONS"]
    FFMPEG_OPTIONS = settings["FFMPEG_OPTIONS"]
    RETRY_COUNT = settings["RETRY_COUNT"]
    AUTOSWITCH_START_DELAY = settings["AUTOSWITCH_START_DELAY"]
    VOTE_TIMER = settings["VOTE_TIMER"]
    PV_DEVNULL_INTERVAL = settings["PV_DEVNULL_INTERVAL"]
    PV_PIPE_INTERVAL = settings["PV_PIPE_INTERVAL"]
    TL_API = settings["TL_API"]
    DEFILER_API = settings["DEFILER_API"]
    DEBUG = settings["DEBUG"]
    TEST = settings["TEST"]
    dummy_videos[:] = settings["DUMMY_VIDEOS"]
        
    
    STREAM = []
    STREAM.append(None)
    for stream_settings in settings["STREAM"]:
        STREAM.append(stream_settings)
        if TEST:
            #STREAM[-1]["channel"] = stream_settings["test_channel"]
            STREAM[-1]["stream_key"] = stream_settings["test_stream_key"]
    if TEST:
        TWITCH_IRC_SERVER = settings["TEST_IRC_SERVER"]


def load_afreeca_database(filename):
    with open(filename, 'r') as hF:
        return json.load(hF)
    
def load_modlist(filename):
    with open(filename, 'r') as hF:
        return frozenset(json.load(hF) + [iter_stream["nickname"] for iter_stream in STREAM[1:ACTIVE_BOTS+1]])


def load_help_for_commands(filename):
    with open(filename, 'r') as hF:
        help_for_commands = json.load(hF)
        return help_for_commands

def load_forbidden_players(filename):
    with open(filename, 'r') as hF:
        return frozenset(json.load(hF))


def latin_match(strg, search=re.compile(r'[^a-zA-Z0-9_ -.]').search):
    return not bool(search(strg))


def debug_send(text):
    if DEBUG == []:
        return
    if "logfile" in DEBUG:
        if debug_send.logfiledescriptor is None or not os.path.isfile("log%d" % _stream_id):
            if debug_send.logfiledescriptor is not None:
               debug_send.logfiledescriptor.close()
            
            try:
                debug_send.logfiledescriptor = open("log%d" % _stream_id, 'a')
            except Exception as x:
                print("exception occured while trying to open logfile: " + str(x))
                print(text)
                return
        print( datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + ' ' + text
             , file=debug_send.logfiledescriptor
             )
        debug_send.logfiledescriptor.flush()
    if "chat" in DEBUG:
        if conn is not None and conn.connection.is_connected() and "stdout" not in DEBUG:
            conn.msg("[dbg_from_chat] " + text)
        else:
            print("[dbg] " + text)
    if "stdout" in DEBUG:
        print("[dbg] " + text)
debug_send.logfiledescriptor = None


def start_multiprocess(target, args=()):
    if target.__name__ not in mpids:
        #print("warning, %s is not in mpids" % target.__name__)
        debug_send("warning, %s is not in mpids" % target.__name__)
    mprocess = Process(target=target, args=args)
    mprocess.start()
    mpids[target.__name__] = mprocess.pid

def spawn_and_wait(expire_secs):
    def function(target):
        def do_mprocess(*args):
            if target.__name__ not in mpids:
                #print("warning, %s is not in mpids" % target.__name__)
                debug_send("warning, %s is not in mpids" % target.__name__)
            if not pid_alive(mpids[target.__name__]):
                mprocess = Process(target=target, args=args)
                mprocess.start()
                mpids[target.__name__] = mprocess.pid
                mprocess.join(expire_secs)
                if mprocess.is_alive():
                    conn.msg("warning, [%s] mprocess takes too long to proceed, terminating" % \
                    target.__name__)
                    
                    mprocess.terminate()
                    if mprocess.is_alive():
                        conn.msg("problem, [%s] is still alive inside multiprocessing wrapper, killing" % \
                        target.__name__)
                        os.kill(mprocess.pid, 9)
                        return False
                return True
            else:
                conn.msg("warning, [%s] mprocess is already running" % target.__name__)
                return False
        return do_mprocess
    return function


def pid_alive(pid):
    try:
        if pid is not None and psutil.pid_exists(pid) and psutil.Process(pid).status != psutil.STATUS_ZOMBIE:
            return True
        else:
            return False
    except Exception as x:
        debug_send("exception occurred while checking pid status: " + str(x))
        return False

def terminate_pid_p(pids_process):
    #debug_send("about to terminate [%s] process" % pids_process)
    try:
        if pids[pids_process] is None:
            debug_send("error, process [%s] is None" % pids_process)
            return False
    
        pid = pids[pids_process]
        if pids_process == "ffmpeg":
            os.kill(pid, 15)
        else:
            os.killpg(pid, 15)
    except Exception as x:
        debug_send("Exception while terminating %d pid (%s): " % (pid if pid is not None else -1, pids_process) + str(x))
        return False
    
    # waiting a few seconds to terminate
    counter = 10
    time.sleep(0.01)
    while pid_alive(pid) and counter >= 0:
        counter -= 1
        time.sleep(0.3)
    
    if pid_alive(pid):
        conn.msg("warning, couldn't terminate [%s] process" % pids_process)
        return False
    else:
        return True

def kill_pid_p(pids_process):
    try:
        if pids[pids_process] is None:
            debug_send("error, process [%s] is None" % pids_process)
            return False
        
        pid = pids[pids_process]
        if pids_process == "ffmpeg":
            os.kill(pid, 9)
        else:
            os.killpg(pid, 9)
    except Exception as x:
        #debug_send("Exception while killing %d pid:" + str(x) % pid)
        debug_send("Exception while killing %d pid (%s): " % (pid, pids_process) + str(x))
    
    # waiting a few seconds to kill
    counter = 10
    time.sleep(0.01)
    while pid_alive(pid) and counter >= 0:
        counter -= 1
        time.sleep(0.3)
    
    if pid_alive(pid):
        conn.msg("warning, couldn't kill [%s] process" % pids_process)
        return False
    else:
        return True

def terminate_pid_m(mpids_process):
    #debug_send("about to terminate [%s] mprocess" % mpids_process)
    if mpids[mpids_process] is None:
        debug_send("error, mprocess [%s] is None" % mpids_process)
        return False
    
    mpid = mpids[mpids_process]
    try:
        os.kill(mpid, 15)
    except Exception as x:
        debug_send("Exception while terminating %d mpid:" % (mpid) + str(x))
        return False
    
    # waiting a few seconds to terminate
    counter = 10
    time.sleep(0.01)
    while pid_alive(mpid) and counter >= 0:
        counter -= 1
        time.sleep(0.3)
    
    if pid_alive(mpid):
        conn.msg("warning, couldn't terminate [%s] mprocess" % mpids_process)
        return False
    else:
        return True

def kill_pid_m(mpids_process):
    #debug_send("about to kill [%s] mprocess" % mpids_process)
    if mpids[mpids_process] is None:
        debug_send("error, mprocess [%s] is None" % mpids_process)
        return False
    
    mpid = mpids[mpids_process]
    try:
        os.kill(mpid, 9)
    except Exception as x:
        debug_send("Exception while killing %d mpid:" % (mpid) + str(x))
        return False
    
    # waiting a few seconds to kill
    counter = 10
    time.sleep(0.01)
    while pid_alive(mpid) and counter >= 0:
        counter -= 1
        time.sleep(0.3)
    
    if pid_alive(mpid):
        conn.msg("warning, couldn't kill [%s] mprocess" % mpids_process)
        return False
    else:
        return True


class IRCClass(SimpleIRCClient):
    def __init__(self, server, port, nickname, password, channel):
        SimpleIRCClient.__init__(self)
        self.server = server
        self.port = port
        self.nickname = nickname
        self.password = password
        self.channel = channel
        self.connect(self.server, self.port, self.nickname, self.password)
        
    
    def on_welcome(self, connection, event):
        if irc.client.is_channel(self.channel):
            connection.join(self.channel)
            # connecting to other bots' channels:
            for iter_channel in [iter_stream["channel"] for iter_stream in STREAM[1:ACTIVE_BOTS+1]]:
                if iter_channel != self.channel:
                    connection.join(iter_channel)
            self.msg("Bot online (v%s)" % VERSION)
            on_title(["--quiet", "temporary snipealot%d" % _stream_id])
            time.sleep(1)
            if not pid_alive(mpids["stream_supervisor"]):
                toggles["stream_supervisor__on"] = True
                stream_supervisor__mprocess = Process(target=stream_supervisor, args=())
                stream_supervisor__mprocess.start()
                mpids["stream_supervisor"] = stream_supervisor__mprocess.pid
    
    def on_disconnect(self, connection, event):
        debug_send("\ndisconnected from %s\n" % connection.server)
        time.sleep(10)
        if not self.connection.is_connected():
            #on_switch_irc([])
            #if len(args) > 0:
                #print("connecting to %s server" % args[0])
                #conn.connect(args[0], 6667, conn.nickname, "")
                
            if conn.connection.server == TWITCH_IRC_SERVER["address"]:
                print("connecting to %s server" % FALLBACK_IRC_SERVER["address"])
                conn.connect( FALLBACK_IRC_SERVER["address"], FALLBACK_IRC_SERVER["port"]
                            , conn.nickname, ""
                            )
            else:
                print("connecting to %s server" % TWITCH_IRC_SERVER["address"])
                conn.connect( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                            , conn.nickname, conn.password
                            )
        #self.connect(FALLBACK_IRC_SERVER["address"], self.port, self.nickname, self.password)
        #on_restartbot([])
        
    def on_pubmsg(self, connection, event):
        message = event.arguments[0]
        
        # receiving onstream response from other bots
        if "twitch.tv/snipealot" in message and event.source.nick.replace('#', '') in \
        [iter_stream["nickname"] for iter_stream in STREAM[1:ACTIVE_BOTS+1]]:
            onstream_responses[int(event.source.nick[-1])] = True
        
        # receiving commands
        elif message[0] == '!' and len(message) > 1:
            if message[1:] == "onstream" and not pid_alive(mpids["on_onstream"]):
                on_onstream__mprocess = Process(target=on_onstream, args=(event.target,))
                on_onstream__mprocess.start()
                mpids["on_onstream"] = on_onstream__mprocess.pid
            elif event.target == self.channel:
                if not len(message) <= 96:
                    self.msg("error, command string exceeds 96 characters")
                    return
                cmd_string = message[1:]
                
                
                Command = cmd_string.split()[0]
                arguments = cmd_string.split()[1:]
                
                # should be investigated more... are there security pitfalls here
                #if not latin_match(cmd_string) and Command != "title":
                    #self.msg("error, your command string contains non-latin characters")
                    #return
                
                if event.source.nick.lower() in modlist:
                    if Command in all_commands:
                        try:
                            if all_commands[Command](arguments) == False:
                                self.msg("error, incorrect command arguments")
                                if Command in help_for_commands:
                                    self.msg(help_for_commands[Command])
                        except Exception as x:
                            self.msg("internal error occurred while running %s command: %s" % \
                            (Command, str(x)))
                    else:
                        self.msg("error, illegal command")
                elif Command in user_commands:
                    try:
                        if user_commands[Command](arguments) == False:
                            self.msg("error, incorrect command arguments")
                            if Command in help_for_commands:
                                self.msg(help_for_commands[Command])
                    except Exception as x:
                        self.msg("internal error occurred while running %s user command: " + str(x))
        
        
        # receiving vote
        elif toggles["voting__on"] and event.target == self.channel:
            try:
                if message.lower() in votes.keys() and event.source.nick not in voted_users:
                    voted_player = message.lower()
                    for player in votes.keys():
                        if voted_player == player:
                            votes[player] += 1
                            voted_users.append(event.source.nick)
                            break
            except Exception as x:
                self.msg("internal error occurred while processing a vote: " + str(x))
    
    #receive ping -> try to connect to twitch server (if not connected to it)
    #bad experience, ping is received right after connecting, so this method is useless
    #def on_ping(self, connection, event):
        #if conn.connection.server != TWITCH_IRC_SERVER["address"]:
            ##on_switch_irc([])
            #conn.connection.quit()
    
    
    def msg(self, message):
        for line in message.split('\n'):
            if self.connection.is_connected():
                self.connection.privmsg(self.channel, line)
            else:
                print("not connected, redirected message here: " + message)


def on_onstream(request_channel):
    for i in range(len(onstream_responses)):
        onstream_responses[i] = False
    
    if _stream_id != 1:
        time.sleep(0.3)
        counter = 30*(_stream_id - 1)
        while counter > 0:
            if onstream_responses[_stream_id - 1] == True:
                #conn.connection.privmsg(request_channel, ("onstream_response[%d] == True" % (_stream_id - 1)))
                break
            else:
                counter -= 1
                time.sleep(0.1)
    
    onstream_value = status["player"]
    if status["player"] != "<idle>" and not pid_alive(pids["pv_to_pipe"]):
        if RETRY_COUNT - toggles["livestreamer__on"] > 1:
            onstream_value = "reconnecting to " + status["player"]
        else:
            onstream_value = "switching to " + status["player"]
    
    conn.connection.privmsg( request_channel
                           , "twitch.tv/snipealot%d   %s" % (_stream_id, onstream_value)
                           )


def on_help(args):
    if len(args) == 0:
        conn.msg("Available commands for moderators:")
        toprint = set(help_for_commands.keys()).intersection(mod_commands.keys())
        conn.msg(", ".join(sorted(toprint, key=lambda s: s.lower())))
        conn.msg("For additional help, type \"!help <command>\"; available commands for non-moderators: " + \
                 ", ".join(user_commands))
    elif len(args) == 1:
        if args[0].lstrip('!') in help_for_commands:
            conn.msg(help_for_commands[args[0].lstrip('!')])
        else:
            conn.msg("there is no help message for such command")
            return True
    else:
        return False


def on_vote(args):
    if len(args) < 2 and args[0] != "--all":
        return False
    
    if args[0] == "--all":
        vote_set = None
    else:
        vote_set = frozenset([p.lower() for p in args])
        if not vote_set.issubset([iter_dict[NICKNAME_].lower() for iter_dict in afreeca_database.values()]) or \
        vote_set.intersection(forbidden_players):
            conn.msg("error, one of the specified players does not exist in database or forbidden")
            return True
    
    
    def on_voting():
        voted_player = voting(vote_set)
        if voted_player == -1:
            # was aborted or already running
            return
        elif voted_player == None:
            if status["player"] == "<idle>":
                conn.msg("no votes received and stream is idle, picking random streamer")
                on_setplayer([random.choice(list(vote_set))])
            else:
                conn.msg("no votes received, keeping the current player")
        elif voted_player != status["player"].lower():
            # careful here, zombifies vote process?
            on_setplayer([voted_player])
    
    start_multiprocess(on_voting)



def voting(vote_set=None):
    if toggles["voting__on"]:
        conn.msg("error, another voting process is already running")
        return -1
    
    toggles["voting__on"] = True
    
    if vote_set is None:
        online_set = online_fetch()
        if online_set == -1:
            # i.e. couldn't get online list
            conn.msg("couldn't start vote for all online players")
            return -1
        elif len(online_set) < 2:
            conn.msg("less than 2 streamers available for voting, aborting")
            return -1
        vote_set = online_set
    
    vote_set = frozenset([p.lower() for p in vote_set])
    
    
    
    def clear_votes():
        for player in votes.keys():
            votes[player] = 0
    
    voted_users[:] = []
    
    
    counter = VOTE_TIMER
    report_interval = 20
    conn.msg("vote started, %d seconds remaining" % counter)
    time.sleep(report_interval)
    while counter >= 0 and toggles["voting__on"]:
        counter -= report_interval
        conn.msg("vote in progress, %d seconds remaining, (%s)" % \
         (counter, ', '.join(["%s: %d" % (k2,v2) for k2,v2 in votes.items() \
         if (vote_set is None or k2 in vote_set) and v2 > 0])))
        if counter >= report_interval:
            time.sleep(report_interval)
        else:
            time.sleep(counter)
            break
    if toggles["voting__on"] == False:
        conn.msg("voting is aborted!")
        return -1
    else:
        toggles["voting__on"] = False
    
    
    if vote_set is None:
        these_votes = votes
    else:
        #these_votes = lambda votes, vote_set: {key: votes[key] for key in vote_set}
        these_votes = {key: votes[key.lower()] for key in vote_set}
    #print(str(vote_set))
    #print(str(these_votes))
    
    max_votes = max(these_votes.values())
    if max_votes > 0:
        conn.msg("vote ended, "+', '.join(["%s: %d" % (k2,v2) for k2,v2 in these_votes.items() if v2 > 0]))
        winners = [k for k, v in these_votes.items() if v == max_votes]
        if len(winners) == 1:
            clear_votes()
            return winners[0]
        else:
            conn.msg("there are several winners, selecting random winner")
            clear_votes()
            return random.choice(winners)
    else:
        conn.msg("vote ended")
        clear_votes()
        return None


def stream_supervisor():
    print("streaming supervisor launched")
        
    def autoswitch():
        if pid_alive(mpids["livestreamer"]) or toggles["voting__on"]:
            autoswitch.waitcounter = AUTOSWITCH_START_DELAY
        else:
            autoswitch.waitcounter -= SUPERVISOR_INTERVAL
        
        if autoswitch.waitcounter <= 0:
            autoswitch.waitcounter = AUTOSWITCH_START_DELAY
            # getting onstream_set
            statuses = []
            for iter1_status in get_statuses()[1:]:
                if iter1_status is not None:
                    statuses.append(iter1_status)
            onstream_set = frozenset([iter2_status["status"]["player"] for iter2_status in statuses])
            
            # getting online_set
            online_set = online_fetch(quiet=True)
            
            if online_set == -1 or len(online_set) == 0:
                # means that couldn't fetch the online list
                return
            else:
                
                if (len(online_set) > 0 and len(online_set - onstream_set - forbidden_players) == 0):
                    choice_set = online_set
                else:
                    choice_set = online_set - onstream_set - forbidden_players
                # doubful code... #############
                #if not pid_alive(mpids["ffmpeg"]) and toggles["streaming__enabled"]:
                    #conn.msg("online streamers are available, supervisor starts streaming to twitch...")
                    #on_startstream([])
                    ## waiting at max 30 seconds for ffmpeg process to start
                    #counter = 30
                    #while not pid_alive(pids["ffmpeg"]) and counter >= 0:
                        #counter -= 1
                        #time.sleep(1)
                ####################
                if len(choice_set) == 1:
                    conn.msg("stream was idle for more than %d seconds, switching to %s" % \
                     (AUTOSWITCH_START_DELAY, list(choice_set)[0]))
                    on_setplayer([list(choice_set)[0]])
                else:
                    conn.msg( "stream was idle for more than %d seconds, autoswitch started, "
                              "select from the following players:\n%s" % \
                              (AUTOSWITCH_START_DELAY, ", ".join(choice_set)) )
                    voted_player = voting(choice_set)
                    if voted_player == -1:
                        return
                    elif voted_player != None:
                        on_setplayer([voted_player])
                    else:
                        conn.msg("no votes received, picking random streamer")
                        on_setplayer([random.choice(list(choice_set))])
    
    autoswitch.waitcounter = AUTOSWITCH_START_DELAY
    
    
    def twitch_stream_online_supervisor():
        twitch_stream_online_supervisor.counter -= SUPERVISOR_INTERVAL
        if twitch_stream_online_supervisor.counter <= 0:
            twitch_stream_online_supervisor.counter = TWITCH_POLL_INTERVAL
            if not twitch_stream_online():
                conn.msg("wtf, twitch reports \"offline\" status for the stream, ooh... restarting stream")
                on_restartstream([])
                return False
            else:
                print(">>>>> twitch reports online status for the stream")
        return True
            
    
    def dummy_video_loop():
        while pid_alive(pids["ffmpeg"]) and pid_alive(mpids["ffmpeg"]) and \
        pid_alive(mpids["stream_supervisor"]) and toggles["dummy_video_loop__on"]:
            
            global dummy_videos
            if len(dummy_videos) == 0:
                dummy_videos = [ f for f in os.listdir("dummy_videos/") \
                                 if f[-3:] == ".ts" and os.path.isfile("dummy_videos/"+f) ]
                
            
            
            dummy_videofile = "dummy_videos/" + random.choice(dummy_videos)
            if os.path.isfile(dummy_videofile):
                debug_send("* using \"%s\" video file" % dummy_videofile)
            else:
                debug_send("fatal error, video file \"%s\" is not found" % dummy_videofile)
                time.sleep(3)
                toggles["dummy_video_loop__on"] = False
                return
            
            try:
                if not stat.S_ISFIFO(os.stat(_stream_pipe).st_mode):
                    conn.msg("error, stream pipe file is invalid")
                    return
            except Exception as x:
                conn.msg("error, stream pipe file is invalid: " + str(x))
                toggles["dummy_video_loop__on"] = False
                return
            
            dummy_video_loop__cmd = "cat \"" + dummy_videofile + "\" > " + _stream_pipe
            dummy_video_loop__process = Popen(dummy_video_loop__cmd, preexec_fn=os.setsid, shell=True)
            pids["dummy_video_loop"] = dummy_video_loop__process.pid
            dummy_video_loop__process.wait()
        toggles["dummy_video_loop__on"] = False
    
    
    def antispam(just_check=False):
        if (datetime.now() - antispam.blocktime).seconds > 34*60: # if not blocked for 34 minutes
            if just_check:
                return True
            antispam.lastmsglist.append(datetime.now())
            if (datetime.now() - antispam.lastmsglist[0]).seconds > 7*60: # for the last 7 minutes
                # removing one element older than 7 minutes
                antispam.lastmsglist.pop(0)
            if len(antispam.lastmsglist) > 3:
                # for the last 3 minutes there were more than 3 dummy video messages
                conn.msg("afreeca is lagging too hardcore... suppressing dummy video messages")
                antispam.lastmsglist = []
                antispam.blocktime = datetime.now()
                return False
            return True
        else:
            #conn.msg("antispam returns false, antispam.blocktime = %s" % str(antispam.blocktime))
            return False
    antispam.blocktime = datetime.fromtimestamp(0)
    antispam.lastmsglist = []
    
    def dummy_video_supervisor():
        def start_dummy_video():
            toggles["dummy_video_loop__on"] = True
            dummy_video_loop__mprocess = Process(target=dummy_video_loop)
            dummy_video_loop__mprocess.start()
            mpids["dummy_video_loop"] = dummy_video_loop__mprocess.pid
            debug_send("dummy_video_loop started")
        
        if not os.path.isfile(_stream_rate_file) or not pid_alive(mpids["livestreamer"]):
            #print("input rate file doesn't exist")
            if not pid_alive(mpids["dummy_video_loop"]) and pid_alive(mpids["ffmpeg"]):
                # turn on dummy video
                start_dummy_video()
        elif pid_alive(pids["livestreamer"]):
            try:
                hF = open(_stream_rate_file, 'r')
            except Exception as x:
                debug_send("cannot open input rate file: " + str(x))
                return
            
            try:
                for char in hF.read(3):
                    if char.isdigit():
                        if char == '0':
                            pv_to_devnull()
                            #if not pid_alive(mpids["dummy_video_loop"]) and pid_alive(mpids["ffmpeg"]) and \
                            #not toggles["dummy_video_loop__on"]:
                            if not pid_alive(mpids["dummy_video_loop"]) and pid_alive(mpids["ffmpeg"]):
                                # turn on dummy video
                                if antispam():
                                    conn.msg("(afreeca is not responding, starting dummy video)")
                                
                                start_multiprocess(commercial, args=(30,))
                                start_dummy_video()
                        else:
                            # very important part!!!!
                            if toggles["dummy_video_loop__on"]:
                                if pid_alive(pids["dummy_video_loop"]):
                                    # turn off dummy video
                                    toggles["dummy_video_loop__on"] = False
                                    if antispam(just_check=True):
                                        conn.msg("(afreeca started sending stream data, stopping dummy video)")
                                    if terminate_pid_p("dummy_video_loop"):
                                        #time.sleep(0.1)
                                        pv_to_pipe()
                            else:
                                pv_to_pipe()
                        break
            except Exception as x:
                debug_send("exception occurred while reading input rate file: [%d] %s" % (sys.exc_info()[-1].tb_lineno, str(x)))
                toggles["dummy_video_loop__on"] = False
                if pid_alive(pids["livestreamer"]):
                    pv_to_devnull()
            hF.close()
    
    
    # check if all dummy videos exist
    for videofile_relpath in ["dummy_videos/"+videofile for videofile in dummy_videos]:
        try:
            if not os.path.isfile(videofile_relpath):
                conn.msg( "fatal problem, dummy video file \"%s\" does not exist, exiting stream supervisor" \
                          % videofile_relpath )
                return
        except Exception as x:
            conn.msg("exception occured while looking for dummy videos: " + str(x))
            conn.msg("exitting stream supervisor")
            return
    
    
    while toggles["stream_supervisor__on"]:
        if pid_alive(pids["livestreamer"]) and pid_alive(pids["dummy_video_loop"]):
            time.sleep(1)
        else:
            time.sleep(SUPERVISOR_INTERVAL)
        #print("-------------------------------- stream supervisor ping")
        if not pid_alive(mpids["ffmpeg"]):
            if toggles["streaming__enabled"]:
                conn.msg("supervisor starts streaming to twitch...")
                on_startstream([])
                time.sleep(SUPERVISOR_INTERVAL)
                on_refresh(["--quiet"])
                # or retransmit command with corresponding stream_id argument to another bot?
                twitch_stream_online_supervisor.counter = TWITCH_POLL_INTERVAL
            else:
                if 0 < datetime.now().timestamp() % 740 <= SUPERVISOR_INTERVAL:
                    conn.msg("stream is off, moderators can use !startstream")
        else:
            try:
                # checks twitch stream online status every 7*60 seconds
                if not twitch_stream_online_supervisor():
                    continue
            except Exception as x:
                conn.msg("warning, exception occurred while running twitch_stream_online_supervisor(): " + str(x))
            
            try:
                dummy_video_supervisor()
            except Exception as x:
                conn.msg("warning, exception occurred while running dummy_video_supervisor(): " + str(x))
            
            try:
                autoswitch()
            except Exception as x:
                conn.msg("warning, exception occurred while running autoswitch(): " + str(x))
                autoswitch.waitcounter = AUTOSWITCH_START_DELAY



def on_startsupervisor(args):
    if len(args) != 0:
        conn.msg("error, this command doesn't accept any arguments")
        return
    
    if not pid_alive(mpids["stream_supervisor"]):
        toggles["stream_supervisor__on"] = True
        stream_supervisor__mprocess = Process(target=stream_supervisor)
        stream_supervisor__mprocess.start()
        mpids["stream_supervisor"] = stream_supervisor__mprocess.pid
    else:
        conn.msg("error, stream supervisor is already running")


#def on_stopsupervisor(args):
    #return
    #if len(args) > 1 or (len(args) == 1 and args[0] != "-force"):
        #return False
    
    #if pid_alive(mpids["stream_supervisor"]):
        #toggles["stream_supervisor__on"] = False
        #if len(args) == 1 and args[0] == "-force":
            #conn.msg("forcing stream supervisor to close")
            #if not terminate_pid_p("stream_supervisor"):
                #kill_pid_p("stream_supervisor")
    #else:
        #conn.msg("error, stream supervisor is not running")
        #return True


def on_startstream(args):
    ffmpeg_options = FFMPEG_OPTIONS
    
    # first argument -- stream_id
    if len(args) == 1:
        if args[0][0].isdigit() and int(args[0][0]) in range(1, 1+ACTIVE_BOTS):
            conn.connection.privmsg(STREAM[int(args[0][1])]["channel"], "!startstream")
            return True
        else:
            conn.msg("error, invalid stream id")
            return False
    
    # second argument '--', all further are additional ffmpeg options (wasn't tested)
    if len(args) >= 1:
        if args[0] == '--':
            ffmpeg_options = args[1:]
        else:
            return False
    
    
    def ffmpeg():
        conn.msg("launching ffmpeg...")
        
        keep_pipe(_stream_pipe)
        
        # starting ffmpeg to stream from pipe
        ffmpeg__cmd = [ "ffmpeg", "-re", "-i", _stream_pipe ]
        ffmpeg__cmd += [ "-vcodec", "copy", "-acodec", "libmp3lame", "-ab", "128k" ] + ffmpeg_options
        ffmpeg__cmd += [ "-f", "flv", RTMP_SERVER + '/' + STREAM[_stream_id]["stream_key"]]
        # Popen(, cwd="folder")
        print("\n%s\n" % ' '.join(ffmpeg__cmd))
        ffmpeg__process = Popen(ffmpeg__cmd, preexec_fn=os.setsid)
        pids["ffmpeg"] = ffmpeg__process.pid
        ffmpeg__exit_code = ffmpeg__process.wait()
        
        if pid_alive(pids["keep_pipe"]):
            terminate_pid_p("keep_pipe")
        if pid_alive(pids["keep_pipel"]):
            terminate_pid_p("keep_pipel")
        
        debug_send("streaming to twitch ended with code " + str(ffmpeg__exit_code))
    
    
    # check if pipe file exists&valid or create a new one
    if not os.path.exists(_stream_pipe):
        print("streaming pipe doesn't exist, attempting to create one")
        try:
            os.mkfifo(_stream_pipe)
        except:
            conn.msg("error, could not create a pipe for streaming processes")
            return
        else:
            debug_send("created a new pipe, filename: " + _stream_pipe)
    elif not stat.S_ISFIFO(os.stat(_stream_pipe).st_mode):
        conn.msg("file \"%s\" is not a pipe, aborting" % _stream_pipe)
        try:
            os.remove(_stream_pipe)
            os.mkfifo(_stream_pipe)
        except:
            conn.msg("error, could not create a pipe for streaming processes")
            return
        else:
            debug_send("created a new pipe, filename: " + _stream_pipe)
    
    
    # starting ffmpeg streaming mprocess
    if pid_alive(mpids["ffmpeg"]): # and twitch status is not online
        conn.msg("stream is already running")
    else:
        ffmpeg__mprocess = Process(target=ffmpeg)
        ffmpeg__mprocess.start()
        mpids["ffmpeg"] = ffmpeg__mprocess.pid
        toggles["streaming__enabled"] = True


def on_stopstream(args):
    if not (0 <= len(args) <= 1): 
        return False
    
    toggles["streaming__enabled"] = False
    if pid_alive(mpids["livestreamer"]) and on_stopplayer([]) == -1:
        conn.msg("warning, couldn't stop livestreamer")
    
    if pid_alive(mpids["ffmpeg"]) or pid_alive(pids["ffmpeg"]):
        if pid_alive(pids["ffmpeg"]):
            if not terminate_pid_p("ffmpeg"):
                debug_send("trying to kill ffmpeg process...")
                if kill_pid_p("ffmpeg"):
                    debug_send("killed ffmpeg process...")
                else:
                    return -1
    else:
        conn.msg("streaming process is not running")
        return -2


def on_restartstream(args):
    #if not (0 <= len(args) <= 1): 
        #return False
    if len(args) != 0: 
        return False
    
    # if failed during stopping ffmpeg
    if on_stopstream([]) == -1:
        return
    
    time.sleep(1)
    on_startstream([])
    time.sleep(1)
    on_refresh(["--quiet"])


def startplayer(afreeca_id, player):
    def livestreamer(afreeca_id):
        toggles["livestreamer__on"] = RETRY_COUNT
        while toggles["livestreamer__on"]:
            toggles["livestreamer__on"] -= 1
            if os.path.exists(_stream_rate_file):
                try:
                    os.remove(_stream_rate_file)
                except Exception as x:
                    debug_send("exception occurred while deleting %s file: %s" % (_stream_rate_file, str(x)))
            keep_pipe(_stream_pipel)
            pv_to_devnull()
            
            try:
                if not stat.S_ISFIFO(os.stat(_stream_pipel).st_mode):
                    conn.msg("error, stream pipe file is invalid (pipel)")
                    return
            except Exception as x:
                conn.msg(str(x))
                return
            
            livestreamer__cmd = "livestreamer " + ' '.join(LIVESTREAMER_OPTIONS)
            livestreamer__cmd += " afreeca.com/%s best -O > %s" % (afreeca_id, _stream_pipel)
            print("\n%s\n" % livestreamer__cmd)
            livestreamer__process = Popen(livestreamer__cmd, preexec_fn=os.setsid, shell=True)
            pids["livestreamer"] = livestreamer__process.pid
            livestreamer__exit_code = livestreamer__process.wait()
            
            if toggles["livestreamer__on"]:
                #conn.msg("reconnecting to %s (%s)  [%d retries left]" % \
                         #(player, afreeca_id, toggles["livestreamer__on"]))
                pv_to_devnull()
        
        
        # carefull here, if to keep this in loop or not, or is it needed at all?
        if pid_alive(pids["pv_to_devnull"]):
            terminate_pid_p("pv_to_devnull")
        if pid_alive(pids["pv_to_pipe"]):
            terminate_pid_p("pv_to_pipe")
        
        if os.path.exists(_stream_rate_file):
            try:
                os.remove(_stream_rate_file)
            except Exception as x:
                debug_send("exception occurred while deleting %s file: %s" % (_stream_rate_file, str(x)))
        
        
        status["player"] = "<idle>"
        status["afreeca_id"] = None
        
        status["prev_player"] = player
        status["prev_afreeca_id"] = afreeca_id
        dump_status()
        on_title(["--quiet", "temporary snipealot%d" % _stream_id])
        
        # on_tldef
        tldef__mprocess = Process(target=on_tldef, args=(["<idle>",  "--quiet"],))
        tldef__mprocess.start()
        mpids["tldef"] = tldef__mprocess.pid
        
        debug_send("livestreamer ended with code " + str(livestreamer__exit_code))
        
        start_multiprocess(commercial, args=(30,))
        
        if livestreamer__exit_code == 1:
            conn.msg("the afreeca stream appears to be offline or inaccessible")
        elif livestreamer__exit_code == -15:
            conn.msg("afreeca stream playback ended")
        else:
            conn.msg("afreeca stream playback ended with code: " + str(livestreamer__exit_code))
    
    
    if pid_alive(mpids["livestreamer"]) or pid_alive(pids["livestreamer"]):
        conn.msg("error, another afreeca playback stream is already running, wait a bit")
        return True
    
    if not pid_alive(pids["ffmpeg"]):
        conn.msg("error, streaming to twitch is not running")
        return True
    
    
    # check if pipe file exists&valid or create a new one
    if not os.path.exists(_stream_pipel):
        print("streaming pipe doesn't exist, attempting to create one")
        try:
            os.mkfifo(_stream_pipel)
        except Exception as x:
            conn.msg("error, could not create a pipe for streaming processes: " + str(x))
            return
        else:
            debug_send("created a new pipe, filename: " + _stream_pipel)
    elif not stat.S_ISFIFO(os.stat(_stream_pipel).st_mode):
        conn.msg("file \"%s\" is not a pipe, replacing" % _stream_pipel)
        try:
            os.remove(_stream_pipel)
            os.mkfifo(_stream_pipel)
        except Exception as x:
            conn.msg("error, could not create a pipe for streaming processes: " + str(x))
            return
        else:
            debug_send("created a new pipe, filename: " + _stream_pipel)
        
    
    toggles["voting__on"] = False
    conn.msg("switching to " + player)
    
    status["player"] = player
    status["afreeca_id"] = afreeca_id
    dump_status()
    on_title(["--quiet", player])
    
    livestreamer__mprocess = Process(target=livestreamer, args=(afreeca_id,))
    livestreamer__mprocess.start()
    mpids["livestreamer"] = livestreamer__mprocess.pid
    
    # on_tldef
    tldef__mprocess = Process(target=on_tldef, args=([player, "--quiet"],))
    tldef__mprocess.start()
    mpids["tldef"] = tldef__mprocess.pid



def on_stopplayer(args):
    if len(args) != 0:
        conn.msg("error, this command doesn't accept any arguments")
        return
    
    if pid_alive(mpids["livestreamer"]):
        # in case livestreamer pid has not started yet
        counter = 10
        while not pid_alive(pids["livestreamer"]) and counter >= 0:
            counter -= 1
            time.sleep(0.1)
        
        
        if pid_alive(pids["livestreamer"]):
            toggles["livestreamer__on"] = False
            if terminate_pid_p("livestreamer"):
                counter = 40
                while pid_alive(mpids["livestreamer"]) and counter >= 0:
                    counter -= 1
                    time.sleep(0.3)
                
                if not pid_alive(mpids["livestreamer"]):
                    time.sleep(0.1)
                    #conn.msg("afreeca playback stopped")
                else:
                    debug_send("error, [livestreamer] internal process is still running")
                    return -1
            else:
                debug_send("couldn't terminate livestreamer")
                return -1
        else:
            debug_send("fatal error, livestreamer internal process exists without external one")
    else:
        conn.msg("error, afreeca playback is not running")


def keep_pipe(pipe):
    if pipe == _stream_pipe:
        # pipe for ffmpeg to read from
        if not pid_alive(pids["keep_pipe"]):
            keep_pipe__process = Popen("cat > " + pipe, preexec_fn=os.setsid, shell=True)
            pids["keep_pipe"] = keep_pipe__process.pid
    elif pipe == _stream_pipel:
        # pipe for livestreamer to send to
        if not pid_alive(pids["keep_pipel"]):
            keep_pipel__process = Popen("cat > " + pipe, preexec_fn=os.setsid, shell=True)
            pids["keep_pipel"] = keep_pipel__process.pid
    else:
        debug_send("script error, invalid pipe name: " + pipe)



def pv_to_devnull():
    if toggles["pv_to"] == "devnull" or pid_alive(pids["pv_to_devnull"]):
        #print("_____________________[dbg1] pv_to_devnull is already alive")
        return
    if pid_alive(pids["pv_to_pipe"]):
        print("_____________________[dbg1] terminating pv_to_pipe process")
        if not terminate_pid_p("pv_to_pipe"):
            return
            
    toggles["pv_to"] = "devnull"
    _pv_interval = PV_DEVNULL_INTERVAL
    pv_to_devnull__cmd = "pv %s -f -i %d -r 2>&1 1>/dev/null" % (_stream_pipel, _pv_interval)
    pv_to_devnull__cmd += " | while read -d $'\\r' line; do echo $line > %s; done" % _stream_rate_file
    pv_to_devnull__process = Popen(pv_to_devnull__cmd, preexec_fn=os.setsid, shell=True)
    pids["pv_to_devnull"] = pv_to_devnull__process.pid
    #print("\n$$$$(Popen)$$$$$ pv_to_devnull $$$$$$(%d)$$$$$\n" % pv_to_devnull__process.pid)
    debug_send("$$$$(Popen)$$$$$  pv_to_devnull $$$$$$(%d)$$$$$" % pv_to_devnull__process.pid)
    #debug_send("%s > %s" % (_stream_pipel, "/dev/null"))
    toggles["pv_to"] = None

def pv_to_pipe():
    if toggles["pv_to"] == "pipe" or pid_alive(pids["pv_to_pipe"]):
        #print("_____________________[dbg1] pv_to_pipe is already alive")
        return
    if pid_alive(pids["pv_to_devnull"]):
        print("_____________________[dbg1] terminating pv_to_devnull process")
        if not terminate_pid_p("pv_to_devnull"):
            return
    
    try:
        if not stat.S_ISFIFO(os.stat(_stream_pipe).st_mode):
            conn.msg("error, stream pipe file is invalid")
            return
    except Exception as x:
        conn.msg(str(x))
        return
    
    toggles["livestreamer__on"] = RETRY_COUNT
    
    toggles["pv_to"] = "pipe"
    _pv_interval = PV_PIPE_INTERVAL
    pv_to_pipe__cmd = "pv %s -f -i %s -r 2>&1 1>%s" % (_stream_pipel, _pv_interval, _stream_pipe)
    pv_to_pipe__cmd += " | while read -d $'\\r' line; do echo $line > %s; done" % _stream_rate_file
    pv_to_pipe__process = Popen(pv_to_pipe__cmd, preexec_fn=os.setsid, shell=True)
    pids["pv_to_pipe"] = pv_to_pipe__process.pid
    #print("\n****(Popen)**** pv_to_pipe ******(%d)******\n" % pv_to_pipe__process.pid)
    debug_send("****(Popen)**** pv_to_pipe ******(%d)******" % pv_to_pipe__process.pid)
    #debug_send("%s > %s" % (_stream_pipel, _stream_pipe))
    toggles["pv_to"] = None



def on_setplayer(args):
    # !setplayer [-n] <player>
    # where n optional stream id
    if not (1 <= len(args) <= 2):
        return False
    
    if args[0][0] == '-':
        if args[0][1].isdigit() and int(args[0][1]) in range(1, 1+ACTIVE_BOTS):
            if len(args) == 2:
                conn.connection.privmsg(STREAM[int(args[0][1])]["channel"], "!setplayer " + args[1])
                return True
            else:
                return False
        else:
            return False
        
    
    player = args[0].lower()
    afreeca_id = None
    
    for iter_id, iter_dict in afreeca_database.items():
        if iter_dict[NICKNAME_].lower() == player:
            afreeca_id = iter_id
            player = iter_dict[NICKNAME_]
            break
    
    if afreeca_id == None:
        conn.msg("error, such player does not exist in database")
        return True
    elif player in forbidden_players:
        conn.msg("error, this player is forbidden")
        return True
    
    if pid_alive(mpids["livestreamer"]):
        if on_stopplayer([]) == -1:
            return
    startplayer(afreeca_id, player)


def on_setmanual(args):
    if len(args) == 0 or len(args) > 2:
        return False
    if len(args) >= 1:
        afreeca_id = args[0]
        if len(args) == 1:
            if afreeca_id in afreeca_database:
                player = afreeca_database[afreeca_id][NICKNAME_]
            else:
                conn.msg("error, unknown afreeca ID")
                return True
    
    if len(args) == 2:
        player = args[1]
    
    if pid_alive(mpids["livestreamer"]):
        if on_stopplayer([]) == -1:
            return
    startplayer(afreeca_id, player)


def on_refresh(args):
    if len(args) > 0 and args[0] != "--quiet":
        conn.msg("error, this command doesn't accept any arguments")
        return
    
    if status["player"] != "<idle>" and status["afreeca_id"] is not None:
        on_setmanual([status["afreeca_id"], status["player"]])
    elif status["prev_player"] is not None and status["prev_afreeca_id"] is not None:
        on_setmanual([status["prev_afreeca_id"], status["prev_player"]])
    elif "--quiet" not in args:
        conn.msg("error, no player is assigned to this channel and no previous players in history either")


def on_tldef(args):
    if TEST:
        return True
    if not (0 <= len(args) <= 2):
        conn.msg("error, this command accepts maximum of 2 arguments")
        return False
    elif len(args) > 1 and "--quiet" not in args:
        return False
    
    def post_requests():
        # teamliquid.net
        counter = 2
        while counter:
            counter -= 1
            try:
                r = requests.post( TL_API["UPDATE_ADDRESS"] % STREAM[_stream_id]["tl_id"]
                                 , headers = { "Authorization": TL_API["AUTHORIZATION"]
                                             , "Content-Length": str(data_length)
                                             , "Content-Type": "application/x-www-form-urlencoded"
                                             }
                                 , data = data
                                 )
            except Exception as x:
                debug_send( "warning, exception occured while updating player nickname & race at "
                            "teamliquid.net: " + str(x) )
            if r.status_code == 200:
                if "--quiet" not in args:
                    conn.msg("successfully updated player nickname & race at teamliquid.net")
                break
            elif counter == 0:
                conn.msg( "warning, couldn't update player nickname & race at teamliquid.net, "
                          "got %d response" % r.status_code )
                        
        
        # defiler.ru
        counter = 2
        while counter:
            counter -= 1
            try:
                r = requests.post( DEFILER_API["UPDATE_ADDRESS"] % _stream_id
                                 , data = dict(data.items() | DEFILER_API["AUTH_FIELD"].items())
                                 )
            except Exception as x:
                debug_send( "warning, exception occured while updating player nickname & race at "
                            "defiler.ru: " + str(x) )
            if r.status_code == 200:
                if "--quiet" not in args:
                    conn.msg("successfully updated player nickname & race at defiler.ru")
                break
            elif counter == 0:
                conn.msg( "warning, couldn't update streamer nickname & race at defiler.ru, "
                          "got %d response" % r.status_code )
    
    if len(args) == 0:
        player = status["player"]
    else:
        player = args[0]
    
    afreeca_id = None
    for iter_id, iter_dict in afreeca_database.items():
        if iter_dict[NICKNAME_].lower() == player.lower():
            afreeca_id = iter_id
            player = iter_dict[NICKNAME_]
            break
    
    if afreeca_id in afreeca_database:
        data = { "title": player, "race": afreeca_database[afreeca_id][RACE_] }
        data_length = len("title="+player+"&race="+afreeca_database[afreeca_id][RACE_])
    else:
        data = { "title": player }
        data_length = len("title="+player)
    
    
    # is it needed?
    if not toggles["streaming__enabled"]:
        status["player"] = args[0]
        status["afreeca_id"] = None
        dump_status()
    
    tldef__mprocess = Process(target=post_requests, args=())
    tldef__mprocess.start()
    mpids["tldef"] = tldef__mprocess.pid
    
    conn.connection.privmsg("#mca64launcher", "title=%s&race=%s" % \
                            (data["title"], data["race"] if "race" in data.keys() else ""))


def twitch_stream_online():
    TWITCH_HLS_PLAYLIST = "http://usher.twitch.tv/api/channel/hls/{0}.m3u8?token={1}&sig={2}"
    
    try:
        r = requests.get("https://api.twitch.tv/api/channels/{0}/access_token".format(STREAM[_stream_id]["channel"][1:]))
        url = TWITCH_HLS_PLAYLIST.format(STREAM[_stream_id]["channel"][1:], r.json()["token"], r.json()["sig"])
        
        r = requests.get(url)
    except Exception as x:
        conn.msg("warning, improper response from twitch about online status: " + str(x))
        return -1
    
    return True if r.status_code == 200 else False


# is it needed? yes
def on_isbjon(args):
    if len(args) == 0:
        if status["afreeca_id"] is not None:
            afreeca_id = status["afreeca_id"]
        elif status["prev_afreeca_id"] is not None:
            afreeca_id = status["prev_afreeca_id"]
        else:
            conn.msg("no active streamer to check on this channel")
            return
    elif len(args) == 1:
        if args[0][0] == '/':
            # afreeca id is specified
            afreeca_id = args[0][1:]
        else:
            # player nickname is specified
            player = args[0].lower()
            afreeca_id = None
            
            for iter_id, iter_dict in afreeca_database.items():
                if iter_dict[NICKNAME_].lower() == player:
                    afreeca_id = iter_id
            
            if afreeca_id == None:
                conn.msg("error, such player does not exist in database")
                return True
    else:
        return False
    
    start_multiprocess(isbjon, (afreeca_id,))


#def old_isbjon(afreeca_id, quiet=False):
    #user_agent = "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.1; Trident/6.0)"
    #try:
        #r = requests.get( "http://live.afreeca.com:8079/app/index.cgi"
                        #, headers={"User-Agent": user_agent}, params=dict(szBjId=afreeca_id)
                        #)
        #match = re.search(r"<img id=\"broadImg\" src=\".+\/(\d+)\.gif\"", r.text)
    #except Exception as x:
        #conn.msg("warning, improper response from afreeca about broadcast jockey online status: " + str(x))
        #return -1
    
    #if not quiet:
        #conn.msg(afreeca_id + " is " + ("online" if match else "offline"))
    
    #return True if match else False

def isbjon(afreeca_id, quiet=False):
    try:
        xml_tree = etree.parse("http://afbbs.afreeca.com:8080/api/video/get_bj_liveinfo.php?szBjId=" + \
                               afreeca_id)
        
        result = xml_tree.find("result").text
    except Exception as x:
        debug_send("warning, improper response from afreeca about broadcast jockey online status: " + str(x))
        return -1
    
    if not quiet:
        conn.msg(afreeca_id + " is " + ("online" if result == '1' else "offline"))
    
    return True if result == '1' else False


def on_commercial(args):
    if len(args) > 1:
        return False
    
    if len(args) == 1:
        if args[0].isdigit():
            length = int(args[0])
        else:
            conn.msg("parameter must be a number")
            return
    else:
        length = 30
    
    if (datetime.now() - commercial.lastruntime[0]).seconds > 10*60:
        start_multiprocess(commercial, args=(length,))
    else:
        conn.msg("error, interval between commercials is less than 8 minutes")

def commercial(length):
    if (datetime.now() - commercial.lastruntime[0]).seconds <= 10*60:
        return
    
    print("requesting %ds commercial" % length)
    headers = TWITCH_V3_HEADER.copy()
    headers["Authorization"] = "OAuth " + STREAM[_stream_id]["password"]
    
    try:
        r = requests.post( '/'.join([ TWITCH_KRAKEN_API, "channels", STREAM[_stream_id]["channel"][1:]
                                    , "commercial"
                                    ])
                         , headers = headers
                         , data = { "length": length }
                         )
    except Exception as x:
        debug_send("warning, exception occured while sending twitch api request for commerial: " + str(x))
    
    if r.status_code == 204:
        print("--) successful commercial request")
        commercial.lastruntime[0] = datetime.now()
    else:
        conn.msg("unsuccessful response from twitch after sending commercial request: " + \
                 str(r.text.replace('\n', '\\n ')))
commercial.lastruntime = manager.list([datetime.now()])


def on_title(args):
    if len(args) == 0:
        args = [status["player"]]
    
    if args[0] == "--quiet":
        del args[0]
        quiet = True
    else:
        quiet = False
    
    def proceed_on_title():
        headers = TWITCH_V3_HEADER.copy()
        headers["Authorization"] = "OAuth " + STREAM[_stream_id]["password"]
        
        counter = 2
        while counter:
            counter -= 1
            try:
                r = requests.put( '/'.join([TWITCH_KRAKEN_API, "channels", STREAM[_stream_id]["channel"][1:]])
                                , headers = headers
                                , data = { "channel[status]": ' '.join(args) }
                                )
            except Exception as x:
                debug_send( "warning, exception occured while sending twitch api request for "
                            "stream title update: " + str(x) )
            if r.status_code == 200:
                if not quiet:
                    conn.msg("successfully updated twitch stream title")
                break
            elif counter == 0:
                conn.msg("warning, couldn't update stream title, got %d response from twitch" % r.status_code)
    
    start_multiprocess(proceed_on_title)

def on_online(args):
    if len(args) > 1:
        return False
    
    force = False
    if len(args) == 1:
        if args[0] == "--force":
            force = True
        else:
            return False
    
    if not pid_alive(mpids["online_fetch"]):
        start_multiprocess(online_fetch, (force,))
    else:
        debug_send("warning: attempted to start second mprocess for !online")


def online_fetch(force=False, quiet=False):
    @spawn_and_wait(70)
    def proceed_online_fetch():
        try:
            xml_tree = etree.parse( "http://live.afreeca.com:8057/afreeca/broad_list_api.php?szType=xml"
                                  , etree.XMLParser(ns_clean=True, recover=True)
                                  )
            real_broad = xml_tree.find("REAL_BROAD")
        except Exception as x:
            debug_send("exception occurred while getting xml list: " + str(x))
            return
        
        online_bw_pro = []
        
        afreeca_database_fset = frozenset(afreeca_database.keys()) # maybe for better lookup performance
        for bj in real_broad:
            if bj.find("user_id").text in afreeca_database_fset and \
            bj.find("broad_cate_no").text == "00040001":
                online_bw_pro.append(afreeca_database[bj.find("user_id").text][NICKNAME_])
        
        online_bw_pro = sorted(online_bw_pro, key=lambda s: s.lower())
        if not quiet:
            conn.msg("players online: " + ", ".join(online_bw_pro))
        with open("afreeca_online_list.json", 'w') as hF:
            json.dump(online_bw_pro, hF, indent=4, separators=(',', ': '), sort_keys=True)
    
    
    local_datetime = datetime.now()
    local_unixtime = time.mktime(local_datetime.timetuple())
    try:
        TIME_DIFF_M = int((local_unixtime - os.path.getmtime("afreeca_online_list.json"))//60)
    except:
        TIME_DIFF_M = 100500
    
    try:
        with open("afreeca_online_list.json", 'r') as hF:
            online_bw_pro = json.load(hF)
    except Exception as x:
        debug_send(str(x))
    else:
        if TIME_DIFF_M < ONLINE_LIST_INTERVAL and not force:
            if not quiet:
                conn.msg("using recent fetch data (less than %d minutes old)" % ONLINE_LIST_INTERVAL)
                conn.msg("players online: " + ", ".join(sorted(online_bw_pro, key=lambda s: s.lower())))
            return frozenset(online_bw_pro)
    
    if not quiet:
        conn.msg("fetching online streamers list...")
    
    
    #proceed_online_fetch__mprocess = Process(target=proceed_online_fetch)
    #proceed_online_fetch__mprocess.start()
    #mpids["proceed_online_fetch"] = proceed_online_fetch__mprocess.pid
    
    #proceed_online_fetch__mprocess.join(70)
    if proceed_online_fetch():
        try:
            with open("afreeca_online_list.json", 'r') as hF:
                return frozenset(json.load(hF))
        except Exception as x:
            debug_send(str(x))
            return -1
    else:
        debug_send("warning: [proceed_online_fetch] mprocess takes too long to proceed, terminating")
        terminate_pid_m("proceed_online_fetch")
        return -1
        



def on_version(args):
    if len(args) != 0:
        conn.msg("error, this command doesn't accept any arguments")
    conn.msg("Bot version: " + VERSION)




def on_processes(args):
    if len(args) != 0:
        conn.msg("error, this command doesn't accept any arguments")
    
    alive_pids = []
    for pid_name, pid in pids.items():
        if pid_alive(pid):
            alive_pids.append(pid_name)
    conn.msg("alive pids: " + ', '.join(alive_pids))
    
    alive_mpids = []
    for mpid_name, mpid in mpids.items():
        if pid_alive(mpid):
            alive_mpids.append(mpid_name)
    conn.msg("alive mpids: " + ', '.join(alive_mpids))
    
    current_toggles = []
    for Toggle, Toggle_state in toggles.items():
        current_toggles.append("[%s: %s]" % (Toggle, str(Toggle_state)))
    conn.msg("toggles: " + ', '.join(current_toggles))
    
    conn.msg("active children: " + str(active_children()))


def on_killprocess(args):
    if len(args) != 1:
        conn.msg("error, this command needs 1 argument - mprocess name")
        return
    
    process = args[0]
    all_pids = dict(list(pids.items()) + list(mpids.items()))
    if process in all_pids.keys() and pid_alive(all_pids[process]):
        conn.msg("trying to kill [" + process + "] process (" + str(all_pids[process]) + ")")
        try:
            os.kill(all_pids[process], 9)
        except Exception as x:
            debug_send(str(x))
    else:
        conn.msg("error, such process doesn't exist or not alive")


def on_restartbot(args):
    if len(args) != 0:
        return False
    
    conn.msg("going offline...")
    time.sleep(0.2)
    #conn.connection.quit()
    exit()


def on_switch_irc(args):
    #if not (0 <= len(args) <= 1):
        #conn.msg("error, this command accepts only one optional argument - <irc_server_address>")
        #return
    #conn.connect( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                #, conn.nickname, conn.password
                #)
    conn.connection.quit()

def on_irc(args):
    conn.msg("connected to %s IRC server" % conn.connection.server)


def dump_status():
    try:
        with open("status_%d.json" % _stream_id, 'w') as hF:
            json.dump( { "status": dict(status)
                       , "pids": dict(pids)
                       , "mpids": dict(mpids)
                       , "toggles": dict(toggles)
                       #, "votes": dict(votes)
                       }, hF, indent=4, separators=(',', ': '), sort_keys=True )
    except Exception as x:
        conn.msg("error, could not save status file: %s" % str(x))

def get_statuses():
    statuses = [None] * (ACTIVE_BOTS+1)
    for stream_id in range(1, 1+ACTIVE_BOTS):
        try:
            with open("status_%d.json" % stream_id, 'r') as hF:
                statuses[stream_id] = json.load(hF)
        except Exception as x:
            statuses[stream_id] = None
            print("exception occurred while reading status file of bot#%d : %s" % (stream_id, str(x)) )
    
    return statuses

def on_addon_load(args):
    if len(args) != 1:
        conn.msg("error, 1 argument required")
        return
    addon_filename = args[0]
    
    if addon_filename not in os.listdir("addons/"):
        conn.msg("error, such addon does not exist")
        return
    
    debug_send("loading addon: " + addon_filename)
    #try:
        #module = __import__("modules." + module_name, globals())
    #except Exception as x:
        #debug_send("warning, exception occured while loading %s module: %s" % (module_name, str(x)))
        #return
    
    def try_addon():
        try:
            with open("addons/" + addon_filename, 'r') as f:
                exec(f.read(), globals())
        except Exception as x:
            del addons[addon_filename]
            debug_send("exception in %s addon: %s" (addon_filename, str(x)))
    
    #try:
        #start_multiprocess(getattr(module, module_name).start)
    #except Exception as x:
        #debug_send("warning, exception occured in %s module: %s" % (module_name, str(x)))
    
    process = Process(target=try_addon)
    process.start()
    addons.update({addon_filename: process.pid})

def on_addon_unload(args):
    if len(args) != 1:
        conn.msg("error, 1 argument required")
        return
    
    addon_filename = args[0]
    if addon_filename not in addons:
        conn.msg("error, such addon is not loaded")
        return
    
    try:
        kill_child_processes(addons[addon_filename])
        os.kill(addons[addon_filename], 9)
    except Exception as x:
        conn.msg("exception occured while unloading %s addon" % addon_filename)
    
    time.sleep(1) # careful here
    if pid_alive(addons[addon_filename]):
        conn.msg("internal error, addon %s is not unloaded" % addon_filename)
    else:
        del addons[addon_filename]
        conn.msg("addon %s is unloaded" % addon_filename)

def on_addon_reload(args):
    if len(args) != 1:
        conn.msg("error, 1 argument required")
        return
    
    if args[0] not in addons:
        conn.msg("error, such addon is not loaded")
        return
    
    on_addon_unload(args)
    on_addon_load(args)


def on_addons(args):
    if len(args) != 0:
        conn.msg("error, this command doesn't accept arguments")
        return
    
    conn.msg("loaded addons: " + ", ".join([name + (" (%d)" % pid) for name, pid in addons.items()]))


def on_reloadsettings(args):
    if len(args) > 0:
        conn.msg("error, this command doesn't accept any arguments")
        return False
    
    load_settings("settings.json")
    
    global afreeca_database, modlist, all_commands, help_for_commands, forbidden_players
    try:
        afreeca_database = load_afreeca_database("afreeca_database.json")
        modlist = load_modlist("modlist.json")
        all_commands = dict(list(user_commands.items()) + list(mod_commands.items()))
        help_for_commands = load_help_for_commands("help_for_commands.json")
        forbidden_players = load_forbidden_players("forbidden_players.json")
    except Exception as x:
        debug_send("failed to reload configs " + str(x))
        time.sleep(7)
        return
        # idea: combine msg+debug using ": ", but print second part only when debugging is enabled
    
    global votes
    try:
        votes = manager.dict(
                dict.fromkeys([
                iter_dict[NICKNAME_].lower() for iter_dict in afreeca_database.values()
                if iter_dict[NICKNAME_] not in forbidden_players], 0))
        
    except Exception as x:
        if conn is not None:
            conn.msg("fatal error, couldn't reload available votes list, vote system may not work")
            debug_send(str(x))
            return
    
    if conn is not None:
        conn.msg("reloaded settings")



# afreeca ping/mtr command?
mod_commands = { "help": on_help
               , "startstream": on_startstream
               , "commercial": on_commercial
               , "restartstream": on_restartstream
               , "startsupervisor": on_startsupervisor
               #, "stopsupervisor": on_stopsupervisor
               , "setplayer": on_setplayer
               , "setmanual": on_setmanual
               , "stopplayer": on_stopplayer
               , "stopstream": on_stopstream
               , "restartbot": on_restartbot
               , "switch_irc": on_switch_irc
               , "refresh": on_refresh
               , "online": on_online
               , "processes": on_processes
               , "killprocess": on_killprocess
               , "reloadsettings": on_reloadsettings
               , "addons": on_addons
               , "addon_load": on_addon_load
               , "addon_unload": on_addon_unload
               , "addon_reload": on_addon_reload
               , "tldef": on_tldef
               , "vote": on_vote
               , "title": on_title
               , "irc": on_irc
               , "version": on_version
               }

user_commands = { "onstream": on_onstream
                , "isbjon": on_isbjon
                }

def main():
    on_reloadsettings([])
    if len(sys.argv) != 2:
        print("error," "wrong number of arguments")
        print("usage:" "python bot <STREAM_ID>")
        exit()
    if int(sys.argv[1]) not in range(1+len(STREAM)):
        print("error," "wrong stream id")
        print("usage:" "python bot <STREAM_ID>")
        exit()
    
    
    global _stream_id, _stream_pipe, _stream_pipel, _stream_rate_file
    _stream_id = int(sys.argv[1])
    _stream_pipe = "pipe"+str(_stream_id)
    _stream_pipel = _stream_pipe + 'l'
    _stream_rate_file = "stream%d_input_rate" % _stream_id
    
    
    global mpids, pids, toggles, status, voted_users, onstream_responses, addons
    pids = manager.dict({ "dummy_video_loop": None, "keep_pipe": None, "keep_pipel": None, "ffmpeg": None
                        , "livestreamer": None, "pv_to_devnull": None, "pv_to_pipe": None 
                        })
    mpids = manager.dict({ "stream_supervisor": None, "dummy_video_loop": None, "ffmpeg": None
                         , "tldef": None, "livestreamer": None, "online_fetch": None, "proceed_on_title": None
                         , "proceed_online_fetch": None, "on_onstream": None, "on_voting": None
                         , "commercial": None, "isbjon": None
                         })
    toggles = manager.dict({ "stream_supervisor__on": True, "dummy_video_loop__on": False, "pv_to": None
                           , "streaming__enabled": True, "livestreamer__on": False, "voting__on": False
                           })
    status = manager.dict({ "player": "<idle>", "afreeca_id": None
                          , "prev_player": "<idle>", "prev_afreeca_id": None 
                          })
    onstream_responses = manager.list([ False, False, False, False, False, False ])
    voted_users = manager.list([])
    
    global conn
    conn = IRCClass( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                   , STREAM[_stream_id]["nickname"]
                   , "oauth:" + STREAM[_stream_id]["password"] if not TEST else ""
                   , STREAM[_stream_id]["channel"]
                   )
    

    logging.basicConfig(level=logging.DEBUG)
    
    debug_send("========================")
    dump_status()
    
    for addon_filename in os.listdir("addons/"):
        if addon_filename[-3:] == ".py" and os.path.isfile("addons/"+addon_filename):
            on_addon_load([addon_filename])
    
    conn.start()


@atexit.register
def stop_processes():
    print("--------- atexit issued ---------")
    for Toggle in toggles.keys():
        toggles[Toggle] = False
    
    
    for pid_name, pid in pids.items():
        if pid_alive(pid):
            print("terminating [%s] process" % pid_name)
            terminate_pid_p(pid_name)
        #else:
            #print("%s process not alive" % pid_name)
    
    time.sleep(3)
    
    print("waiting additional 7 seconds for mpids to close automatically")
    counter = 7
    while counter >= 0 and all([pid_alive(mpid) for mpid in mpids.values()]):
        counter -= 1
        time.sleep(1)
    
    for mpid_name, mpid in mpids.items():
        if pid_alive(mpid):
            print("killing [%s] multiprocess" % mpid_name)
            kill_pid_m(mpid_name)
        #else:
            #print("%s multiprocess not alive" % mpid_name)    
    
    #pkill -f "/bin/sh -c cat loop0.ts > pipe4"
    
    for pid_name, pid in pids.items():
        if pid_alive(pid):
            print("process [%s] is still alive" % pid_name)
    
    for mpid_name, mpid in mpids.items():
        if pid_alive(mpid):
            print("multiprocess [%s] is still alive" % mpid_name)
    
    kill_child_processes(os.getpid(), 9)
    
    if debug_send.logfiledescriptor is not None:
        print("<<<<<<<===Bot exitted===>>>>>>>\n\n", file=debug_send.logfiledescriptor)
        debug_send.logfiledescriptor.close()
    
    print("stopped")


def kill_child_processes(parent_pid, sig=15):
    try:
        p = psutil.Process(parent_pid)
    except Exception as x:
        debug_send("exception occured while getting process by pid: " + str(x))
        return
    
    child_pid = p.get_children(recursive=True)
    for pid in child_pid:
        os.kill(pid.pid, sig)

def signal_handler(signal, frame):
    print("You pressed Ctrl+C!")
    exit()
signal.signal(signal.SIGINT, signal_handler)

if __name__ == "__main__":
    main()
