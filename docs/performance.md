# Performance and VPS limits

## Production target

The screenshot path is tuned for the production host, not for the number of
logical CPUs reported by a development machine:

- **CPU:** 1 shared vCPU
- **Memory:** 1 GB RAM
- **Disk:** 25 GB SSD

Local OCR is CPU-bound. Running tile readers in parallel therefore adds process
and memory pressure without shortening one request on this host. Point values are
now submitted as separate pages to one Tesseract process. `OMP_THREAD_LIMIT=1`
is set by the application when the operator has not supplied a value, and remains
explicit in `ops/feutsolver.env.example`.

The request semaphore still permits two simultaneous OCR requests. That preserves
the existing multi-session behaviour, even though two active requests necessarily
share the one vCPU. The solver and external-OCR limits are unchanged.

Streamlit source polling and hot reload are disabled in the checked-in production
configuration. Releases are activated atomically and restarted through
`feutsolver.path`, so polling the immutable checkout only steals CPU from requests.
For local development, hot reload can be enabled explicitly:

```bash
streamlit run app.py --server.runOnSave=true --server.fileWatcherType=poll
```

## Reproducible measurement

`ops/benchmark_screenshot_pipeline.py` measures trusted image validation, local
OCR, word suggestions, generation of at most twelve solutions, and JSON
serialization. The lexicon and exact suggestion-membership set are loaded before
the timer, matching the application's background startup preload. Browser upload,
network transfer, Streamlit transport, and client rendering are intentionally
outside the interval because they depend on the client and connection.

Run the benchmark sequentially with the production CPU limit:

```bash
OMP_THREAD_LIMIT=1 FEUTSOLVER_RUNTIME_CACHE_WRITE=0 \
  .venv/bin/python ops/benchmark_screenshot_pipeline.py \
  tests/screenshots/IMG_5913.PNG tests/screenshots/IMG_6091.png \
  --warmups 1 --repetitions 5
```

On 2026-09-29, the pre-change path at Git revision
`75185c155ab003638daa1b5aa84b0d3fc7f80ae9` and the optimized path were measured
on the same arm64 development host. The table contains five-run warm medians after
one warmup; requests were processed sequentially. The included
[`performance-baseline-2026-09-29.json`](performance-baseline-2026-09-29.json)
records the baseline command, environment, phase medians, individual totals, and
payload hashes. `FEUTSOLVER_BENCHMARK_ROOT` lets the benchmark target an exported
checkout of that revision without modifying the active tree.

| Screenshot | Board tiles | Before | After | Speed-up | Confidence |
| --- | ---: | ---: | ---: | ---: | ---: |
| `IMG_5913.PNG` | 76 | 1.303 s | 0.477 s | **2.73×** | 95.4% |
| `IMG_6091.png` | 100 | 1.362 s | 0.384 s | **3.55×** | 95.3% |

A fresh-process first request for `IMG_5913.PNG` fell from 2.507 s to 0.582 s
(**4.31×**). The optimized process startup imports the Python Tesseract adapter,
and the existing background preload completes the same word-membership cache,
before request timing; no screenshot result is precomputed.

OCR remains the dominant phase. In the final run it took 0.453 s and 0.367 s;
suggestions took about 0.002 s, solution generation 0.021 s and 0.014 s, and
serialization less than 0.001 s. Across all nine checked-in screenshots the final
median was 0.202–0.578 s.

These are algorithm comparisons, not a production latency promise. The shared VPS
can have different CPU contention, and client/network time is not included. Repeat
the command on the VPS after deployment before publishing a live latency target.

## What changed

- Local OCR consumes the already verified RGB image directly. The bounded,
  lossless PNG is still created for explicit external OCR or an actual external
  fallback, but no longer encoded, flushed, and decoded for the normal local path.
- Board localization, colour matching, rack classification, glyph thresholding,
  and profile comparison use bounded NumPy operations with the same thresholds and
  decision rules. The board locator converts only 31 sampled columns to float64,
  avoiding a full-image 480 MB allocation at the maximum accepted resolution.
- Weak point glyphs keep their old canvases, Tesseract engine, page segmentation,
  character whitelist, parsing, and reconciliation. They are independent pages in
  one TIFF, so one process start replaces six to eight process starts on the heavy
  screenshots. If the batch itself fails or times out, the exceptional path falls
  back to the previous independent per-glyph reader so successful point evidence
  is never discarded with a failed page.
- Strong glyph profiles still skip point OCR only where the reconciliation rule
  could not change the result. The solver, move ordering, confidence calculation,
  validation limits, pending-move checks, and external OCR path are unchanged.
- The existing background lexicon preload now also warms the exact normalized
  membership set used by word suggestions. This moves roughly 0.49 s of one-time
  work out of the first screenshot request without adding a worker or changing the
  set's contents.

## Accuracy and regression guardrails

All nine real-screenshot golden OCR regressions still match their exact expected
board, rack, bonuses, and confidence. For every screenshot that invokes point OCR,
the batched reader is also compared page-for-page with the previous individual
reader; all results are identical, including unreadable values represented by
`None`. Each benchmark repetition produced the same serialized payload hash.

Keep the full screenshot suite and the batch-versus-individual comparison when
changing crop geometry, glyph thresholds, Tesseract options, or profile matching.
The SSD is not a request-time bottleneck: deployment prebuilds the lexicon cache,
and production runtime cache writes remain disabled.
