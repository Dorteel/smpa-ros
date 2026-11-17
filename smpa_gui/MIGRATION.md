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

`smpa-ros-gui-v5-ros2-package.patch` applies on `main` (89462f4, whose `gui/` is
identical to GUI version 3) and on `integration/gui-v3-sense`. In a separate worktree:

```bash
cd ~/ros2_ws/src/smpa-ros
git worktree add ~/smpa-ros-gui-v5 -b gui/v5-ros2-package main
cd ~/smpa-ros-gui-v5
git am "$HOME/ros2_ws/src/smpa-ros/Claude outputs/smpa-ros-gui-v5-ros2-package.patch"
cd smpa_gui && ./install_gui.sh --dev && ./test_gui.sh
```

Nothing outside `gui/` → `smpa_gui/` is touched: no SMPA package, interface, launch
file, `install.sh`, `test.sh`, root `package.xml` or root `setup.py` is changed.

### Relation to the ongoing ROS 2 package restructuring (Codex)

- `smpa_gui/` is self-contained. It can stay at the repository root or move unchanged
  into whatever package folder layout Codex introduces (for example `src/smpa_gui/`).
  It refers to nothing outside itself except one default: when run from a source
  checkout, the SMPA tab looks for `memory/` beside the `smpa_gui/` folder. After a
  move, pass `smpa_memory_root:=` (or `--memory-root`) instead.
- SMPA's build scripts select packages with explicit `--paths`, so `smpa_gui` is not
  built by them. A combined build should add `--paths <repo>/smpa_gui`, built with the
  GUI's interpreter if the console script should use `smpa_gui/.venv` (README.md,
  "Python environment").
- The root `setup.py` collects only `smpa_ros`, `speech` and `sense`; `smpa_gui` is not
  picked up by it. If Codex changes that `find_packages` call, `smpa_gui` must stay
  excluded.
- A combined launcher should include `share/smpa_gui/launch/gui.launch.py` (README.md,
  "Including the GUI in another launch file"); no SMPA launch file needs to change
  for the GUI.
- If a GUI worktree is kept inside a colcon workspace, it contains a second copy of
  the SMPA sources; keep worktrees outside `src/` or build with explicit `--paths`.

## 2. The GUI version 4 conversation dashboard (not integrated)

GUI version 4 (SMPA Dashboard: conversation through SMPA's own conversation server,
Sense/Belief/Plan Turtle graph views) exists as a commit on the GUI branch
`gui/v4-conversation` and as the patch `smpa-ros-gui-v4-conversation.patch` in
`Claude outputs/`. It was written for the `gui/` layout and has not been merged. It is
**not** duplicated in version 5; it is preserved as it is.

### Trial

Applying the version 4 patch with `git am -3` on top of version 5 was tried: files that
version 4 changed and version 5 moved follow the move (for example
`gui/backend/app.py` → `smpa_gui/smpa_gui/app.py`), but there are content conflicts in
`app.py`, `style.css`, `test_gui.sh`, two integration tests and three documents, and
the files version 4 *adds* are created under the old `gui/` paths. The port is
therefore a manual but mechanical step.

### Port procedure

1. Start from the version 5 branch: `git checkout -b gui/v6-conversation gui/v5-ros2-package`.
2. `git am -3 smpa-ros-gui-v4-conversation.patch`, then resolve:
   - New backend modules go to the optional SMPA adapter:
     `gui/backend/{smpa_layout,smpa_models,conversation_service,conversation_transcript}.py`
     → `smpa_gui/smpa_gui/smpa_adapter/`; `rdf_graphs.py` likewise (it reads SMPA's
     graph files). Imports `backend.X` become `smpa_gui.smpa_adapter.X`.
   - `gui/frontend/tabs/smpa/{dashboard,rdf_view}.js` → `smpa_gui/smpa_gui/frontend/tabs/smpa/`.
   - `gui/mock_graphs/` → `smpa_gui/smpa_gui/mock_graphs/` and add it to `package_data`
     in `setup.py` (the frontend glob does not cover it), so mock graphs exist in an
     installed package; `gui/layouts/` → `smpa_gui/layouts/` and install it to
     `share/smpa_gui/layouts`.
   - Tests and fixtures: `gui/tests/…` → `smpa_gui/test/…`, with `'tests'` paths
     changed to `'test'`.
   - `app.py`: keep version 5's routes and access rules; add version 4's conversation
     routes, which use the same `authorised` check (now named for all changes). The
     conversation routes must also answer "disabled" when `smpa_enabled` is false, and
     the managed conversation server must be stopped in the same lifespan end.
   - `rdflib==7.1.4` goes into `requirements.txt` and `<exec_depend>python3-rdflib</exec_depend>`
     into `package.xml`.
   - `server.py`: add version 4's `--layout` and `--smpa-root`; `launch/gui.launch.py`:
     add matching `smpa_layout` and `smpa_root` arguments (passed only when `smpa` is true).
     For an installed package, `smpa_root` has no useful default and must be given.
   - Contract version: merge 1.3 (conversation) and 1.4 into 1.5.
3. Run `./test_gui.sh --browser` with `SMPA_GUI_TEST_SMPA_ROOT` pointing at an SMPA
   checkout that has its `.venv`.

Version 4's requests to Codex (SMPA_CONTRACTS.md C6–C9 in that patch: transcript in
`world_model/conversation.md`, producer contract for `sense/belief/plan.ttl`, model
descriptions, conversation-server conveniences) are unaffected by the packaging.
