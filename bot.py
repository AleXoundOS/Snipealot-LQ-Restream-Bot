#    Python IRC bot for restreaming afreeca Brood War streamers to twitch.
#    Copyright (C) 2016 Tomokhov Alexander <alexoundos@ya.ru>
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


import sys
import os
import json
import logging
import re
from multiprocessing import Process, Manager, active_children, Lock, Queue
from subprocess import Popen, PIPE, DEVNULL
import time
from datetime import datetime
import random
import stat
import atexit
import signal
import psutil
from lxml import etree
import requests
from inspect import stack
from irc.client import SimpleIRCClient
import irc

from modules import livestreamer_helper

from modules import afreeca_api
from modules.afreeca_api import isbjon, get_online_BJs
online_fetch = get_online_BJs


VERSION = "2.2.14"
ACTIVE_BOTS = 4


TWITCH_POLL_INTERVAL = 8*60 # seconds
SUPERVISOR_INTERVAL = 0.2 # seconds
MIN_INPUT_RATE = 37*1024 # bytes per second

TWITCH_KRAKEN_API = "https://api.twitch.tv/kraken"
TWITCH_V3_HEADER = { "Accept": "application/vnd.twitchtv.v3+json" }

DUMMY_VIDEOS_PATH = "dummy_videos/2016-07-17"

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
PV_PIPE_INTERVAL = 7
VOTE_TIMER = None
DEBUG = [] # "chat,stdout,logfile"
TEST = False

manager = Manager()
pids = None
mpids = None
toggles = None
status = None
onstream_responses = manager.dict({})
votes = None
voted_users = None
dummy_videos = manager.list([])
commercial_lastruntime = manager.list([datetime.now()])

_stream_id = None
_stream_pipe = None
_stream_pipel = None
_stream_rate_file = None
_stream_buffer_file = None

conn = None

all_commands = None
afreeca_database = None
modlist = None
help_for_commands = None
forbidden_players = None

bjStreamCarriers = manager.dict({})
logfiledescriptor = None
lock_dd = Lock()
lock_logger = Lock()
lock_commercial = Lock()
lock_reconnect = Lock()

def load_settings(filename):
    global STREAM, TWITCH_IRC_SERVER, RTMP_SERVER, LIVESTREAMER_OPTIONS, FFMPEG_OPTIONS, RETRY_COUNT, TEST
    global AUTOSWITCH_START_DELAY, VOTE_TIMER, DEBUG, FALLBACK_IRC_SERVER, TL_API, DEFILER_API
    global PV_PIPE_INTERVAL, MIN_INPUT_RATE
    
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
    PV_PIPE_INTERVAL = settings["PV_PIPE_INTERVAL"]
    TL_API = settings["TL_API"]
    DEFILER_API = settings["DEFILER_API"]
    DEBUG = settings["DEBUG"]
    TEST = settings["TEST"]
    MIN_INPUT_RATE = settings["MIN_INPUT_RATE"]
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
        return frozenset(json.load(hF) + [iter_stream["nickname"] for iter_stream in STREAM[1:]])


def load_help_for_commands(filename):
    with open(filename, 'r') as hF:
        help_for_commands = json.load(hF)
        return help_for_commands

def load_forbidden_players(filename):
    with open(filename, 'r') as hF:
        return frozenset(json.load(hF))


def latin_match(strg, search=re.compile(r'[^a-zA-Z0-9_ -.]').search):
    return not bool(search(strg))


def debug_send(text, tochat=False):
    with lock_logger:
        if DEBUG == []:
            return
        if "logfile" in DEBUG:
            print( datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + ' ' + text
                 , file=logfiledescriptor )
            logfiledescriptor.flush()
        if ("chat" in DEBUG) or tochat:
            if conn is not None and conn.connection.is_connected() and "stdout" not in DEBUG or tochat:
                conn.msg(text)
            #else:
                #print("[dbg] " + text)
        if "stdout" in DEBUG:
            print("[dbg] " + text)


def print_exception(exception, message, tochat=False):
    if message != print_exception.last_message:
        print_exception.last_message = message
    else:
        tochat = False
    
    debug_send( "[%d] Exception occured while %s: %s" % (sys.exc_info()[-1].tb_lineno, message, str(exception))
              , tochat )
print_exception.last_message = ""


def start_multiprocess(target, args=()):
    if target.__name__ not in mpids:
        debug_send("warning, %s is not in mpids" % target.__name__)
    mprocess = Process(target=target, args=args)
    #mprocess.daemon = True
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
        if pid is not None and psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE:
            #print("pid " + str(pid) + " exists!!!!!!!!!!!!!!!!!!, zombie status = " + str(psutil.Process(pid).status() == psutil.STATUS_ZOMBIE))
            return True
        else:
            return False
    except Exception as x:
        print_exception(x, "%s: checking pid status" % pid_alive.__name__)
        return False

def terminate_pid_p(pids_process):
    try:
        if pids[pids_process] is None:
            debug_send("error, process [%s] is None" % pids_process)
            return False
    
        pid = pids[pids_process]
        os.kill(pid, 15)
        
    except Exception as x:
        print_exception(x, "terminating %d pid (%s)" % (pid if pid is not None else -1, pids_process))
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
        os.kill(pid, 9)
        
    except Exception as x:
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

def close_pid_p_gradually(process_name):
    if process_name not in pids or pids[process_name] is None:
        debug_send("error, process [%s] is None" % process_name)
        return False
    
    if not psutil.pid_exists(pids[process_name]):
        return True
    
    try:
        p = psutil.Process(pids[process_name])
        for method in [p.terminate, p.kill]: ## using [] instead of {} preserves order
            if p.is_running() and p.status() != psutil.STATUS_ZOMBIE:
                method()

            try:
                p.wait(timeout=2)
            except:
                pass
            
            if not p.is_running():
                return True
            elif p.status() == psutil.STATUS_ZOMBIE:
                debug_send("%s: warning process \"%s\" is zombie" % (stack()[1][3], process_name))
                return True
            
            debug_send( "%s: warning, process is still alive after using %s method at [%s] process"
                      % (stack()[1][3], method.__name__, process_name) )
    except Exception as x:
        print_exception(x, "%s: closing %s" % (stack()[1][3], process_name))
        return False
    
    return False


def terminate_pid_m(mpids_process):
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
            for iter_channel in [iter_stream["channel"] for iter_stream in STREAM[1:]]:
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
            #if not pid_alive(mpids["chat_connection_track"]):
                #chat_connection_track__mprocess = Process(target=chat_connection_track, args=())
                #chat_connection_track__mprocess.start()
                #mpids["chat_connection_track"] = chat_connection_track__mprocess.pid
    
    def on_disconnect(self, connection, event):
        debug_send("\ndisconnected from %s\n" % connection.server)
        while not self.connection.is_connected():
            time.sleep(10)
            debug_send("conn.connection.quit()")
            conn.connection.quit()
    
    def on_pubmsg(self, connection, event):
        message = event.arguments[0]
        
        # receiving onstream response from other bots
        if "twitch.tv/" in message and \
        event.source.nick in [iter_stream["nickname"] for iter_stream in STREAM[1:]]:
            onstream_responses[event.source.nick] = True
        
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
                            print_exception( x, "running %s command: %s" % (Command, str(x))
                                           , tochat=True )
                    else:
                        self.msg("error, illegal command")
                elif Command in user_commands:
                    try:
                        if user_commands[Command](arguments) == False:
                            self.msg("error, incorrect command arguments")
                            if Command in help_for_commands:
                                self.msg(help_for_commands[Command])
                    except Exception as x:
                        self.msg("internal error occurred while running %s user command: %s" % (Command, str(x)))
        
        
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
    
    def msg(self, message):
        print("going to send \"%s\"" % (message))
        def split_and_send_string(string):
            splitLength = 384 - 2
            index = 0
            while True:
                print("in while True")
                if (len(string) - index <= splitLength):
                    self.connection.privmsg(self.channel, string[index:])
                    break
                
                # first try to find commas in the current portion of text of length <= splitLength and split by them
                posOfComma = string[index:index+splitLength].rfind(',')
                # then spaces
                posOfSpace = string[index:index+splitLength].rfind(' ')
                
                if posOfComma > 0:
                    self.connection.privmsg(self.channel, string[index:index+posOfComma+1])
                    #print(string[index:posOfComma+1])
                    index += posOfComma + 1
                elif posOfSpace > 0:
                    self.connection.privmsg(self.channel, string[index:index+posOfSpace])
                    #print(string[index:posOfSpace])
                    index += posOfSpace
                
                # search for spaces at the beginning of next string portion and ignore them
                while string[index] == ' ':
                    print("in index += 1")
                    index += 1
                
                # if no commas neither spaces found
                if posOfComma < 0 and posOfSpace < 0:
                    self.connection.privmsg(self.channel, string[index:index+splitLength])
                    #print(string[index:index+splitLength])
                    index += splitLength
                
                if (index < len(string)):
                    print("sleeping for 1 second after %d characters in message", splitLength)
                    time.sleep(1)
                else:
                    break
        
        if self.connection.is_connected():
            message_list = message.split('\n')
            lines_count = len(message_list)
            if lines_count > 1:
                for line in message_list:
                    try:
                        split_and_send_string(line)
                    except Exception as x:
                        print_exception(x, "sending privmsg")
                        debug_send(message)
                        break
                    lines_count -= 1
                    if lines_count > 0:
                        #print("sleeping for 1 second after new line in a message")
                        time.sleep(1)
            else:
                try:
                    print("before split_and_send_string")
                    split_and_send_string(message)
                except Exception as x:
                    print_exception(x, "sending privmsg")
                    debug_send(message)
        else:
            print("not connected, redirected message to stdout: " + message)


