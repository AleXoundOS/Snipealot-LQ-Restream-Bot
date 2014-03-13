from pytz import timezone
from datetime import timedelta

ADDON_TEST = False

if ADDON_TEST:
    import requests
    from lxml import etree
    from datetime import datetime, timedelta
    import time
    import re

DEBUG_ADDON = True

MAIN_POLL_INTERVAL = 7 # seconds
TL_POLL_INTERVAL = 40 # minutes
ANNOUNCE_OFFSETS = [ 5, 15, 30, 60, 3*60, 7*60, 12*60, 18*60, 24*60, 72*60 ] # minutes
#ANNOUNCE_OFFSETS = []
#for i in range(60*24//5):
    #ANNOUNCE_OFFSETS.append(i*5)

DAYS_AHEAD = 4

# note: be careful to have precise local time, synchronize it using ntp for example

def debug_addon(text):
    if not DEBUG_ADDON:
        return
    
    if debug_addon.logfiledescriptor is None or not os.path.isfile("addons/tl_upcoming_events.%d.log" % _stream_id):
        if debug_addon.logfiledescriptor is not None:
           debug_addon.logfiledescriptor.close()
        
        try:
            debug_addon.logfiledescriptor = open("addons/tl_upcoming_events.%d.log" % _stream_id, 'a')
        except Exception as x:
            print("exception occured while trying to open logfile for addon: " + str(x))
            return
    print( datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + ' ' + text
         , file=debug_addon.logfiledescriptor
         )
    debug_addon.logfiledescriptor.flush()
debug_addon.logfiledescriptor = None


def get_events():
    def get_events_from_page(month="this"):
        # assuming that teamliquid uses KST timezone
        if month == "this":
            #url = "http://www.teamliquid.net/calendar/%d/%02d/?tourney=17" % (tl_dt_now.year, tl_dt_now.month)
            url = "www.teamliquid.net/calendar/%d/%02d/?tourney=17" % (tl_dt_now.year, tl_dt_now.month)
            event_month = tl_dt_now.month
        elif month == "next":
            #url = "http://www.teamliquid.net/calendar/%d/%02d/?tourney=17" % (tl_dt_now.year, tl_dt_now.month + 1)
            url = "www.teamliquid.net/calendar/%d/%02d/?tourney=17" % (tl_dt_now.year, tl_dt_now.month + 1)
            event_month = tl_dt_now.month + 1
        else:
            raise Exception
        
        #cookies = { "SID": "1cb594dd87fc81cf51271f273170a89f", "sb": "pfp75ehBUh" }
        cookies = { "SID": "3e5e77007348ef42813bfd7d48d12e6a", "sb": "R4r7I9mVeY" }
        r = requests.get("http://" + url, cookies=cookies)
        myhtml = etree.HTML(r.text)
        
        events = []
        for div_calendar_day in myhtml.iter("div"):
            if "id" in div_calendar_day.keys():
                match = re.match("calendar_(.+)content", div_calendar_day.attrib["id"])
                if match is not None:
                    #print("div_calendar_day = " + div_calendar_day.attrib["id"])
                    #print("div_calendar_day match = {%s}" % match.group(1))
                    day = int(match.group(1))
                    for a_event in div_calendar_day.iter("a"):
                        if "name" in a_event.keys() and "event_" in a_event.attrib["name"]:
                            #print(a_event.attrib["name"])
                            calendar_link = url + '#' + a_event.attrib["name"]
                            
                            title = None
                            for element in a_event.getnext().iter():
                                if title is None and element.tag == "span":
                                    title = element.text.strip()
                                    #print("title = " + title)
                                elif element.tag == "strong" and "Event Time:" in element.text:
                                    recognized_time = datetime.strptime(element.text, "Event Time: %H:%M KST ")
                                    #print("recognized_time = %s" % recognized_time)
                                    event_dt = tl_dt_now.replace( day=day, hour=recognized_time.hour
                                                                , minute=recognized_time.minute, month = event_month
                                                                )
                                    events.append([event_dt, title, calendar_link, None])
                                    #events.append((event_dt.astimezone(timezone("Europe/Moscow")), calendar_link, title))
                                    #events.append((event_dt, calendar_link, title))
                                    #events.append((event_dt.astimezone(timezone("UTC")), None, title, calendar_link))
                                    #print("got event: " + str(events[-1]))
                                    break
        return events

    tl_dt_now = datetime.utcnow().replace(tzinfo=timezone("UTC"), second=0, microsecond=0).astimezone(timezone("Asia/Seoul"))
    events = get_events_from_page(month="this")
    if (tl_dt_now + timedelta(days=DAYS_AHEAD)).month > tl_dt_now.month:
        events += get_events_from_page(month="next")
    
    return events


