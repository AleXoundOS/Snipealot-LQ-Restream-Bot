from modules import afreeca_api
from modules.afreeca_api import isbjon, get_online_BJs, print_online_list
import json

def load_afreeca_database(filename):
    with open(filename, 'r') as hF:
        return json.load(hF)

def main():
    #afreeca_api.init(print_msg, print_msg)
    afreeca_database = load_afreeca_database("afreeca_database.json")
    #choice_dicts = get_online_BJs(afreeca_database, verbose=True, broadlist_filename="test_data/broad_list_api_2.js")
    choice_dicts = get_online_BJs(afreeca_database, verbose=False)
    
    
    
    '''
    onstream_set = ["IcaruS", "leto", "Sonic"]
    forbidden_players = ["Movie", "Hwasin"]
    
    for bj in choice_dicts:
        if (bj["nickname"] in onstream_set) or (bj["nickname"] in forbidden_players):
            choice_dicts.remove(bj)
    
    print()
    print(choice_dicts)
    '''
    
    #BJs = 
    #print(BJs)
    #print(len(get_online_BJs(afreeca_database, broadlist_filename="broad_list_api_2.js")))
    #print(frozenset(bj["nickname"] for bj in get_online_BJs(afreeca_database, broadlist_filename="broad_list_api_2.js")))
    
    #isbjon("sereniss87")

if __name__ == '__main__':
    main()
