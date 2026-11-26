"""Cloud protocol 1: the byobot logbook through a folder in the person's own cloud storage.

Every file is immutable and has one writer. The keeper (the Mac holding the full store) reads every device's
segments into the store and publishes a plan for the phones; a relay (a phone) writes its own segments and
reads the plan. Segments, checkpoints and plans are encrypted under the epoch's key and signed by their writer.
"""
