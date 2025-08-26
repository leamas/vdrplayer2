vdrplayer2 README
=================

Tool to replay logs created by OpenCPN Data Monitor in VDR mode.

Use `vdrplayer2 --help` for more info. On windows: `python vdrplayer2 --help`

Licensing:
----------

Copyright (c) Alec Leamas 2025

License: GPL3-or-later 

Kudos: Dan Dickey a k a Transmitterdan for the original VDRplayer.py script
at https://github.com/transmitterdan/VDRplayer.git which has been the
inspiration for this.


Installation:
-------------

    $ python -m build
    $ pip install dist/*.gz

Status
------

Works for me on Linux using test data in *vdrplayer2/data*.


Running
-------

All usage requires a corresponding OpenCPN network connection. Sample 
usage:

1. Start OpenCPN and create a network connection. TCP connection,
   m0183 messages on port 7788. Keep the connection disabled.
2. Install vdrplayer2 (see above)
3. Run vdrplayer in a terminal window:
   
       $ DATADIR=vdrplayer2/data 
       $ vdrplayer2 -r tcp -m 0183 -p 7788 $DATADIR/monitor-0183.csv

   This value of DATADIR is if running from a clone of this repo.
   If not, pip installs the test files in a library location
   like *~.local/lib/python3.13/site-packages/vdrplayer2/data/*, YMMV
4. In OpenCPN, enable the connection.
5. Use Data Monitor to confirm that data is flowing

Although `-r signalk` sort of works it is brittle. The order described above
must be followed exactly. If not, it "seems" to work, but no data 
is transferred

