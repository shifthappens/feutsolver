# Performance and VPS limits

## Production target

Use this resource profile when changing the screenshot-to-solutions path:

- **CPU:** 1 shared vCPU. The OS may report more CPUs than the VPS CPU quota allows.
- **Memory:** 1 GB RAM.
- **Disk:** 25 GB SSD.

The app is CPU-bound while it finds the board and reads tile glyphs. More local
tile workers do not make one screenshot faster on this host; they compete for the
same CPU and use more memory. The production example therefore caps the
process-wide local tile OCR pool at one worker and limits Tesseract's OpenMP
threads to one. The separate request semaphore still allows two OCR requests at
once, so those requests can compete for the same vCPU. Keep that limit intact to
preserve existing multi-session capacity. The explicit tile-worker setting also
handles VPS CPU quotas that Python cannot see through CPU affinity.

`ops/feutsolver.env.example` is the reference configuration. The live service must
load its environment file for these limits to apply.

## Current single-worker measurements

On 2026-09-28, the previous local OCR path was replayed and compared with the
optimized path on the same development host. Both runs used a process-wide local
tile OCR pool capped at one worker and `OMP_THREAD_LIMIT=1`. Requests were
processed sequentially; simultaneous-request throughput was not measured. The
timed interval included image validation and local OCR,
word suggestions, generating up to twelve legal moves, and serializing the result
as JSON. The lexicon was already loaded, as it normally is after the app's startup
preload. These timings exclude Streamlit transport and browser rendering.

| Screenshot | Board tiles | Previous path | Optimized path | Confidence | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `IMG_5913.PNG` | 76 | 6.57 s and 7.86 s (two runs) | 1.23 s median (three runs) | 95.4% | Serialized output identical |
| `IMG_6091.png` | 100 | — | 1.30 s median (three runs) | 95.3% | Identical across runs |

For `IMG_5913.PNG`, local point-value OCR fell from 81 Tesseract calls to 6.
The previous and optimized complete outputs matched, including the recognized
board and rack, confidence, dictionary suggestions, ordered moves, and serialized
payload. The 114 Python tests, including the real-screenshot OCR regressions, pass.

The benchmark host is not the production VPS. It had more than one CPU available,
although this measurement forced local OCR to one worker. Treat the times as an
algorithm comparison, not a promise for the shared VPS. Re-measure on the VPS
after deployment before publishing a production latency target.

## Changes that help on one CPU

- Rack detection now caches a compact one-byte-per-pixel tile mask. The original
  scan repeatedly classified the same pixels when it looked for tile rows and
  then measured each tile's height. A local timing on `IMG_5913.PNG` reduced this
  step from 0.786 s to 0.172 s, with the same detected rack.
- Large glyphs with a strong Wordfeud template match skip tiny point-value OCR.
  The existing reconciliation rules ignore a point conflict for those strong
  matches, so launching Tesseract could not alter their result. Point OCR still
  runs for weaker matches and for the existing Q fallback.
- The local tile-worker cap and `OMP_THREAD_LIMIT=1` avoid parallel CPU work that
  a one-vCPU quota cannot run at the same time.
- Known-word suggestions are already cached against the wordlist content digest;
  this avoids renormalizing the full list after every screenshot.

## Further work to consider

The remaining Tesseract calls are limited to weaker glyph matches. A persistent
Tesseract API or batched point-value reader could reduce process startup further,
but it must return the same value for every crop, including uncertain and failed
reads, before replacing the current checks. Reworking connected-component scans
in `_dark_components` may also help CPU use, but needs the full screenshot
regression set because it directly affects letter shapes and confidence.

The SSD is not the current request-time bottleneck. The deploy workflow prebuilds
the lexicon cache and runtime cache writes are disabled in production. Preserve
those behaviours to avoid rebuilding or writing the wordlist cache during a user
request.