def pp_timedelta(delta):
    output = ""
    delta_s = int(delta.total_seconds())
    if delta.days != 0:
        output += str(delta.days)
        if delta.days == 1:
            output += " day "
        else:
            output += " days "
        delta_s -= delta.days*3600*24
    if delta_s // 3600 != 0:
        output += str(delta_s // 3600)
        if delta_s // 3600 == 1:
            output += " hour "
        else:
            output += " hours "
        delta_s -= (delta_s // 3600)*3600
    if delta_s // 60 != 0:
        output += str(delta_s // 60)
        if delta_s // 60 == 1:
            output += " minute "
        else:
            output += " minutes "
    
    return output.rstrip()


def generate_notifications_list(events):
    dt_now = datetime.utcnow().replace(tzinfo=timezone("UTC")).astimezone(timezone("Asia/Seoul"))
    notifications = []
    
    for event in events:
        notification_deltas = [(event[0] - timedelta(minutes=offset) - dt_now) \
                               for offset in ANNOUNCE_OFFSETS \
                               if (event[0] - timedelta(minutes=offset) - dt_now).total_seconds() > 0]
        
        if len(notification_deltas) > 0:
            event[-1] = min(notification_deltas) + dt_now
            notifications.append(event)
            debug_addon("%s %s event will be notified at %s with remaining %s message" % \
                  ( event[1].rjust(52)
                  , event[0].astimezone(timezone("Europe/Moscow"))
                  , event[-1].astimezone(timezone("Europe/Moscow"))
                  , pp_timedelta(event[0] - event[-1])
                  ))
    return notifications


def start():
    conn.msg("(*) loaded tl_upcoming_events addon")
    while True:
        events = get_events()
        #for event in events:
            #print(event[0])
        
        scheduled_for_notification = generate_notifications_list(events)
        if len(scheduled_for_notification) > 0:
            nearest_notification_dt = min([notification[-1] for notification in scheduled_for_notification])
            debug_addon("nearest notification dt is: " + str(nearest_notification_dt.astimezone(timezone("Europe/Moscow"))))
        
        dt_now = datetime.utcnow().replace(tzinfo=timezone("UTC")).astimezone(timezone("Asia/Seoul"))
        if len(scheduled_for_notification) == 0 or \
           nearest_notification_dt > (dt_now + timedelta(minutes=TL_POLL_INTERVAL)):
            debug_addon("sleeping for %d minutes\n" % TL_POLL_INTERVAL)
            time.sleep(TL_POLL_INTERVAL*60)
        else:
            debug_addon("sleeping for %s\n" % pp_timedelta(nearest_notification_dt - dt_now))
            #print("sleeping for %d seconds" % (nearest_notification_dt - dt_now).total_seconds())
            time.sleep((nearest_notification_dt - dt_now).total_seconds())
            for notification in scheduled_for_notification:
                if notification[-1] == nearest_notification_dt:
                    debug_addon("notification: %s remaining until\n[%s] %s" % \
                          (pp_timedelta(notification[0] - notification[-1]), notification[1], notification[2]))
                    conn.msg("notification: %s remaining until [%s] %s" % \
                          (pp_timedelta(notification[0] - notification[-1]), notification[1], notification[2]))
                    time.sleep(1) # to reduce messages rate


#if __name__ == "__main__":
    #start()
if ADDON_TEST:
    for event in get_events():
        if event[0].day == 21:
            print(event)
    exit()

start()
