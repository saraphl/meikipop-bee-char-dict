# meikipop bridge: lets meikipop move new cards to the top of the new card queue,
# which AnkiConnect no longer allows (it rejects edits of a card's position).
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from anki.collection import OpChanges
from anki.consts import CARD_TYPE_NEW, QUEUE_TYPE_SUSPENDED
from anki.errors import NotFoundError
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp

DEFAULT_PORT = 8766
MAIN_THREAD_TIMEOUT_SECONDS = 5  # keep below meikipop's request timeout
MAX_BODY_BYTES = 1024

_server = None


class _Result:
    # CollectionOp needs an object with 'changes'; 'body' is the JSON reply
    def __init__(self, body: dict, changes: OpChanges):
        self.body, self.changes = body, changes


def prioritize(col, card_id: int) -> _Result:
    """Moves a new card above every other new card in the collection, using negative positions."""
    try:
        card = col.get_card(card_id)
    except NotFoundError:
        return _Result({'skipped': 'card not found', 'stale': True}, OpChanges())
    if card.type != CARD_TYPE_NEW or card.queue == QUEUE_TYPE_SUSPENDED:
        return _Result({'skipped': 'not an unsuspended new card', 'stale': True}, OpChanges())
    if card.odid:
        return _Result({'skipped': 'card is in a filtered deck'}, OpChanges())
    # cards in filtered decks keep their position in odue
    top = col.db.scalar('select min(case when odid then odue else due end) from cards where type = ?', CARD_TYPE_NEW)
    card.due = min(top or 0, 0) - 1
    return _Result({'position': card.due}, col.update_card(card))


def _prioritize_on_main(card_id: int) -> dict:
    # runs as a CollectionOp on Anki's main thread, so undo and open screens are updated as for any other edit
    done, abandoned, reply = threading.Event(), threading.Event(), {}

    def finish(body: dict):
        reply.update(body)
        done.set()

    def start():
        if abandoned.is_set():  # meikipop already gave up on this request
            return
        if not mw.col:
            return finish({'error': 'no collection open'})
        try:
            CollectionOp(parent=mw, op=lambda col: prioritize(col, card_id)) \
                .success(lambda result: finish(result.body)) \
                .failure(lambda e: finish({'error': str(e)})) \
                .run_in_background()
        except Exception as e:
            finish({'error': str(e)})

    mw.taskman.run_on_main(start)
    if done.wait(MAIN_THREAD_TIMEOUT_SECONDS):
        return reply
    abandoned.set()
    return {'error': "Anki's main thread is busy"}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        port = self.server.server_address[1]
        # refuse requests made by web pages, including via DNS rebinding
        if self.headers.get('Origin') or self.headers.get('Host') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            return self._reply(403, {'error': 'forbidden'})
        if self.path != '/prioritize':
            return self._reply(404, {'error': 'unknown endpoint'})
        try:
            length = int(self.headers.get('Content-Length', 0))
            if not 0 < length <= MAX_BODY_BYTES:
                raise ValueError
            card_id = int(json.loads(self.rfile.read(length))['card'])
        except (ValueError, KeyError, TypeError):
            return self._reply(400, {'error': 'expected {"card": <card id>}'})
        self._reply(200, _prioritize_on_main(card_id))

    def _reply(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _start():
    global _server
    port = (mw.addonManager.getConfig(__name__) or {}).get('port', DEFAULT_PORT)
    try:
        _server = ThreadingHTTPServer(('127.0.0.1', port), _Handler)
    except OSError as e:
        print(f"meikipop bridge: could not listen on port {port}: {e}")
        return
    threading.Thread(target=_server.serve_forever, daemon=True, name="meikipop bridge").start()


def _stop():
    global _server
    if _server:
        _server.shutdown()
        _server.server_close()
        _server = None


gui_hooks.profile_did_open.append(_start)
gui_hooks.profile_will_close.append(_stop)
