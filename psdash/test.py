#!/usr/bin/python
import time
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler


class MyHandler(FileSystemEventHandler):
    def on_modified(self, event):
        print {'event_type': event.event_type,  'path':  event.src_path}

if __name__ == "__main__":
    event_handler = MyHandler()
    observer = Observer()
    observer.schedule(event_handler, path='/opt/stack/', recursive=False)
    observer.start()
    print "hello"
    while True:
        time.sleep(1)

    observer.join()
    
    print "hello1111111"