def chat_connection_track(just_switch=False, reconnection_interval=8):
    # maybe the "conn" inside multiprocess is just a copy, haha
    def switch():
        if not lock_reconnect.acquire(block=False):
            debug_send("one reconnect function is already running")
            return
        
        if conn.connection.server == TWITCH_IRC_SERVER["address"]:
            conn.connection.server = FALLBACK_IRC_SERVER["address"]
            debug_send("connecting to %s server" % FALLBACK_IRC_SERVER["address"])
            conn.connect( FALLBACK_IRC_SERVER["address"], FALLBACK_IRC_SERVER["port"]
                        , conn.nickname, "" )
        else:
            conn.connection.server = TWITCH_IRC_SERVER["address"]
            debug_send("connecting to %s server" % TWITCH_IRC_SERVER["address"])
            conn.connect( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                        , conn.nickname, conn.password )
        #conn.connect(FALLBACK_IRC_SERVER["address"], conn.port, conn.nickname, conn.password)
        
        lock_reconnect.release()
    
    if just_switch:
        switch()
        return
    
    while True:
        if not conn.connection.is_connected():
            switch()
        elif conn.connection.server != TWITCH_IRC_SERVER["address"]:
            # warning, this is done without calling disconnect from connected server
            switch()
            if not conn.connection.is_connected():
                debug_send("waiting 8*60 seconds after twitch IRC server connection attempt")
                time.sleep(8*60)
        time.sleep(reconnection_interval)


def on_onstream(request_channel):
    # clearing them all
    for i in onstream_responses.keys():
        onstream_responses[i] = False
    
    if _stream_id != 1:
        time.sleep(0.3)
        counter = 30*(_stream_id - 1)
        while counter > 0:
            if STREAM[_stream_id - 1]["nickname"] in onstream_responses and \
               onstream_responses[STREAM[_stream_id - 1]["nickname"]] == True:
                break
            else:
                counter -= 1
                time.sleep(0.1)
    
    onstream_value = status["player"]
    if  status["player"] != "[idle]" \
    and pids["dd_to_pipe"] is not None \
    and psutil.pid_exists(pids["dd_to_pipe"]) \
    and psutil.Process(pids["dd_to_pipe"]).status == psutil.STATUS_STOPPED:
        if RETRY_COUNT - toggles["livestreamer__on"] > 1:
            onstream_value = "reconnecting to " + status["player"]
        else:
            onstream_value = "switching to " + status["player"]
    
    try:
        # (ommiting '#' character from channel string, when composing twitch link)
        conn.connection.privmsg( request_channel
                               , "twitch.tv/%-10s   %s" % (STREAM[_stream_id]["channel"][1:], onstream_value) )
    except Exception as x:
        print_exception(x, "sending !onstream responce")
        return False


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
        vote_set.intersection([player.lower() for player in forbidden_players]):
            conn.msg("error, one of the specified players does not exist in database or forbidden")
            return True
    
    
    def on_voting():
        voted_player = voting(vote_set)
        if voted_player == -1:
            # was aborted or already running
            return
        elif voted_player == None:
            if status["player"] == "[idle]":
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
        online_set = frozenset(bj["nickname"] for bj in online_fetch(afreeca_database, tune_oom=True))
        if online_set == -1:
            # i.e. couldn't get online list
            conn.msg("couldn't start vote for all online players")
            return -1
        elif len(online_set) < 2:
            conn.msg("less than 2 streamers available for voting, aborting")
            return -1
        else:
            vote_set = online_set - forbidden_players
    
    vote_set = frozenset([p.lower() for p in vote_set])
    
    
    # clearing votes key/value dict
    for player in votes.keys():
        votes[player] = 0
    
    voted_users[:] = []
    
    
    counter = VOTE_TIMER
    report_interval = 20
    conn.msg("voting started, %d seconds remaining, type the desired nickname" % counter)
    time.sleep(report_interval)
    while counter >= 0 and toggles["voting__on"]:
        counter -= report_interval
        conn.msg( "voting in progress, %d seconds remaining, (%s)" % \
                  ( counter, ', '.join( [ "%s: %d" % (k2,v2) for k2,v2 in votes.items() \
                                          if (vote_set is None or k2 in vote_set) and v2 > 0 ] ) ) )
        if counter >= report_interval:
            time.sleep(report_interval)
        else:
            time.sleep(counter)
            break
    if toggles["voting__on"] == False:
        conn.msg("voting aborted!")
        return -1
    else:
        toggles["voting__on"] = False
    
    
    if vote_set is None:
        these_votes = votes
    else:
        #these_votes = lambda votes, vote_set: {key: votes[key] for key in vote_set}
        these_votes = {key: votes[key.lower()] for key in vote_set}
    debug_send("vote_set: " + str(vote_set))
    debug_send("these_votes: " + str(these_votes))
    
    max_votes = max(these_votes.values())
    if max_votes > 0:
        conn.msg("voting ended; "+', '.join(["%s: %d" % (k2,v2) for k2,v2 in these_votes.items() if v2 > 0]))
        winners = [k for k, v in these_votes.items() if v == max_votes]
        if len(winners) == 1:
            return winners[0]
        else:
            conn.msg("there are several winners, selecting random winner")
            return random.choice(winners)
    else:
        conn.msg("voting ended")
        return None


