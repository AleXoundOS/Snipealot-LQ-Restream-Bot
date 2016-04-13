from modules import livestreamer_helper
from datetime import datetime

timeStartFetch = None
timeEndFetch = None

def pickStream(afreeca_id, carrierPreference):
    global timeStartFetch, timeEndFetch
    #LivestreamerObject = Livestreamer() # doing this every time a new BJ is requested saves memory
    
    timeStartFetch = datetime.now()
    
    lStreamsForBJ = livestreamer_helper.LivestreamerObject.streams("afreeca.com/" + afreeca_id)
    
    timeEndFetch = datetime.now()
    print("fetchTime = " + str (timeEndFetch - timeStartFetch))
    
    if not lStreamsForBJ:
        print("livestreamer returned no streams for " + afreeca_id)
        return None
    #print("lStreamsForBJ: " + str(lStreamsForBJ))
    
    streamsForBJ = livestreamer_helper.filterSupportedStreams(lStreamsForBJ)
    if not streamsForBJ:
        print("there are no supported streams for %s" % (afreeca_id))
        return None
    
    if carrierPreference != None:
        stream = livestreamer_helper.selectStreamByPreference(streamsForBJ, carrierPreference)
        if stream:
            return stream
        print("stream carrier \"%s\" is unavailable for %s" % (carrierPreference, afreeca_id))
    
    stream = livestreamer_helper.selectStreamBest(streamsForBJ)
    if stream:
        return stream
    print("there is no \"best\" stream for %s" % (afreeca_id))
    
    return livestreamer_helper.selectStreamAny(streamsForBJ)

if __name__ == "__main__":
    import sys
    stream = pickStream(sys.argv[1], sys.argv[2].upper())
    if stream:
        print("selected stream is \"%s\" (%s)" % (stream["lName"], stream["carrier"]))
    
    print("processingTime = " + str(datetime.now() - timeEndFetch))
