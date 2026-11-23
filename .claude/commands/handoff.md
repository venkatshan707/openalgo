Invoke the `handoff` skill to write session handoff files.

The handoff skill will:
1. Synthesize everything worked on in this session
2. Write `My_Docs/handoffs/latest_<session_name>.md` (overwritten each run)
3. Write `My_Docs/handoffs/handoff_<session_name>_<timestamp>.md` (permanent archive)

Derive the session name from the current topic unless the user passed one as an argument: $ARGUMENTS