def stream_supervisor():
    debug_send("stream supervisor started")
    #signal.signal(signal.SIGPIPE, signal.SIG_IGN) # avoiding termination at closed pipe
    
    def autoswitch():
        if pid_alive(mpids["livestreamer"]) or toggles["voting__on"]:
            autoswitch.waitcounter = AUTOSWITCH_START_DELAY
        else:
            autoswitch.waitcounter -= SUPERVISOR_INTERVAL
        
        if autoswitch.waitcounter <= 0:
            autoswitch.waitcounter = AUTOSWITCH_START_DELAY + 60
            # getting onstream_set
            statuses = []
            for iter1_status in get_statuses()[1:]:
                if iter1_status is not None and iter1_status != "[idle]":
                    statuses.append(iter1_status)
            onstream_set = frozenset([iter2_status["status"]["player"] for iter2_status in statuses])
            
            ## encapsulating online_fetch function into multiprocessing queue
            q = Queue()
            def queueFetchedPlayers(q):
                q.put(online_fetch(afreeca_database, quiet=True, tune_oom=True))
            
            p = Process(target=queueFetchedPlayers, args=(q,))
            p.start()
            p.join()
            
            # getting online_set
            online_BJs = q.get()
            
            if online_BJs == -1 or len(online_BJs) == 0:
                # means that couldn't fetch the online list
                conn.msg( "afreeca online returns no streamers online (seems to be down), aborting autoswitch,"
                          " deferring autoswitch for 8 minutes" )
                autoswitch.waitcounter = 8*60
                return
                
            else:
                choice_dicts = []
                
                # if all online BJs are already being restreamed at the moment
                if (len(online_BJs) > 0 and len( frozenset(bj["nickname"] for bj in online_BJs) -
                                                 onstream_set - forbidden_players ) == 0):
                    pass
                else:
                    #print("[dbg] onstream_set = %s" % str(onstream_set))
                    #print("[dbg] choice_dicts = %s" % ", ".join([bj["nickname"] for bj in online_BJs]))
                    for bj in online_BJs:
                        #print("[dbg] checking player {%s}" % bj["nickname"])
                        if (bj["nickname"] not in onstream_set) and (bj["nickname"] not in forbidden_players) and \
                           (bj["is_password"] == "N"):
                            choice_dicts.append(bj)
                    
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
                if len(choice_dicts) == 0:
                    random_onstream_player = random.choice(list(onstream_set))
                    debug_send( "stream was idle for more than %d seconds, picking random onstream streamer %s" % \
                                (AUTOSWITCH_START_DELAY, random_onstream_player), tochat=True )
                    on_setplayer(random_onstream_player)
                elif len(choice_dicts) == 1:
                    debug_send( "stream was idle for more than %d seconds, switching to %s" % \
                                (AUTOSWITCH_START_DELAY, choice_dicts[0]["nickname"]), tochat=True )
                    on_setplayer([choice_dicts[0]["nickname"]])
                else:
                    debug_send( "stream was idle for more than %d seconds, autoswitch started, "
                                "select from the following players:\n" % AUTOSWITCH_START_DELAY, tochat=True )
                    afreeca_api.print_online_list(choice_dicts, message="")
                    voted_player = voting(frozenset(bj["nickname"] for bj in choice_dicts))
                    if voted_player == -1:
                        return
                    elif voted_player != None:
                        on_setplayer([voted_player])
                    else:
                        debug_send( "no votes received, randomly picking one of the two BJs " +
                                    "with highest viewers count and afreeca rank available online", tochat=True )
                        on_setplayer([random.choice([bj["nickname"] for bj in choice_dicts][:2])])
    
    autoswitch.waitcounter = AUTOSWITCH_START_DELAY
    
    
    def twitch_stream_online_supervisor():
        twitch_stream_online_supervisor.counter -= SUPERVISOR_INTERVAL
        if twitch_stream_online_supervisor.counter <= 0:
            twitch_stream_online_supervisor.counter = TWITCH_POLL_INTERVAL
            if not twitch_stream_online():
                debug_send( "wtf, twitch reports \"offline\" status for the stream, ooh... restarting stream"
                          , tochat=True )
                on_restartstream([])
                return False
            else:
                print(">>>>> twitch reports online status for the stream")
        return True
            
    
    def dummy_video_loop():
        while pid_alive(pids["ffmpeg"]) and pid_alive(mpids["ffmpeg"]) and \
        pid_alive(mpids["stream_supervisor"]) and toggles["dummy_video_loop__on"]:
            global dummy_videos
            dummy_videos_path = DUMMY_VIDEOS_PATH
            
            if len(dummy_videos) == 0:
                dummy_videos = [ f for f in os.listdir(DUMMY_VIDEOS_PATH) \
                                 if f[-3:] == ".ts" and os.path.isfile(DUMMY_VIDEOS_PATH+"/"+f) ]
            if len(dummy_videos) == 0:
                debug_send("fatal error, no dummy videos found, sleeping for 8 minutes", tochat=True)
                time.sleep(60*8)
                toggles["dummy_video_loop__on"] = False
                return
            
            dummy_videofile = DUMMY_VIDEOS_PATH + "/" + random.choice(dummy_videos)
            
            if os.path.isfile(dummy_videofile):
                debug_send("* using \"%s\" video file" % dummy_videofile)
            else:
                debug_send("fatal error, video file \"%s\" is not found" % dummy_videofile)
                time.sleep(3)
                toggles["dummy_video_loop__on"] = False
                return
            
            try:
                if not stat.S_ISFIFO(os.stat(_stream_pipe).st_mode):
                    debug_send("error, stream pipe file is invalid", tochat=True)
                    return
            except Exception as x:
                debug_send("error, stream pipe file is invalid: " + str(x), tochat=True)
                toggles["dummy_video_loop__on"] = False
                return
            
            #dummy_video_loop__cmd = "cat \"" + dummy_videofile + "\" > " + _stream_pipe
            dummy_video_loop__cmd = "/opt/ffmpeg-git/bin/"
            dummy_video_loop__cmd += "ffmpeg -y -hide_banner -loglevel warning -fflags +nobuffer -vsync passthrough " \
                                     "-re -i " + dummy_videofile + " " \
                                     "-c copy " \
                                     "-movflags +faststart " \
                                     "-f mpegts " + _stream_pipe
            
            debug_send(dummy_video_loop__cmd)
            dummy_video_loop__process = Popen(dummy_video_loop__cmd.split(), shell=False)
            pids["dummy_video_loop"] = dummy_video_loop__process.pid
            debug_send("dummy_video_loop exited with code %d" % dummy_video_loop__process.wait())
        #toggles["dummy_video_loop__on"] = False
    #def dummy_video_loop():
    
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
    antispam.lastmsglist = []
    antispam.blocktime = datetime.fromtimestamp(0)
    
    def dummy_video_supervisor(p_dds):
        def start_dummy_video():
            toggles["dummy_video_loop__on"] = True
            dummy_video_loop__mprocess = Process(target=dummy_video_loop)
            dummy_video_loop__mprocess.start()
            mpids["dummy_video_loop"] = dummy_video_loop__mprocess.pid
            debug_send("dummy_video_loop mprocess started")
        
        if pid_alive(pids["livestreamer"]) and toggles["livestreamer__on"] and os.path.exists(_stream_rate_file):
            try:
                try:
                    hF = open(_stream_rate_file, 'r')
                except Exception as x:
                    debug_send("cannot open input rate file: " + str(x))
                    hF.close()
                    return
                
                inside_digits = False
                inside_units = False
                digits = ""
                units = ""
                for char in hF.read(16):
                    if char.isdigit():
                        inside_digits = True
                    if not char.isdigit() and char != '.' and inside_digits == True:
                        inside_digits = False
                        inside_units = True
                    
                    if inside_units == True and char == ']':
                        break
                    
                    if inside_digits:
                        digits += char
                    if inside_units == True:
                        units += char
                
                hF.close()
                
                units = units.strip()
                
                if units == "B/s":
                    units_multiplier = 1
                elif units == "KiB/s":
                    units_multiplier = 1024
                elif units == "MiB/s":
                    units_multiplier = 1024*1024
                else:
                    debug_send("error while parsing stream input rate file: units=\"%s\"" % units)
                    return
                
                rate = float(digits) * units_multiplier
                
                # debug output, shows what it has read and calculated rate in bytes
                #with open(_stream_rate_file, 'r') as hF:
                    #print("%s  %f %s  [%s]" % (hF.read(16)[:-1], float(digits), units, rate))
            
            except Exception as x:
                print_exception(x, "reading input rate file: [%d]" % (sys.exc_info()[-1].tb_lineno))
                hF.close()
                toggles["dummy_video_loop__on"] = False
                rate = 0
            
            if rate >= MIN_INPUT_RATE and toggles["livestreamer__on"]:
            # if input rate is sufficient
                ## suspend dummy video playback (into twitch ffmpeg)
                if pids["dummy_video_loop"] is not None and psutil.pid_exists(pids["dummy_video_loop"]):
                    try:
                        p_dvl = psutil.Process(pids["dummy_video_loop"])
                    except Exception as x:
                        debug_send("Exception2: Process(p_dvl): " + str(x))
                        return
                    
                    # if it's not suspended
                    if p_dvl.status() != psutil.STATUS_STOPPED:
                        #debug_send("p_dvl.status() = " + p_dvl.status())
                        
                        # suspend it
                        debug_send("suspending p_dvl")
                        p_dvl.suspend()
                        
                        if antispam(just_check=True):
                            conn.msg("(afreeca started sending stream data, stopping dummy video)")
                
                dd_to_pipe(p_dds)
                return
        
        # disconnect livestreamer from twitch ffmpeg
        dd_to_buffer(p_dds)
        
        #if not pid_alive(mpids["dummy_video_loop"]) and pid_alive(mpids["ffmpeg"]):
        if not pid_alive(mpids["dummy_video_loop"]):
            # start dummy video mprocess
            start_dummy_video()
        
        ## resume dummy video playback (into twitch ffmpeg)
        # if dummy_video_loop process exists
        elif pids["dummy_video_loop"] is not None and psutil.pid_exists(pids["dummy_video_loop"]):
            try:
                p_dvl = psutil.Process(pids["dummy_video_loop"])
            except Exception as x:
                debug_send("Exception1: Process(p_dvl): " + str(x))
                return
            
            # if it's suspended
            if p_dvl.status() == psutil.STATUS_STOPPED:
                # resume it
                debug_send("resuming p_dvl")
                p_dvl.resume()
                
                # now without ignoring SIGCHLD signal commercial process doesn't leave open FIFO pipe descriptor
                start_multiprocess(commercial, args=(30,))
                
                if antispam():
                    conn.msg("(afreeca is not responding, starting dummy video)")
            #else:
                #debug_send("p_dvl.status() = " + p_dvl.status())
        elif not psutil.pid_exists(pids["ffmpeg"]):
            debug_send("dummy_video_loop does not exist, is it ok? (while twitch ffmpeg is not running)")
        #if pid_alive(pids["livestreamer"]) and os.path.exists(_stream_rate_file):
    #def dummy_video_supervisor(p_dds):
    
    # check if all dummy videos exist
    for videofile_relpath in ["dummy_videos/"+videofile for videofile in dummy_videos]:
        try:
            if not os.path.isfile(videofile_relpath):
                debug_send( "fatal problem, dummy video file \"%s\" does not exist, exiting stream supervisor" \
                          % videofile_relpath, tochat=True )
                return
        except Exception as x:
            print_exception(x, "looking for dummy videos", tochat=True)
            debug_send("exiting stream supervisor", tochat=True)
            return
    
    p_dds = {"dd_to_buffer": None, "dd_to_pipe": None}
    
    dummy_timer = 0
    while toggles["stream_supervisor__on"]:
        if pid_alive(pids["livestreamer"]):
            if pids["dummy_video_loop"] is not None and psutil.pid_exists(pids["dummy_video_loop"]):
                try:
                    p_dvl = psutil.Process(pids["dummy_video_loop"])
                    if p_dvl.status() != psutil.STATUS_STOPPED:
                        # check for having dummy videos running for too long, refresh after 140s of running dummy videos
                        if (dummy_timer > 140):
                            #on_refresh(["--quiet"])
                            on_refresh([])
                            dummy_timer = 0
                        else:
                            dummy_timer += SUPERVISOR_INTERVAL
                    else:
                        dummy_timer = 0
                except Exception as x:
                    debug_send("supervisor loop: Process(dvl): " + str(x), tochat=True)
            else:
                dummy_timer = 0
        else:
            dummy_timer = 0
        
        time.sleep(SUPERVISOR_INTERVAL)
        #print("-------------------------------- stream supervisor ping")
        if not pid_alive(mpids["ffmpeg"]):
            if toggles["streaming__enabled"]:
                debug_send("supervisor starts streaming to twitch...", tochat=True)
                on_startstream([])
                time.sleep(4)
                on_refresh(["--quiet"])
                # or retransmit command with corresponding stream_id argument to another bot?
                twitch_stream_online_supervisor.counter = TWITCH_POLL_INTERVAL
            else:
                if 0 < datetime.now().timestamp() % 740 <= SUPERVISOR_INTERVAL:
                    conn.msg("stream is off, moderators can use !startstream")
        else:
            try:
                # checks twitch stream online status every TWITCH_POLL_INTERVAL seconds
                if not twitch_stream_online_supervisor():
                    continue
            except Exception as x:
                print_exception(x, "running twitch_stream_online_supervisor", tochat=True)
            
            try:
                dummy_video_supervisor(p_dds)
            except Exception as x:
                print_exception(x, "running dummy_video_supervisor", tochat=True)
            
            try:
                autoswitch()
            except Exception as x:
                print_exception(x, "running autoswitch", tochat=True)
                autoswitch.waitcounter = AUTOSWITCH_START_DELAY
    debug_send("stream supervisor exited!")
    #while toggles["stream_supervisor__on"]:
