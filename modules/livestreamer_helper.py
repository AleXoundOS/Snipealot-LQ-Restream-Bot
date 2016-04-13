import random
from livestreamer.api import streams
from livestreamer.exceptions import (LivestreamerError, PluginError, NoStreamsError, NoPluginError, StreamError)
from livestreamer.stream import RTMPStream, HLSStream
from livestreamer.session import Livestreamer

lCarriers = {"RTMPStream": "RTMP", "HLSStream": "HLS"}
LivestreamerObject = Livestreamer()

def filterSupportedStreams(lStreamsForBJ):
    # get list of supported streams with corresponding carriers
    return [ {"lName": lStream, "carrier": lCarriers[type(lStreamsForBJ[lStream]).__name__]}
                                           for lStream in lStreamsForBJ
                                           if type(lStreamsForBJ[lStream]).__name__ in lCarriers ]

def selectStreamByPreference(streamsForBJ, streamCarrierPreference):
    # get streams with desired carrier
    streamsWithDesiredCarrier = list(filter(lambda stream: stream["carrier"] == streamCarrierPreference, streamsForBJ))
    if not streamsWithDesiredCarrier:
        print("no streams with desired carrier (%s)" % (streamCarrierPreference))
        return None
    
    print("streamsWithDesiredCarrier: " + str(list(streamsWithDesiredCarrier)))
    
    streamBest = selectStreamBest(streamsWithDesiredCarrier)
    if streamBest:
        return streamBest
    
    streamAny = selectStreamAny(streamsWithDesiredCarrier)
    if streamAny:
        return streamAny
    
    return None

def selectStreamBest(streamsForBJ):
    print("looking for \"best\" stream")
    bestStreamList = [stream for stream in streamsForBJ if stream["lName"] == "best"]
    if bestStreamList:
        return bestStreamList[0]
    else:
        return None

def selectStreamAny(streamsForBJ):
    print("looking for any stream")
    return random.choice(streamsForBJ)

def selectStreamForBJ(afreeca_id, streamCarrierPreference):
    #LivestreamerObject = Livestreamer() # doing this every time a new BJ is requested saves memory (a lot!)
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
        print("there are no supported streams")
        return None
    
    #print("streamsForBJ: " + str(streamsForBJ))
    
    streamSelectOrder = [selectStreamByPreference, selectStreamBest, selectStreamAny]
    for f in streamSelectOrder:
        if f.__code__.co_argcount == 1:
            stream = f(streamsForBJ)
        elif f.__code__.co_argcount == 2:
            stream = f(streamsForBJ, streamCarrierPreference)
        else:
            print("fatal error")
            return None
        
        if stream:
            print("selected stream is \"%s\", %s" % (stream["lName"], lStreamsForBJ["carrier"]))
            return stream
    
    print("no streams selected!")
    return None

if __name__ == "__main__":
    import sys
    selectStreamForBJ(sys.argv[1], sys.argv[2])
