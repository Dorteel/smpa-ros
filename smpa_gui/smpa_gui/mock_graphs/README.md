# Mock graphs (GUI fixtures, not SMPA data)

These Turtle files let the SMPA Dashboard's graph views be developed and tested
without a running SMPA. They are **not** observations, beliefs or plans: every
resource lives in the reserved namespace `https://example.invalid/smpa-gui-mock/`,
each file declares itself mock data, and the GUI shows a MOCK banner whenever
they are displayed.

The GUI only reads these files. It never copies them into a run directory and
never writes any Turtle file. Point `mock_graphs` (or `SMPA_GUI_MOCK_GRAPHS`) at
another folder to use different fixtures.