#def stream_supervisor():

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
            try:
                conn.connection.privmsg(STREAM[int(args[0][1])]["channel"], "!startstream")
            except Exception as x:
                print_exception(x, "sending command to another channel")
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
        debug_send("launching ffmpeg...", tochat=True)
        #signal.signal(signal.SIGPIPE, signal.SIG_IGN) # avoiding termination at closed pipe
        
        p_pipe = maintain_pipe(_stream_pipe)
        p_pipel = maintain_pipe(_stream_pipel)
        
        if p_pipe == None or p_pipel == None:
            debug_send("p_pipe = %s, p_pipel = %s" % (str(p_pipe), str(p_pipel)), tochat=True)
            return
        
        # starting ffmpeg to stream from pipe
        #ffmpeg__cmd = "ffmpeg -hide_banner -flags +global_header -fflags +nobuffer -vsync drop -copytb 1 " \
        ffmpeg__cmd = "/opt/ffmpeg-git/bin/"
        ffmpeg__cmd += "ffmpeg -hide_banner -fflags +nobuffer -vsync 0 " \
                       "-re -i " + _stream_pipe + " " \
                       "-c copy -bsf:a aac_adtstoasc " \
                       "-c:a libfdk_aac -cutoff 18000 -b:a 128k -ar 48000 " \
                       "-movflags +faststart " \
                       "-f flv " + RTMP_SERVER + "/" + STREAM[_stream_id]["stream_key"]
        
        #ffmpeg__cmd += ffmpeg_options
        #ffmpeg__cmd += [ "-bsf:a", "aac_adtstoasc", "-f", "flv", RTMP_SERVER + '/' + STREAM[_stream_id]["stream_key"]]
        debug_send("\n%s\n" % ffmpeg__cmd)
        ffmpeg__process = Popen(ffmpeg__cmd.split())
        pids["ffmpeg"] = ffmpeg__process.pid
        ffmpeg__exit_code = ffmpeg__process.wait()
        
        debug_send("streaming to twitch ended with code %d" % ffmpeg__exit_code, tochat=True)
        
        close_pid_p_gradually("keep_%s" % _stream_pipe)
        close_pid_p_gradually("keep_%s" % _stream_pipel)
    
    
    # check if pipe file exists&valid or create a new one
    if not os.path.exists(_stream_pipe):
        debug_send("streaming pipe doesn't exist, attempting to create one")
        try:
            os.mkfifo(_stream_pipe)
        except:
            debug_send("error, could not create a pipe for streaming processes", tochat=True)
            return
        else:
            debug_send("created a new pipe, filename: " + _stream_pipe)
    elif not stat.S_ISFIFO(os.stat(_stream_pipe).st_mode):
        debug_send("file \"%s\" is not a pipe, aborting" % _stream_pipe, tochat=True)
        try:
            os.remove(_stream_pipe)
            os.mkfifo(_stream_pipe)
        except:
            debug_send("error, could not create a pipe for streaming processes", tochat=True)
            return
        else:
            debug_send("created a new pipe, filename: " + _stream_pipe)
    
    
    # starting ffmpeg streaming mprocess
    if pid_alive(mpids["ffmpeg"]): # and twitch status is not online
        debug_send("stream is already running", tochat=True)
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
        debug_send("warning, couldn't stop livestreamer", tochat=True)
    
    if pid_alive(mpids["ffmpeg"]) or pid_alive(pids["ffmpeg"]):
        if pid_alive(pids["ffmpeg"]):
            if not terminate_pid_p("ffmpeg"):
                debug_send("trying to kill ffmpeg process...")
                if kill_pid_p("ffmpeg"):
                    debug_send("killed ffmpeg process...")
                else:
                    return -1
    else:
        debug_send("streaming process is not running", tochat=True)
        return -2


