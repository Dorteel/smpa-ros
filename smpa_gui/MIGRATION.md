# Migration notes

## 1. From `gui/` (GUI versions 1–3) to the ROS 2 package `smpa_gui/` (version 5)

Version 5 moves the GUI folder with `git mv`, so `git log --follow` keeps each file's
history. Behaviour and HTTP contract are unchanged apart from additions (contract 1.4).

| Before (`gui/`) | Now (`smpa_gui/`) |
|---|---|
| `gui/backend/__main__.py` (`python -m backend`) | `smpa_gui/server.py` (`smpa_gui` console script, `python -m smpa_gui`) |
| `gui/backend/app.py` | `smpa_gui/app.py` |
| `gui/backend/{gateway,ros_graph,ros_names,ros_topics,ros_parameters,ros_interfaces,sensors,action_goals}.py` | `smpa_gui/ros_adapter/…` (same file names) |
| `gui/backend/smpa_runs.py` | `smpa_gui/smpa_adapter/smpa_runs.py` |
| `gui/frontend/` | `smpa_gui/smpa_gui/frontend/` (installed as package data) |
| `gui/tests/` | `smpa_gui/test/` |
| `gui/COLCON_IGNORE` | removed: the folder is now a colcon package |
| — | `package.xml`, `setup.py`, `setup.cfg`, `resource/smpa_gui`, `launch/gui.launch.py`, `MIGRATION.md` |
| `gui/.venv` | `smpa_gui/.venv` (a virtual environment cannot be moved: run `./install_gui.sh` again; the old `gui/.venv` can then be deleted) |

Commands: `./run_gui.sh`, `./install_gui.sh` and `./test_gui.sh` are run from
`smpa_gui/` instead of `gui/`, with the same options plus `--no-smpa` and
`install_gui.sh --build <workspace>`. New: `ros2 run smpa_gui smpa_gui` and
`ros2 launch smpa_gui gui.launch.py` (README.md, "Run").

### Applying the version 5 patch

`smpa-ros-gui-v5-ros2-package.patch` applies on `main` (89462f4, and also 717094b after
the restructuring: in both, `gui/` is identical to GUI version 3) and on
`integration/gui-v3-sense`. In a separate worktree:

```bash
cd ~/ros2_ws/src/smpa-ros
git worktree add ~/smpa-ros-gui-v5 -b gui/v5-ros2-package main
cd ~/smpa-ros-gui-v5
git am "$HOME/ros2_ws/src/smpa-ros/Claude outputs/smpa-ros-gui-v5-ros2-package.patch"
cd smpa_gui && ./install_gui.sh --dev && ./test_gui.sh
```

Nothing outside `gui/` → `smpa_gui/` is touched: no SMPA package, interface, launch
file, `install.sh`, `test.sh` or package manifest is changed.

### Relation to SMPA's ROS 2 package restructuring (Codex)

State seen on 2026-11-18: `main` (717094b) contains the restructuring
(`feature/llm-reorganization`): `smpa_ros/` is now an ament_python package folder
(`smpa_ros/smpa_ros/…`, `smpa_ros/memory.py`, `smpa_ros/config.yaml`), shared inference
moved to the new package `smpa_llm/`, and the root keeps compatibility symlinks.
`install.sh` and `test.sh` build `--paths smpa_ros smpa_llm smpa_ros_interfaces`. The
`gui/` folder on `main` is still GUI version 3.

- `smpa_gui/` is self-contained and sits beside `smpa_ros/` and `smpa_llm/`. SMPA's
  scripts select packages by explicit `--paths`, so they do not build `smpa_gui`; build
  it with `./install_gui.sh --build ~/ros2_ws` (or add `--paths <repo>/smpa_gui`).
- The GUI depends on no SMPA package. Its Dashboard runs SMPA's conversation server
  from the checkout and recognises both the packages layout and the earlier flat one.
- A combined launcher should include `share/smpa_gui/launch/gui.launch.py` (README.md,
  "Including the GUI in another launch file"); no SMPA launch file needs to change.
- SMPA's `README.md` still describes `gui/` (`./gui/install_gui.sh`, `./gui/run_gui.sh`).
  It is Codex/user-owned and was not changed; after merging, those commands become
  `./smpa_gui/install_gui.sh` and `./smpa_gui/run_gui.sh` (or `ros2 launch smpa_gui gui.launch.py`).
- If a GUI worktree is kept inside a colcon workspace, it contains a second copy of
  the SMPA sources; keep worktrees outside `src/` or build with explicit `--paths`.

## 2. From version 5 to version 6 (Dashboard integrated)

GUI version 4 (SMPA Dashboard: conversation through SMPA's own conversation server,
Sense/Belief/Plan Turtle graph views) was written for the `gui/` layout. Version 6 ports
it into the package instead of applying the old patch:

| Version 4 (`gui/`) | Version 6 (`smpa_gui/`) |
|---|---|
| `backend/{smpa_layout,smpa_models,conversation_service,conversation_transcript,rdf_graphs}.py` | `smpa_gui/smpa_adapter/` (same names) |
| `frontend/tabs/smpa/{dashboard,rdf_view}.js` | `smpa_gui/frontend/tabs/smpa/` |
| `mock_graphs/` | `smpa_gui/mock_graphs/` (package data, installed) |
| `layouts/smpa-current.yaml` | `layouts/` (installed to `share/smpa_gui/layouts`) |
| `tests/{test_conversation,test_dashboard_units,test_browser_dashboard}.py`, `tests/fixtures/{openai_stub.py,conversation/}` | `test/…` |
| `--smpa-root`, `--layout` | also launch arguments `smpa_root`, `smpa_layout` |
| contract 1.3 | contract 1.5 (= 1.3 + 1.4) |

Changes made while porting, because SMPA changed or the package requires it:

- **SMPA layout.** The conversation server is found at `smpa_ros/smpa_ros/conversation_web.py`
  (packages layout) or `smpa_ros/conversation_web.py` (flat); the default configuration
  is `smpa_ros/config.yaml`; `PYTHONPATH` gets `smpa_ros` and `smpa_llm`.
- **Configuration split.** The model list follows `inference_config` (providers in
  `smpa_llm/config.yaml`) and finds the registry in `smpa_llm`. SMPA itself resolves
  `smpa_llm` through the sourced ROS workspace; the Dashboard lists that as a
  prerequisite instead of starting a server that would fail.
- **Per-run files are candidate lists.** Defaults prefer the requested
  `world_model/{conversation.md,sense.ttl,belief.ttl,plan.ttl}` and fall back to where
  SMPA writes today (`conversation.md`, `belief-state.ttl`). Version 4 used single paths.
- **Installed operation.** Without a source checkout around it, the GUI does not guess
  an SMPA root: give `smpa_root:=`. Mock graphs are installed with the package.
- **Optional SMPA.** `--no-smpa` now also disables every Dashboard route and the page.
- **Small additions.** Edge inspection (tapping an edge shows its triple) and the elapsed
  time of a running reply.

To move a version 5 worktree to version 6, apply `smpa-ros-gui-v6-conversation.patch`
on the `gui/v5-ros2-package` branch (it changes only `smpa_gui/`), then run
`./install_gui.sh --dev` again (rdflib 7.1.4 is added) and rebuild the package.
`smpa-ros-gui-v4-conversation.patch` is superseded and should not be applied.
