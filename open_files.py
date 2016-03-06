import sys
import psutil

for p in [psutil.Process(int(sys.argv[1]))] + psutil.Process(int(sys.argv[1])).children(recursive = True):
    #print(p.name() + ": " + str(p.open_files()))
    print(p.name() + ": " + str(p.connections()))
