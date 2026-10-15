"""Start a private local review workspace."""
import argparse
from pathlib import Path
import threading
import webbrowser

from .core import Store, create_demo
from .server import serve


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=Path.home()/'.marrow-workbench')
    parser.add_argument('--allow-root',type=Path,action='append',default=[])
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--demo',action='store_true',help='Create a synthetic dataset if none exists')
    parser.add_argument('--open-browser',action='store_true')
    args=parser.parse_args()
    store=Store(args.workspace,args.allow_root)
    if args.demo and not store.list():create_demo(store)
    server=serve(store,args.port)
    url=f'http://127.0.0.1:{server.server_port}/'
    print(f'Marrow Dataset Workbench: {url}',flush=True)
    print('Local only. Original sources are never edited. Ctrl+C to stop.',flush=True)
    if args.open_browser:threading.Timer(.5,lambda:webbrowser.open(url)).start()
    try:server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        server.app.executor.shutdown(wait=True)
        store.db.close()


if __name__=='__main__':main()
