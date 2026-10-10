# meikipop/anki/deck.py
import html
import logging
import queue
import re
import threading
from typing import Dict, Optional, Set, Tuple

import requests

from meikipop.anki.numpad import NumpadListener
from meikipop.config.config import config
from meikipop.dictionary.lookup import kata_to_hira

logger = logging.getLogger(__name__)

RETRY_INTERVAL_SECONDS = 10
TAG_REGEX = re.compile(r'<[^>]+>')
FURIGANA_REGEX = re.compile(r'\[[^\]]*\]')
SEARCH_SPECIAL_REGEX = re.compile(r'([\\"*_])')

STATE_NEW = 'new'
STATE_PRIORITIZED = 'prioritized'


class AnkiConnectError(Exception):
    pass


class AnkiConnectUnreachable(AnkiConnectError):
    pass


def _key(word: str, reading: str) -> Tuple[str, str]:
    # readings compared in hiragana, since the deck spells katakana words' readings in hiragana
    return word, kata_to_hira(reading or word)


def _clean_field(value: str) -> str:
    value = html.unescape(TAG_REGEX.sub('', value))
    return FURIGANA_REGEX.sub('', value).replace(' ', '').strip()


def _escape_search(text: str) -> str:
    return SEARCH_SPECIAL_REGEX.sub(r'\\\1', text)


def _is_adverb_to(entry) -> bool:
    return any('adv-to' in sense.get('pos', []) for sense in entry.senses)


