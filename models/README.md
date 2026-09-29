# The model goes here

HearAble needs one GGUF, which is not in this repository because it is 707 MB
and not ours to redistribute.

```
nemotron-3.5-asr-streaming-0.6b.q8_0.gguf
741548352 bytes
sha256  a5c435f294eea8f88ce68dd27b8c3bfea7f777cb2fbba04fcd30eaa555f429ae
```

Put it in this directory, or symlink it here. Check the size and the checksum
before building anything: a truncated copy fails much later and much less
clearly than it should.

Any streaming ASR model with a cache-aware encoder will slot into the same
pipeline; this is the one every measurement in `docs/` and `benchmark/` was
taken with, so changing it invalidates the numbers rather than improving them.
