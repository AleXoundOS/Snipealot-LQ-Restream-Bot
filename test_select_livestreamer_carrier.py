import sys
import random
from livestreamer.api import streams
from livestreamer.exceptions import (LivestreamerError, PluginError, NoStreamsError, NoPluginError, StreamError)
from livestreamer.stream import RTMPStream, HLSStream
from livestreamer.session import Livestreamer

lCarriers = {"RTMPStream": "rtmp", "HLSStream": "hls"}

def selectStreamByPreference(streamsForBJ, streamPreference):
    # get streams with desired carrier
    streamsWithDesiredCarrier = list(filter(lambda stream: stream["carrier"] == streamPreference, streamsForBJ))
    if not streamsWithDesiredCarrier:
        print("no streams with desired carrier")
        return None
    
    print("streamsWithDesiredCarrier: " + str(list(streamsWithDesiredCarrier)))
    
    print("looking for \'best\' stream")
    streamBest = selectStreamBest(streamsWithDesiredCarrier, None)
    if streamBest:
        return streamBest
    
    streamAny = selectStreamAny(streamsWithDesiredCarrier, None)
    if streamAny:
        return streamAny
    
    return None

def selectStreamBest(streamsForBJ, streamPreference):
    bestStreamList = [stream for stream in streamsForBJ if stream["lName"] == "best"]
    if bestStreamList:
        return bestStreamList[0]["lName"]
    else:
        return None

def selectStreamAny(streamsForBJ, streamPreference):
    return random.choice(streamsForBJ)["lName"]

def selectStreamForBJ(afreeca_id, streamPreference):
    LivestreamerObject = Livestreamer() # doing this every time a new BJ is requested saves memory (a lot!)
    lStreamsForBJ = LivestreamerObject.streams("afreeca.com/" + afreeca_id)
    if not lStreamsForBJ:
        print("livestreamer returned no streams for " + afreeca_id)
        return
    
    print("lStreamsForBJ: " + str(lStreamsForBJ))
    
    # get list of supported streams with corresponding carriers
    streamsForBJ = [ {"lName": lStream, "carrier": lCarriers[type(lStreamsForBJ[lStream]).__name__]}
                     for lStream in lStreamsForBJ
                     if type(lStreamsForBJ[lStream]).__name__ in lCarriers ]
    if not streamsForBJ:
        print("there is no supported streams")
        return None
    
    print("streamsForBJ: " + str(streamsForBJ))
    
    for f in streamSelectOrder:
        stream = f(streamsForBJ, streamPreference)
        if stream:
            print("selected stream is \"%s\", %s" % (stream, lStreamsForBJ[stream]))
            return stream
    
    print("no streams selected!")
    return None

streamSelectOrder = [selectStreamByPreference, selectStreamBest, selectStreamAny]
    
if __name__ == "__main__":
    selectStreamForBJ(sys.argv[1], sys.argv[2])
