"""Shared sanity bounds for any chars-per-second value.

Single source of truth for both:
  - the gateway-side calibrator (gateway/voice_speed_calibrator.py)
    which writes calibration results to the DB
  - the runtime-side catalog client (voice_speed_catalog.py) which
    reads them back into the pipeline

Splitting these constants into their own zero-dependency module avoids
the calibrator (which runs in the gateway container without ``requests``
installed) needing to import the full catalog module.

Values outside this range almost always reflect a broken synth response
rather than a legitimately-extreme voice, so we reject them before they
poison downstream rewrite decisions.
"""

MIN_VALID_CPS: float = 2.0
MAX_VALID_CPS: float = 8.0
