# Server release

`install_server.sh RELEASE_DIRECTORY` installs the authenticated controller and an HTTPS tunnel under systemd on the existing Ubuntu host. Deploy an archive of the tested Git commit into a new `/home/ubuntu/strategy-control/releases/COMMIT` directory, then run the installer. Account state stays under the legacy project's `data/strategy_lab`, independently of release code. The old unattended scheduler is disabled; the legacy dashboard and history remain intact.

The service starts with no authorized sessions. `public-origin.txt` holds the current HTTPS URL; `control.token` holds the dashboard password and has owner-only permissions. Never commit either file. The quick tunnel address changes when its process restarts. Use a named Cloudflare tunnel/domain for a stable URL and stronger access policy when a domain is available.

Rollback: stop the new services, set `current` to the directory saved in `previous-release.txt`, then restart the new services. To restore the legacy scheduler instead, first inspect exposure and saved `previous-scheduler-*.txt`, stop the new controller/tunnel, and explicitly re-enable the old scheduler if it was previously enabled. Never run both strategy schedulers against one account inadvertently. Stopping the controller retains unresolved paper positions for recovery.