def on_restartstream(args):
    if len(args) != 0: 
        return False
    
    # if failed during stopping ffmpeg
    if on_stopstream([]) == -1:
        return
    
    time.sleep(1)
    on_startstream([])
    time.sleep(1)
    on_refresh(["--quiet"])


def pickStream(afreeca_id, carrierPreference):
    #LivestreamerObject = Livestreamer() # doing this every time a new BJ is requested saves memory (a lot!)
    lStreamsForBJ = livestreamer_helper.LivestreamerObject.streams("afreeca.com/" + afreeca_id)
    if not lStreamsForBJ:
        #debug_send("livestreamer returned no streams for %s" (afreeca_id))
        debug_send("no streams found for %s" % (afreeca_id), tochat=True)
        return None
    #print("lStreamsForBJ: " + str(lStreamsForBJ))
    
    streamsForBJ = livestreamer_helper.filterSupportedStreams(lStreamsForBJ)
    if not streamsForBJ:
        debug_send("there are no supported streams for %s!" % (afreeca_id), tochat=True)
        return None
    
    if carrierPreference not in [None, "best"]:
        stream = livestreamer_helper.selectStreamByPreference(streamsForBJ, carrierPreference)
        if stream:
            debug_send("forcing %s for %s" % (carrierPreference, afreeca_id), tochat=True)
            return stream
        debug_send("%s stream carrier is unavailable for %s" % (carrierPreference, afreeca_id), tochat=True)
    
    stream = livestreamer_helper.selectStreamBest(streamsForBJ)
    if stream:
        return stream
    debug_send("there is no \"best\" stream for %s" % (afreeca_id))
    
    return livestreamer_helper.selectStreamAny(streamsForBJ)


def startplayer(afreeca_id, player, carrierPreference=None):
    def livestreamer(afreeca_id):
        #signal.signal(signal.SIGPIPE, signal.SIG_IGN) # avoiding termination at closed pipe
        
        ## checking if BJ has a remembered relation of carrier and carrierPreference matches
        if  afreeca_id in bjStreamCarriers \
        and (carrierPreference == None or carrierPreference == bjStreamCarriers[afreeca_id]["carrier"]):
            ## use remembered
            if carrierPreference != None:
                debug_send("forcing %s for %s" % (carrierPreference, afreeca_id), tochat=True)
        ## if stream couldn't be found in remembered bjStreamCarriers or carrierPreference does not match
        else:
            ## try to find a stream matching the carrierPreference
            stream = pickStream(afreeca_id, carrierPreference)
            if stream:
                debug_send("remembering %s carrier for %s" % (stream["carrier"], player), tochat=True)
                bjStreamCarriers[afreeca_id] = stream
            else:
                #debug_send("no streams found for %s" % (player), tochat=True)
                return
        
        debug_send("bjStreamCarriers[%s][\"carrier\"] = %s" % (afreeca_id, bjStreamCarriers[afreeca_id]["carrier"]))
        
        livestreamer__cmd = "livestreamer " + ' '.join(LIVESTREAMER_OPTIONS) # adding options from settings file
        livestreamer__cmd += " afreeca.com/%s %s -O" % (afreeca_id, bjStreamCarriers[afreeca_id]["lName"])
        
        if bjStreamCarriers[afreeca_id]["carrier"] == "RTMP": # if BJ has RTMP stream carrier (read from key/value dictionary)
            ffmpeg__cmd = "/opt/ffmpeg-git/bin/"
            ffmpeg__cmd += "ffmpeg -y -hide_banner -loglevel warning -fflags +nobuffer -vsync passthrough " \
                           "-re -i - " \
                           "-c:v copy " \
                           "-c:a libfdk_aac -cutoff 18000 -b:a 128k -ar 48000 " \
                           "-bsf:v h264_mp4toannexb " \
                           "-movflags +faststart " \
                           "-f mpegts -"
        elif bjStreamCarriers[afreeca_id]["carrier"] == "HLS": # if BJ has HLS stream carrier (read from key/value dictionary)
            # pv --rate-limit doesn't work as expected, it computes overall data size over elapsed time
            #ffmpeg__cmd =   "pv --rate-limit %s --wait --average-rate --timer --rate --bytes | " % (HLS_RATE_LIMIT)
            ffmpeg__cmd = "/opt/ffmpeg-git/bin/"
            ffmpeg__cmd += "ffmpeg -y -hide_banner -loglevel warning -fflags +nobuffer -vsync passthrough " \
                           "-re -i - " \
                           "-c:v copy " \
                           "-c:a libfdk_aac -cutoff 18000 -b:a 128k -ar 48000 " \
                           "-movflags +faststart " \
                           "-f mpegts -"
        else:
            debug_send("huge problem, unknown stream carrier in bjStreamCarriers!", tochat=True)
            return
        
        pv__cmd = "pv - --wait -f -i %s -r 2>&1 1>%s" % (str(PV_PIPE_INTERVAL), _stream_pipel)
        pv__cmd += " | while read -d $'\\r' line; do echo $line > %s; done" % _stream_rate_file
        
        # ffmpeg, livestreamer and pv commands are ready for now, print them
        debug_send("\n%s\n| %s\n| %s\n" % (livestreamer__cmd, ffmpeg__cmd, pv__cmd))
        
        # removing stream rate file if exists
        if os.path.exists(_stream_rate_file):
            try:
                os.remove(_stream_rate_file)
            except Exception as x:
                print_exception(x, "deleting %s file" % (_stream_rate_file))
        
        toggles["livestreamer__on"] = RETRY_COUNT
        while toggles["livestreamer__on"] > 0:
            # checking if stream pipe file (pipe?l) exists and is a pipe
            try:
                if not stat.S_ISFIFO(os.stat(_stream_pipel).st_mode):
                    debug_send("error, stream pipe file is invalid (pipel), maybe try !restartbot", tochat=True)
                    return
            except Exception as x:
                print_exception(x, "stas.S_ISFIFO failed inside livestreamer mprocess")
                return
            
            l_ffmpeg_log_file = open("l_ffmpeg_log%d" % _stream_id, 'a')
            print(datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + " " + player, file=l_ffmpeg_log_file)
            
            # creating livestreamer process that outputs to a pipe
            livestreamer__process = Popen(livestreamer__cmd.split(), stdout=PIPE)
            pids["livestreamer"] = livestreamer__process.pid # adding it's pid to global dictionary
            
            # creating ffmpeg process that gets input from livestreamer and outputs to stdout
            ffmpeg__process = Popen( ffmpeg__cmd.split()
                                   , stdin=livestreamer__process.stdout, stdout=PIPE, stderr=l_ffmpeg_log_file )
            pids["l_ffmpeg"] = ffmpeg__process.pid # adding it's pid to global dictionary
            
            # creating pv process that gets input from ffmpeg
            pv__process = Popen("exec " + pv__cmd, stdin=ffmpeg__process.stdout, shell=True)
            pids["pv"] = pv__process.pid # adding it's pid to global dictionary
            
            livestreamer__process.stdout.close() # dunno
            ##ffmpeg__process.communicate()        # interconnects livestreamer with ffmpeg through a pipe
            ffmpeg__process.stdout.close() # dunno
            pv__process.communicate()        # interconnects ffmpeg with pv through a pipe
            
            livestreamer__exit_code = livestreamer__process.wait() # waiting process to exit and it's exit code
            ffmpeg__exit_code = ffmpeg__process.wait()             # waiting process to exit and it's exit code
            pv__exit_code = pv__process.wait()                     # waiting process to exit and it's exit code
            
            print(datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + " " + player + "\n", file=l_ffmpeg_log_file)
            l_ffmpeg_log_file.close()
            
            # removing stream rate file so that dummy video supervisor knows that pv is not running
            if os.path.exists(_stream_rate_file):
                try:
                    os.remove(_stream_rate_file)
                except Exception as x:
                    print_exception(x, "deleting %s file" % (_stream_rate_file))
            
            toggles["livestreamer__on"] -= 1 # once they both exited, decrement remaining retries count
            
            debug_send( "livestreamer__exit_code = %d, ffmpeg__exit_code = %d, pv__exit_code = %d" \
                      % (livestreamer__exit_code, ffmpeg__exit_code, pv__exit_code))
            
            if toggles["livestreamer__on"] > 0 and isbjon(afreeca_id, quiet=True):
                debug_send("reconnecting to %s (%s)  [%d retries left], exit codes: %d %d" % \
                          (player, afreeca_id, toggles["livestreamer__on"], livestreamer__exit_code, ffmpeg__exit_code))
        #end while toggles["livestreamer__on"] > 0:
        
        # WTF. Why livestreamer's function should set all this crap?
        status["player"] = "[idle]"
        status["afreeca_id"] = None
        
        status["prev_player"] = player
        status["prev_afreeca_id"] = afreeca_id
        dump_status()
        on_title(["--quiet", "temporary snipealot%d" % _stream_id])
        
        # on_tldef
        tldef__mprocess = Process(target=on_tldef, args=(["[idle]",  "--quiet"],))
        tldef__mprocess.start()
        mpids["tldef"] = tldef__mprocess.pid
        
        debug_send( "livestreamer ended with codes: %d %d %d" \
                  % (livestreamer__exit_code, ffmpeg__exit_code, pv__exit_code))
        
        start_multiprocess(commercial, args=(30,))
        
        if livestreamer__exit_code in {0, 255, -15, -9}:
            conn.msg("afreeca stream playback ended")
        else:
            conn.msg( "afreeca stream appears to be offline, inaccessible or has incompatible stream container (%d)"
                    % livestreamer__exit_code )
    #end def livestreamer(afreeca_id):
    
    if pid_alive(mpids["livestreamer"]) or pid_alive(pids["livestreamer"]):
        debug_send("error, another afreeca playback stream is already running, wait a bit and try again")
        toggles["livestreamer__on"] = 0
        close_pid_p_gradually("livestreamer")
        close_pid_p_gradually("l_ffmpeg")
        close_pid_p_gradually("pv")
        return True
    
    if not pid_alive(pids["ffmpeg"]):
        debug_send("error, streaming to twitch is not running", tochat=True)
        return True
    
    toggles["voting__on"] = False
    conn.msg("switching to " + player)
    debug_send("switching to " + player)
    
    #needrestart = False
    #if (afreeca_id == "sogoodtt" and status["afreeca_id"] != afreeca_id):
        #needrestart = True
    
    status["player"] = player
    status["afreeca_id"] = afreeca_id
    dump_status()
    on_title(["--quiet", player])
    
    #if needrestart:
        #on_restartstream([])
    #else:
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
        toggles["livestreamer__on"] = 0
        if any([not pid_alive(pids[process_name]) for process_name in {"livestreamer", "l_ffmpeg", "pv"}]):
            debug_send("waiting for any of livestreamer mprocess processes to finish")
            time.sleep(1)
        
        while ( pid_alive(pids["livestreamer"]) or pid_alive(pids["l_ffmpeg"]) or pid_alive(pids["l_ffmpeg"]) or \
                pid_alive(mpids["livestreamer"]) ):
            toggles["livestreamer__on"] = 0
            close_pid_p_gradually("livestreamer")
            close_pid_p_gradually("l_ffmpeg")
            close_pid_p_gradually("pv")
            toggles["livestreamer__on"] = 0
            time.sleep(1)
    else:
        conn.msg("error, afreeca playback is not running")


