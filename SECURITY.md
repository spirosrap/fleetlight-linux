# Security

Fleetlight monitors computers explicitly configured by the user. It relies on OpenSSH host-key verification and existing key-based access; it does not provision credentials or weaken SSH configuration.

Do not post private fleet configuration, SSH material or unredacted diagnostics in public issues. Report a suspected security problem through this repository's GitHub private vulnerability reporting when available. If private reporting is unavailable, open an issue containing only a request for a private contact channel, without sensitive details.

The first Linux release is a desktop tool for trusted local users. It does not expose a network server or execute commands supplied by a remote probe response. System-update actions require an explicit UI confirmation and an interactive package-manager session.
