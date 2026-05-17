"""Probe SPGC for each title in the Obsidian Q-list, report what's missing."""
import sys
sys.path.insert(0, "/workspace")
from scripts.rag_tools import find_book

TITLES = [
    ("The Lord of the Rings", "Tolkien"),
    ("The Call of Cthulhu", "Lovecraft"),
    ("The Hobbit", "Tolkien"),
    ("The Old Man and the Sea", "Hemingway"),
    ("The Murder of Roger Ackroyd", "Christie"),
    ("1984", "Orwell"),
    ("Nineteen Eighty-Four", "Orwell"),
    ("At the Mountains of Madness", "Lovecraft"),
    ("The Forsyte Saga", "Galsworthy"),
    ("The Raven", "Poe"),
    ("Bleak House", "Dickens"),
    ("The Picture of Dorian Gray", "Wilde"),
    ("Emma", "Austen"),
    ("David Copperfield", "Dickens"),
    ("Wuthering Heights", "Bronte"),
    ("The Monk", "Lewis"),
    ("Lord Jim", "Conrad"),
    ("Frankenstein", "Shelley"),
    ("The Adventures of Sherlock Holmes", "Doyle"),
    ("Pride and Prejudice", "Austen"),
    ("Dracula", "Stoker"),
    ("Treasure Island", "Stevenson"),
    ("Moby-Dick", "Melville"),
    ("Jane Eyre", "Bronte"),
    ("Crime and Punishment", "Dostoyevsky"),
    ("Adventures of Huckleberry Finn", "Twain"),
    ("Alice's Adventures in Wonderland", "Carroll"),
]

for title, author in TITLES:
    r = find_book(title=title, author=author, top=1)
    matches = r.get("matches", [])
    if matches:
        m = matches[0]
        pgid = m["id"]
        t = (m.get("title") or "")[:50]
        a = (m.get("author") or "")[:30]
        print(f"  OK  {title:40s} -> {pgid:8s} {t!r} by {a!r}")
    else:
        print(f"  --  {title:40s} NOT in SPGC")