def maintain_pipe(pipe_name):
    # check if pipe file exists&valid or create a new one
    if not os.path.exists(pipe_name):
        debug_send("streaming pipe doesn't exist, attempting to create one")
        try:
            os.mkfifo(pipe_name)
        except Exception as x:
            debug_send("error, could not mkfifo %s for streaming processes: %s" % (pipe_name, str(x)), tochat=True)
            return -1
        else:
            debug_send("created a new pipe, filename: " + pipe_name)
    elif not stat.S_ISFIFO(os.stat(pipe_name).st_mode):
        debug_send("file \"%s\" is not a pipe, replacing" % pipe_name, tochat=True)
        try:
            os.remove(pipe_name)
            os.mkfifo(pipe_name)
        except Exception as x:
            debug_send("error, could not mkfifo %s for streaming processes: %s" % (pipe_name, str(x)), tochat=True)
            return -1
        else:
            debug_send("created a new pipe, filename: " + pipe_name)
    
    keep_pipe__process = Popen(("dd of=" + pipe_name).split())
    pids["keep_%s" % pipe_name] = keep_pipe__process.pid
    
    return keep_pipe__process


def dd_to_buffer(p):
    with lock_dd:
        if p["dd_to_pipe"] is not None:
            ## suspend existing dd to pipe
            if p["dd_to_pipe"].is_running():
                try:
                    # suspend dd to pipe
                    #debug_send("dd_to_buffer: \"dd_to_pipe\" status = " + p["dd_to_pipe"].status())
                    if p["dd_to_pipe"].status() != psutil.STATUS_STOPPED:
                        debug_send("dd_to_buffer: suspend \"dd_to_pipe\"")
                        p["dd_to_pipe"].suspend()
                except Exception as x:
                    debug_send("dd_to_buffer: (about \"dd_to_pipe\"): " + str(x), tochat=True)
            else:
                debug_send("dd_to_buffer: \"dd_to_pipe\" is not running")
        
        # if dd_to_buffer exists
        if p["dd_to_buffer"] is not None and p["dd_to_buffer"].is_running():
            try:
                # resume existing dd to pipe
                #debug_send("dd_to_buffer: \"dd_to_buffer\" status = " + p_dd.status())
                if p["dd_to_buffer"].status() == psutil.STATUS_STOPPED:
                    debug_send("dd_to_buffer: resume \"dd_to_buffer\"")
                    p["dd_to_buffer"].resume()
                    return
                elif p["dd_to_buffer"].status() in {psutil.STATUS_RUNNING, psutil.STATUS_SLEEPING, psutil.STATUS_DISK_SLEEP}:
                    return
                else:
                    debug_send("dd_to_buffer: closing existing dd with status = " + p["dd_to_buffer"].status())
                    close_pid_p_gradually("dd_to_buffer")
            except Exception as x:
                debug_send("dd_to_buffer: (about \"dd_to_buffer\"): " + str(x), tochat=True)
        
        # in case dd to buffer hasn't been spawned
        ## spawn new dd_to_buffer process
        p["dd_to_buffer"] = psutil.Popen(("dd if=%s of=%s" % (_stream_pipel, _stream_buffer_file)).split())
        pids["dd_to_buffer"] = p["dd_to_buffer"].pid
        debug_send("$$$$(Popen)$$$$$  dd_to_buffer $$$$$$(%d)$$$$$" % p["dd_to_buffer"].pid)


def dd_to_pipe(p):
    with lock_dd:
        ## suspend existing dd to buffer
        if p["dd_to_buffer"] is not None:
            if p["dd_to_buffer"].is_running():
                try:
                    # suspend dd to buffer
                    #debug_send("dd_to_pipe: \"dd_to_buffer\" status = " + p["dd_to_buffer"].status())
                    if p["dd_to_buffer"].status() != psutil.STATUS_STOPPED:
                        debug_send("dd_to_pipe: suspend \"dd_to_buffer\"")
                        p["dd_to_buffer"].suspend()
                except Exception as x:
                    debug_send("dd_to_pipe: (about \"dd_to_buffer\"): %s" + str(x), tochat=True)
            else:
                debug_send("dd_to_pipe: \"dd_to_buffer\" is not running")
        
        # if dd_to_pipe exists
        if p["dd_to_pipe"] is not None and p["dd_to_pipe"].is_running():
            try:
                # resume existing dd to pipe
                #debug_send("dd_to_pipe: \"dd_to_pipe\" status = " + p["dd_to_pipe"].status())
                if p["dd_to_pipe"].status() == psutil.STATUS_STOPPED:
                    debug_send("dd_to_pipe: resume \"dd_to_pipe\"")
                    p["dd_to_pipe"].resume()
                    toggles["livestreamer__on"] = RETRY_COUNT
                    return
                elif p["dd_to_pipe"].status() in {psutil.STATUS_RUNNING, psutil.STATUS_SLEEPING, psutil.STATUS_DISK_SLEEP}:
                    return
                else:
                    debug_send("dd_to_pipe: closing existing dd with status = " + p["dd_to_pipe"].status())
                    close_pid_p_gradually("dd_to_pipe")
            except Exception as x:
                debug_send("dd_to_pipe: (about \"dd_to_pipe\"): " + str(x), tochat=True)
        
        # in case dd to pipe hasn't been spawned
        ## spawn new dd_to_pipe process
        p["dd_to_pipe"] = psutil.Popen(("dd if=%s of=%s" % (_stream_pipel, _stream_pipe)).split())
        pids["dd_to_pipe"] = p["dd_to_pipe"].pid
        debug_send("****(Popen)**** dd_to_pipe ******(%d)******" % p["dd_to_pipe"].pid)


