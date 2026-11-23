# mida + VOCR6 + Screen AI (local)

The [mida + VOCR6](../mida_vocr6/README.md) provider with Chrome Screen AI as a second opinion. Needs both providers set
up.

1. mida + VOCR6 read every line, and VOCR6 gives each character a confidence.
2. If any kana or kanji is below `THRESHOLD` (0.85), Screen AI reads the area around all such lines in one call.
   Unsure punctuation and Latin never trigger it.
3. Each unsure kana or kanji is replaced by the Screen AI character nearest its cell's center (taken only from a Screen
   AI line running through the same line), if that one is a different kana or kanji with a higher confidence
   (plus `MARGIN`).

Replacements are logged at INFO level (`Screen AI: 覧 (0.62) -> 鷺 (1.00) in 覧沢`), once until they change between
scans; kept characters at DEBUG.

When VOCR6 is sure of everything, Screen AI never runs and the speed is that of mida + VOCR6. Otherwise one scan costs
about one extra Screen AI call (~100-150 ms).

## Limits

- Only substitutes characters: a character VOCR6 skipped is not added, and one Screen AI skipped can't be compared.
- A confident VOCR6 misread is never checked.
