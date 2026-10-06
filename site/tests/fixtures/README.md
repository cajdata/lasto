# Test fixtures

## `app/`: a frozen copy of what the site reads from the app

`app/src/lasto/` holds the statements `sitegen` reads from each app file (`sitegen/safety.py`
and `sitegen/appfacts.py`): the safety core's limits and lists, `cli.py`'s command table and
`build_parser`, and the capture timings. They're copied verbatim from the app at dab6e21 (Phase 2
as approved), with nothing else from those files, except a constant a kept statement names (like
`HOTKEY_ID` in the `RegisterHotKey` call) so it reads as it does in the app. No transmit code, and
nothing here is ever imported or run: the site only parses it with `ast`.

The site's tests read this copy instead of the live app. Their expected values are written in
the tests and match this copy, so a change to the app, like Phase 3 adding a command or changing
a timing, can't fail them. What the live pages say about the live app is checked by the strict
build, which reads the real source.

Change these files only to test the extractor against a new shape the app has adopted, and then
update the expected values in the tests with them. Never refresh them to make a failing test pass.
