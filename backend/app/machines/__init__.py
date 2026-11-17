"""Machines: SSH hosts the app works on like on the server itself, and the bastions (jump hosts) they are reached through.

`conn` builds the ssh command lines (one shared connection per host, bastion chains, the app's host keys), `registry`
keeps the list (``[[development.hosts]]`` of the config file plus machines added in the app), `health` checks each hop
of the path, `hostkeys` shows and trusts host keys, `probe` reports what a machine has (OS, CPU, GPUs, tools, sudo).
"""
