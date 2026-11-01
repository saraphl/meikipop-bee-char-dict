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
BRIDGE_TIMEOUT_SECONDS = 10  # longer than the add-on's own wait for Anki's main thread
TAG_REGEX = re.compile(r'<[^>]+>')
FURIGANA_REGEX = re.compile(r'\[[^\]]*\]')
SEARCH_SPECIAL_REGEX = re.compile(r'([\\"*_])')

STATE_NEW = 'new'
STATE_PRIORITIZED = 'prioritized'
STATE_SUSPENDED = 'suspended'


class AnkiError(Exception):
    pass


class AnkiUnreachable(AnkiError):
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
    Index of the new and suspended cards in the configured Anki deck, keyed by (word, reading).
    Prioritized cards are new cards positioned below `anki_prioritized_below` (the add-on uses negative positions),
    so the state survives restarts.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._index: Dict[Tuple[str, str], int] = {}
        self._to_index: Dict[Tuple[str, str], int] = {}  # deck adverbs like ひしひしと, keyed without the と
        self._prioritized: Set[int] = set()
        self._suspended: Set[int] = set()
        self._pending: Set[int] = set()
        self._loader: Optional[threading.Thread] = None
        self._reload_event = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._numpad_handler = None
        self._numpad_listener: Optional[NumpadListener] = None
        self.version = 0  # bumped on every change so the popup knows to re-render

    @staticmethod
    def _post_json(url: str, payload: dict, name: str, timeout: float, hint: str = '') -> dict:
        try:
            response = requests.post(url, json=payload, timeout=timeout).json()
        except requests.Timeout as e:
            raise AnkiUnreachable(f"{name} didn't respond in time") from e
        except (requests.RequestException, ValueError) as e:
            raise AnkiUnreachable(f"{name} unreachable{hint}: {e}") from e
        if not isinstance(response, dict):
            raise AnkiError(f"unexpected response from {name}: {response!r}")
        return response

    def _request(self, action: str, **params):
        payload = self._post_json(config.anki_connect_url, {'action': action, 'version': 6, 'params': params},
                                  'AnkiConnect', timeout=5)
        if payload.get('error'):
            raise AnkiError(f"AnkiConnect '{action}' failed: {payload['error']}")
        return payload.get('result')

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
                self._index, self._to_index, self._prioritized, self._suspended = {}, {}, set(), set()
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
            except AnkiUnreachable as e:
                if not warned:
                    logger.warning(f"Anki: {e}. Retrying every {RETRY_INTERVAL_SECONDS}s in the background.")
                    warned = True
            except AnkiError as e:
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
            raise AnkiError("no deck configured")
        deck = f'"deck:{_escape_search(config.anki_deck)}"'
        deck_query = f'{deck} -is:suspended is:new'
        word_fields = [f.strip() for f in config.anki_word_fields.split(',') if f.strip()]
        reading_field = config.anki_reading_field.strip()

        new_cards = set(self._request('findCards', query=deck_query))
        prioritized = set(self._request('findCards', query=f'{deck_query} prop:pos<{config.anki_prioritized_below}'))
        suspended = set(self._request('findCards', query=f'{deck} is:suspended'))
        notes = self._request('notesInfo', query=f'{deck} (is:new OR is:suspended)')

        index, to_index = {}, {}
        for note in notes:
            cards = note.get('cards', [])
            card_ids = [c for c in cards if c in new_cards]
            if not card_ids and all(c in suspended for c in cards):  # skip notes with another card in study
                card_ids = cards
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
            self._index, self._to_index, self._prioritized, self._suspended = index, to_index, prioritized, suspended
            self.version += 1
        if not index:
            logger.warning(f"Anki: No new or suspended cards found in deck '{config.anki_deck}'. Check the deck and field names.")
        else:
            marked_suspended = suspended.intersection(index.values())
            logger.info(f"Anki: Loaded {len(new_cards)} new ({len(prioritized)} prioritized) and {len(marked_suspended)} "
                        f"suspended cards from deck '{config.anki_deck}' via AnkiConnect.")

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
        if card_id in self._suspended:
            return STATE_SUSPENDED
        return STATE_PRIORITIZED if card_id in self._prioritized else STATE_NEW

    def promote(self, entry):
        """Queues the entry's card to be unsuspended and, if it's new, moved to the top of the new card queue."""
        with self._lock:
            card_id = self._card_for(entry)
            if card_id is None or card_id in self._prioritized or card_id in self._pending:
                return
            self._pending.add(card_id)
            if not self._worker:
                self._worker = threading.Thread(target=self._promote_loop, daemon=True, name="AnkiPromote")
                self._worker.start()
        self._queue.put((card_id, entry.written_form))

    def _promote_loop(self):
        # a single worker keeps positions unique and in the order the keys were pressed
        while True:
            card_id, written_form = self._queue.get()
            try:
                self._promote(card_id, written_form)
            except AnkiError as e:
                logger.warning(f"Anki: Could not unsuspend or reposition '{written_form}': {e}")
            except Exception:
                logger.exception(f"Anki: Unexpected error while promoting '{written_form}'.")
            finally:
                with self._lock:
                    self._pending.discard(card_id)

    def _drop(self, card_id: int):
        # caller holds the lock
        self._index = {k: v for k, v in self._index.items() if v != card_id}
        self._to_index = {k: v for k, v in self._to_index.items() if v != card_id}
        self._suspended.discard(card_id)
        self.version += 1

    def _promote(self, card_id: int, written_form: str):
        # AnkiConnect rejects position edits, so meikipop's own Anki add-on (anki_addon/meikipop_bridge) does both
        response = self._post_json(f'{config.anki_bridge_url}/promote', {'card': card_id},
                                   "meikipop's Anki add-on", timeout=BRIDGE_TIMEOUT_SECONDS, hint=' (is it installed?)')
        if 'error' in response:
            hint = ' (update it in addons21 and restart Anki)' if response['error'] == 'unknown endpoint' else ''
            raise AnkiError(f"meikipop's Anki add-on failed: {response['error']}{hint}")

        if 'skipped' in response:
            logger.info(f"Anki: Skipped '{written_form}': {response['skipped']}.")
            if response.get('stale'):  # no longer a new or suspended card, so drop its marker
                with self._lock:
                    self._drop(card_id)
            return
        position = response.get('position')
        if 'position' not in response or (position is not None and not isinstance(position, int)):
            raise AnkiError(f"unexpected response from meikipop's Anki add-on: {response!r}")

        with self._lock:
            if position is None and not response.get('new'):  # a known card was only unsuspended
                self._drop(card_id)
            else:
                self._suspended.discard(card_id)
                if position is not None:
                    self._prioritized.add(card_id)
                self.version += 1
        if position is None:
            logger.info(f"Anki: Unsuspended '{written_form}'.")
        else:
            moved = 'Unsuspended and moved' if response.get('unsuspended') else 'Moved'
            logger.info(f"Anki: {moved} '{written_form}' to the top of the new card queue (position {position}).")


anki_deck = AnkiDeck()