class AnkiDeck:
    """
    Index of the new cards in the configured Anki deck, keyed by (word, reading).
    Prioritized cards are new cards moved to a negative queue position, so the state survives restarts.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._index: Dict[Tuple[str, str], int] = {}
        self._to_index: Dict[Tuple[str, str], int] = {}  # deck adverbs like ひしひしと, keyed without the と
        self._prioritized: Set[int] = set()
        self._pending: Set[int] = set()
        self._top_position: Optional[int] = None
        self._loader: Optional[threading.Thread] = None
        self._reload_event = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._numpad_handler = None
        self._numpad_listener: Optional[NumpadListener] = None
        self.version = 0  # bumped on every change so the popup knows to re-render

    def _request(self, action: str, **params):
        try:
            response = requests.post(config.anki_connect_url,
                                     json={'action': action, 'version': 6, 'params': params}, timeout=5)
            payload = response.json()
        except (requests.RequestException, ValueError) as e:
            raise AnkiConnectUnreachable(f"AnkiConnect unreachable: {e}") from e
        if payload.get('error'):
            raise AnkiConnectError(f"AnkiConnect '{action}' failed: {payload['error']}")
        return payload['result']

    def set_numpad_handler(self, handler):
        self._numpad_handler = handler

    def start(self):
        """Loads the deck in the background, retrying until Anki is reachable."""
        if not config.anki_enabled:
            return
        if self._numpad_handler and not self._numpad_listener:
            self._numpad_listener = NumpadListener(self._numpad_handler)
            self._numpad_listener.start()
        with self._lock:
            self._reload_event.set()
            if not self._loader:
                self._loader = threading.Thread(target=self._load_loop, daemon=True, name="AnkiDeck")
                self._loader.start()

    def reapply_settings(self):
        if config.anki_enabled:
            self.start()
        else:
            with self._lock:
                self._index, self._to_index, self._prioritized = {}, {}, set()
                self.version += 1

    def _load_loop(self):
        warned = False
        while True:
            with self._lock:
                # exit only when no reload was requested since the last load
                if not config.anki_enabled or not self._reload_event.is_set():
                    self._loader = None
                    return
                self._reload_event.clear()
            try:
                self._load()
                continue
            except AnkiConnectUnreachable as e:
                if not warned:
                    logger.warning(f"Anki: {e}. Retrying every {RETRY_INTERVAL_SECONDS}s in the background.")
                    warned = True
            except AnkiConnectError as e:
                logger.error(f"Anki: Could not load deck '{config.anki_deck}': {e}")
                continue
            except Exception:
                logger.exception("Anki: Unexpected error while loading the deck.")
                continue
            # retry after the interval, or right away when start() requests a reload
            if not self._reload_event.wait(RETRY_INTERVAL_SECONDS):
                self._reload_event.set()

    def _load(self):
        if not config.anki_deck:
            raise AnkiConnectError("no deck configured")
        deck_query = f'"deck:{_escape_search(config.anki_deck)}" -is:suspended is:new'
        word_fields = [f.strip() for f in config.anki_word_fields.split(',') if f.strip()]
        reading_field = config.anki_reading_field.strip()

        new_cards = set(self._request('findCards', query=deck_query))
        prioritized = set(self._request('findCards', query=f'{deck_query} prop:pos<0'))
        notes = self._request('notesInfo', query=deck_query)

        index, to_index = {}, {}
        for note in notes:
            card_ids = [c for c in note.get('cards', []) if c in new_cards]
            if not card_ids:
                continue
            fields = note['fields']
            reading = _clean_field(fields.get(reading_field, {}).get('value', ''))
            for field in word_fields:
                word = _clean_field(fields.get(field, {}).get('value', ''))
                if not word:
                    continue
                word_reading = reading or word
                index.setdefault(_key(word, word_reading), card_ids[0])
                if word.endswith('と') and len(word) > 2 and word_reading.endswith('と'):
                    to_index.setdefault(_key(word[:-1], word_reading[:-1]), card_ids[0])

        with self._lock:
            self._index, self._to_index, self._prioritized = index, to_index, prioritized
            self.version += 1
        if not index:
            logger.warning(f"Anki: No new cards found in deck '{config.anki_deck}'. Check the deck and field names.")
        else:
            logger.info(f"Anki: Loaded {len(new_cards)} new cards from deck '{config.anki_deck}' via AnkiConnect "
                        f"({len(prioritized)} prioritized).")

    def _card_for(self, entry) -> Optional[int]:
        key = _key(entry.written_form, entry.reading)
        card_id = self._index.get(key)
        if card_id is None and key in self._to_index and _is_adverb_to(entry):
            card_id = self._to_index[key]
        return card_id

    def get_state(self, entry) -> Optional[str]:
        card_id = self._card_for(entry)
        if card_id is None:
            return None
        return STATE_PRIORITIZED if card_id in self._prioritized else STATE_NEW

    def prioritize(self, entry):
        """Queues the entry's new card to be moved to the top of the new card queue."""
        with self._lock:
            card_id = self._card_for(entry)
            if card_id is None or card_id in self._prioritized or card_id in self._pending:
                return
            self._pending.add(card_id)
            if not self._worker:
                self._worker = threading.Thread(target=self._prioritize_loop, daemon=True, name="AnkiPrioritize")
                self._worker.start()
        self._queue.put((card_id, entry.written_form))

    def _prioritize_loop(self):
        # a single worker keeps positions unique and in the order the keys were pressed
        while True:
            card_id, written_form = self._queue.get()
            try:
                self._prioritize(card_id, written_form)
            except AnkiConnectError as e:
                logger.warning(f"Anki: Could not prioritize '{written_form}': {e}")
            except Exception:
                logger.exception(f"Anki: Unexpected error while prioritizing '{written_form}'.")
            finally:
                with self._lock:
                    self._pending.discard(card_id)

    def _prioritize(self, card_id: int, written_form: str):
        if not self._request('findCards', query=f'cid:{card_id} is:new -is:suspended'):
            logger.info(f"Anki: '{written_form}' is no longer an unsuspended new card, skipping.")
            with self._lock:
                self._index = {k: v for k, v in self._index.items() if v != card_id}
                self._to_index = {k: v for k, v in self._to_index.items() if v != card_id}
                self.version += 1
            return

        # new card positions are collection-wide; cardsInfo is slow, so only fetch cards below the cached top
        top = self._top_position if self._top_position is not None else 0
        lower = self._request('findCards', query=f'is:new prop:pos<{top}')
        if lower:
            top = min(c['due'] for c in self._request('cardsInfo', cards=lower))
        position = top - 1

        result = self._request('setSpecificValueOfCard', card=card_id, keys=['due'], newValues=[position])
        if result != [True]:
            raise AnkiConnectError(f"setSpecificValueOfCard returned {result}")

        with self._lock:
            self._top_position = position
            self._prioritized.add(card_id)
            self.version += 1
        logger.info(f"Anki: Moved '{written_form}' to the top of the new card queue (position {position}).")


anki_deck = AnkiDeck()
