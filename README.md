vdrplayer2 README
=================

Tool to replay logs created by OpenCPN Data Monitor in VDR mode.

Use `vdrplayer2 --help` for more info. On windows: `python vdrplayer2 --help`

Copyright(c) Alec Leamas 2025

License: GPL3-or-later 

Installation:
-------------

    python -m build
    pip install dist/*.gz

Status
------

Works for me on Linux using test data in tests/.


Running
-------

All usage requires a corresponding OpenCPN network connection. Sample 
usage:

1. Create a network connection in OpenCPN. TCP connection, m0183 messages
   on port 7788
2. Install vdrplayer2 (see above)
3. Run vdrplayer in a terminal window:
    
        vdrplayer2 -r tcp -m 0183 -p 7788 tests/monitor-0183.csv
4. In OpenCPN, enable the connection.
5. Use Data Monitor to confirm that data is flowing

Although -r signalk sort of works it is brittle. The order described above
must be followed exactly. If not, it "seems" to work, but no actual data 
is transferred

