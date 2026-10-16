"""Organizer lookup, in two independent steps:

    find.py   who runs this event series, and what is their website?   (once per series, cached)
    crawl.py  read an organizer's website: every upcoming event, contacts, vendor info   (on demand)

find never reads more than it needs to name the organizer; crawl never guesses who the organizer is.
"""