def on_setplayer(args):
    # !setplayer [-n] <player>
    # where n optional stream id
    if not (1 <= len(args) <= 3):
        return False
    
    if args[0][0] == '-':
        if args[0][1].isdigit() and int(args[0][1]) in range(1, 1+ACTIVE_BOTS):
            if len(args) >= 2:
                try:
                    conn.connection.privmsg(STREAM[int(args[0][1])]["channel"], "!setplayer " + args[1])
                except Exception as x:
                    print_exception(x, "sending command to another channel")
                return True
            else:
                return False
        else:
            return False
    
    ## choosing stream carrier
    carrierPreference = None
    if "--rtmp" in args:
        carrierPreference = "RTMP"
    elif "--hls" in args:
        carrierPreference = "HLS"
    elif "--best" in args:
        carrierPreference = "best"
    
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
    
    if not isbjon(afreeca_id, quiet=True):
        conn.msg(player + " is offline")
        return True
    
    if pid_alive(mpids["livestreamer"]):
        if on_stopplayer([]) == -1:
            return
    startplayer(afreeca_id, player, carrierPreference)


def on_setmanual(args):
    if len(args) == 0 or len(args) > 3:
        return False
    if len(args) >= 1:
        afreeca_id = args[0]
        if len(args) == 1 or len(args) > 1 and "--" == args[1][:2]:
            if afreeca_id in afreeca_database:
                player = afreeca_database[afreeca_id][NICKNAME_]
            else:
                conn.msg("error, unknown afreeca ID")
                return True
    
    if len(args) >= 2 and "--" != args[1][:2]:
        player = args[1]
    
    ## choosing stream carrier
    carrierPreference = None
    if "--rtmp" in args:
        carrierPreference = "RTMP"
    elif "--hls" in args:
        carrierPreference = "HLS"
    elif "--best" in args:
        carrierPreference = "best"
    
    if not isbjon(afreeca_id, quiet=True):
        conn.msg(player + " is offline")
        return True
    
    if pid_alive(mpids["livestreamer"]):
        if on_stopplayer([]) == -1:
            return
    startplayer(afreeca_id, player, carrierPreference)


def on_refresh(args):
    if args and not set(["--quiet", "--hls", "--rtmp", "--best"]).intersection(args):
        conn.msg("error, this command accepts \"--rtmp\" or \"--hls\" or \"--best\"")
        return
    
    ## choosing stream carrier
    carrierPreference = None
    if "--rtmp" in args:
        carrierPreference = "RTMP"
    elif "--hls" in args:
        carrierPreference = "HLS"
    elif "--best" in args:
        carrierPreference = "best"
    
    if carrierPreference == None:
        carrierPreference = random.choice(["best", "RTMP", "HLS"])
        debug_send("on_refresh: random carrierPreference = " + carrierPreference)
    
    # resetting antispam timer (does not work since it's not in multiprocess manager)
    #stream_supervisor.antispam.blocktime = datetime.fromtimestamp(0)
    
    rememberedStatus = status.copy()
    if status["player"] != "[idle]" and status["afreeca_id"] is not None:
        if pid_alive(mpids["livestreamer"]):
            on_stopplayer([])
        startplayer(rememberedStatus["afreeca_id"], rememberedStatus["player"], carrierPreference)
    elif status["prev_player"] is not None and status["prev_afreeca_id"] is not None:
        if pid_alive(mpids["livestreamer"]):
            on_stopplayer([])
        startplayer(rememberedStatus["prev_afreeca_id"], rememberedStatus["prev_player"], carrierPreference)
    elif "--quiet" not in args:
        debug_send( "error, no player is assigned to this channel and no previous players in history either"
                  , tochat=True )


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
                if r.status_code == 200:
                    if "--quiet" not in args:
                        conn.msg("successfully updated player nickname & race at teamliquid.net")
                    break
                elif counter == 0:
                    debug_send( "warning, couldn't update player nickname & race at teamliquid.net, "
                                "got %d response" % r.status_code, tochat=True )
            except Exception as x:
                print_exception(x, "updating player nickname & race at teamliquid.net")
        
        # defiler.ru
        counter = 2
        while counter:
            counter -= 1
            try:
                r = requests.post( DEFILER_API["UPDATE_ADDRESS"] % _stream_id
                                 , data = dict(data.items() | DEFILER_API["AUTH_FIELD"].items())
                                 )
            except Exception as x:
                print_exception(x, "updating player nickname & race at defiler.ru")
            if r.status_code == 200:
                if "--quiet" not in args:
                    conn.msg("successfully updated player nickname & race at defiler.ru")
                break
            elif counter == 0:
                debug_send( "warning, couldn't update streamer nickname & race at defiler.ru, "
                            "got %d response" % r.status_code, tochat=True )
    
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
    
    try:
        conn.connection.privmsg("#mca64launcher", "title=%s&race=%s" % \
                                (data["title"], data["race"] if "race" in data.keys() else ""))
    except Exception as x:
        print_exception(x, "sending info to mca64launcher")


def twitch_stream_online():
    TWITCH_HLS_PLAYLIST = "http://usher.twitch.tv/api/channel/hls/{0}.m3u8?token={1}&sig={2}"
    
    try:
        r = requests.get("https://api.twitch.tv/api/channels/{0}/access_token".format(STREAM[_stream_id]["channel"][1:]))
        url = TWITCH_HLS_PLAYLIST.format(STREAM[_stream_id]["channel"][1:], r.json()["token"], r.json()["sig"])
        
        r = requests.get(url)
    except Exception as x:
        debug_send("warning, improper response from twitch about online status: " + str(x), tochat=True)
        return -1
    
    return True if r.status_code == 200 else False


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
    
    start_multiprocess(isbjon, args=(afreeca_id,))


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
    
    if (datetime.now() - commercial_lastruntime[0]).seconds <= 12*60:
        conn.msg("error, interval between commercials is less than 12 minutes")
        return
    
    start_multiprocess(commercial, args=(length,))

def commercial(length):
    if not lock_commercial.acquire(block=False):
        debug_send("lock_commercial couldn't be acquired")
        return
    
    if (datetime.now() - commercial_lastruntime[0]).seconds <= 12*60:
        lock_commercial.release()
        return
    
    if not twitch_stream_online():
        debug_send("error, cannot start commercial - twitch stream is not running")
        lock_commercial.release()
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
        if r.status_code == 204:
            debug_send("--) successful commercial request")
        elif r.status_code == 422:
            debug_send( "unsuccessful response from twitch after sending commercial request: " + \
                        str(r.text.replace('\n', '\\n ')) )
        else:
            debug_send( "unsuccessful response from twitch after sending commercial request: " + \
                        str(r.text.replace('\n', '\\n ')), tochat=True )
    except Exception as x:
        print_exception(x, "sending twitch api request for commercial")
    
    commercial_lastruntime[0] = datetime.now()
    
    lock_commercial.release()


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
                print_exception(x, "sending twitch api request for stream title update", tochat=True)
                return
            if r.status_code == 200:
                if not quiet:
                    conn.msg("successfully updated twitch stream title")
                break
            elif counter == 0:
                debug_send( "warning, couldn't update stream title, got %d response from twitch" % r.status_code
                          , tochat=True )
    
    start_multiprocess(proceed_on_title)

def on_online(args):
    if len(args) > 1:
        return False
    
    verbose = False
    if len(args) == 1:
        if args[0] == "--verbose":
            verbose = True
        else:
            return False
    
    if not pid_alive(mpids["online_fetch"]):
        start_multiprocess(online_fetch, args=(afreeca_database,verbose,False,True,))
    else:
        debug_send("warning: attempted to start second mprocess for !online")


