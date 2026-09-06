# Updater sources

The desktop update and release-check scripts are adapted from the MIT-licensed
[Fleetlight macOS](https://github.com/spirosrap/fleetlight-macos) shared updater
builders (commit `08355a6`). The CLI updater follows the same native installation
selection. Copyright (c) 2026 Fleetlight contributors; distributed under the repository MIT license.

The Linux controller adds reviewed-target checks and progress messages. Scripts
run only through an explicit update job, except desktop_check.sh, which refreshes
repository metadata without installing packages. Do not invoke update scripts
without the expected-version environment supplied by the controller.
