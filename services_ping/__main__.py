from __future__ import annotations
import argparse, signal
from pathlib import Path
from . import Monitor, Store, create_server
def main():
    p=argparse.ArgumentParser(); p.add_argument("--port",type=int,default=8765); p.add_argument("--data-dir",type=Path,default=Path.home()/".services-ping"); args=p.parse_args()
    store=Store(args.data_dir); monitor=Monitor(store); server=create_server(monitor,port=args.port); monitor.start(); print(f"Services Ping: http://127.0.0.1:{server.server_address[1]}",flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); monitor.close(); store.close()
if __name__ == "__main__": main()