def on_version(args):
    if len(args) != 0:
        conn.msg("error, this command doesn't accept any arguments")
    conn.msg("Bot version: " + VERSION)


def on_processes(args):
    if len(args) > 1:
        conn.msg("error, this command accepts one or none arguments")
    
    if len(args) == 1 and args[0] == "--stdout":
        tochat = False
    else:
        tochat = True
    
    alive_pids = []
    for pid_name, pid in pids.items():
        if pid_alive(pid):
            alive_pids.append(pid_name)
    
    alive_mpids = []
    for mpid_name, mpid in mpids.items():
        if pid_alive(mpid):
            alive_mpids.append(mpid_name)
    
    current_toggles = []
    for Toggle, Toggle_state in toggles.items():
        current_toggles.append("[%s: %s]" % (Toggle, str(Toggle_state)))
    
    current_statuses = []
    for Status, Status_value in status.items():
        current_statuses.append("[%s: %s]" % (Status, str(Status_value)))
    
    debug_send("alive pids: " + ', '.join(alive_pids), tochat)
    debug_send("alive mpids: " + ', '.join(alive_mpids), tochat)
    debug_send("toggles: " + ', '.join(current_toggles), tochat)
    debug_send("statuses: " + ', '.join(current_statuses), tochat)
    debug_send("commercial_lastruntime: " + str(commercial_lastruntime), tochat)
    #debug_send("active children: " + str(active_children()), tochat)


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
        debug_send("error, such process (%s) doesn't exist or not alive", process)


def on_restartbot(args):
    if len(args) != 0:
        return False
    
    debug_send("going offline...", tochat=True)
    time.sleep(0.2)
    #conn.connection.quit()
    exit()


def on_switch_irc(args):
    debug_send("on_switch_irc")
    #if not (0 <= len(args) <= 1):
        #conn.msg("error, this command accepts only one optional argument - <irc_server_address>")
        #return
    #conn.connect( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                #, conn.nickname, conn.password
                #)
    conn.connection.quit()

def on_irc(args):
    debug_send("connected to %s IRC server" % conn.connection.server, tochat=True)


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
        debug_send("error, could not save status file: %s" % str(x), tochat=True)

def get_statuses():
    statuses = [None] * (ACTIVE_BOTS+1)
    for stream_id in range(1, 1+ACTIVE_BOTS):
        try:
            with open("status_%d.json" % stream_id, 'r') as hF:
                statuses[stream_id] = json.load(hF)
        except Exception as x:
            statuses[stream_id] = None
            print_exception(x, "reading status file of bot#%d" % (stream_id))
    
    return statuses


def on_reloadsettings(args):
    if len(args) > 0:
        conn.msg("error, this command doesn't accept any arguments")
        return False
    
    # may not work if launched not from main
    
    load_settings("settings.json")
    
    global afreeca_database, modlist, all_commands, help_for_commands, forbidden_players
    try:
        afreeca_database = load_afreeca_database("afreeca_database.json")
        modlist = load_modlist("modlist.json")
        all_commands = dict(list(user_commands.items()) + list(mod_commands.items()))
        help_for_commands = load_help_for_commands("help_for_commands.json")
        forbidden_players = load_forbidden_players("forbidden_players.json")
    except Exception as x:
        debug_send("failed to reload configs: " + str(x), tochat=True)
        time.sleep(7)
        return
        # idea: combine msg+debug using ": ", but print second part only when debugging is enabled
    
    global votes
    try:
        votes = manager.dict(dict.fromkeys([ iter_dict[NICKNAME_].lower() for iter_dict in afreeca_database.values()
                                             if iter_dict[NICKNAME_] not in forbidden_players ], 0))
    except Exception as x:
        if conn is not None:
            debug_send("fatal error, couldn't reload available votes list, vote system may not work", tochat=True)
            debug_send(str(x))
            return
    
    if conn is not None:
        debug_send("reloaded settings", tochat=True)



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
    if int(sys.argv[1]) not in range(1+len(STREAM[1:])):
        print("error," "wrong stream id")
        print("usage:" "python bot <STREAM_ID>")
        exit()
    
    
    global _stream_id, _stream_pipe, _stream_pipel, _stream_rate_file, _stream_buffer_file
    _stream_id = int(sys.argv[1])
    _stream_pipe = "pipe"+str(_stream_id)
    _stream_pipel = _stream_pipe + 'l'
    _stream_rate_file = "stream%d_input_rate" % _stream_id
    #_stream_buffer_file = "stream%d_buffer" % _stream_id
    _stream_buffer_file = "/dev/null"
    
    
    global mpids, pids, toggles, status, voted_users
    pids = manager.dict({ "dummy_video_loop": None, "ffmpeg": None
                        , "livestreamer": None, "l_ffmpeg": None, "pv": None
                        , "keep_pipe": None, "keep_pipel": None
                        , "dd_to_buffer": None, "dd_to_pipe": None
                        })
    mpids = manager.dict({ "stream_supervisor": None, "dummy_video_loop": None, "ffmpeg": None
                         , "tldef": None, "livestreamer": None, "online_fetch": None, "proceed_on_title": None
                         , "proceed_online_fetch": None, "on_onstream": None, "on_voting": None
                         , "commercial": None, "isbjon": None
                         , "chat_connection_track": None
                         })
    toggles = manager.dict({ "stream_supervisor__on": True, "dummy_video_loop__on": False
                           , "streaming__enabled": True, "livestreamer__on": False, "voting__on": False
                           })
    status = manager.dict({ "player": "[idle]", "afreeca_id": None
                          , "prev_player": "[idle]", "prev_afreeca_id": None 
                          })
    voted_users = manager.list([])
    
    global conn
    conn = IRCClass( TWITCH_IRC_SERVER["address"], TWITCH_IRC_SERVER["port"]
                   , STREAM[_stream_id]["nickname"]
                   , "oauth:" + STREAM[_stream_id]["password"] if not TEST else ""
                   , STREAM[_stream_id]["channel"]
                   )
    
    
    logging.basicConfig(level=logging.DEBUG)
    
    afreeca_api.init(conn.msg, debug_send)
    
    # initializing log file
    global logfiledescriptor
    logfiledescriptor = open("log%d" % _stream_id, 'a')
    
    debug_send("============ %s v%s ============" % (sys.argv[0], VERSION))
    dump_status()
    
    conn.start()
    
    logfiledescriptor.close()


@atexit.register
def stop_processes():
    print("--------- atexit issued ---------")
    for Toggle in toggles.keys():
        toggles[Toggle] = False
    
    
    for pid_name, pid in pids.items():
        if pid_alive(pid):
            print("closing [%s] process" % pid_name)
            close_pid_p_gradually(pid_name)
    
    print("waiting additional 7 seconds for mpids to close automatically")
    counter = 7
    while counter >= 0 and all([pid_alive(mpid) for mpid in mpids.values()]):
        counter -= 1
        time.sleep(1)
    
    for mpid_name, mpid in mpids.items():
        if pid_alive(mpid):
            print("killing [%s] multiprocess !" % mpid_name)
            kill_pid_m(mpid_name)
    
    for pid_name, pid in pids.items():
        if pid_alive(pid):
            print("process [%s] is still alive !" % pid_name)
    
    for mpid_name, mpid in mpids.items():
        if pid_alive(mpid):
            print("multiprocess [%s] is still alive !" % mpid_name)
    
    print("trying to kill child processes")
    kill_child_processes(os.getpid(), 9)
    
    if logfiledescriptor is not None:
        print("<<<<<<<===Bot exited===>>>>>>>\n\n", file=logfiledescriptor)
        logfiledescriptor.close()
    
    print("stopped")


def kill_child_processes(parent_pid, sig=15):
    try:
        p = psutil.Process(parent_pid)
    except Exception as x:
        print_exception(x, "getting process by pid")
        return
    
    child_pid = p.children(recursive=True)
    for pid in child_pid:
        os.kill(pid.pid, sig)

def signal_handler(signal, frame):
    print("You pressed Ctrl+C!")
    exit()
signal.signal(signal.SIGINT, signal_handler)

if __name__ == "__main__":
    main()
