"""The lasto.dev site build. Run it through site/build.py.

It never imports the lasto package. Facts about the safety core come from parsing its
source with ast (see sitegen.safety), so the site can't drift from the code and the
build can't execute anything that talks to hardware.
"""
