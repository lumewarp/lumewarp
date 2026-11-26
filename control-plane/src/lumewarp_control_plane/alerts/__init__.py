"""Alertmanager ingestion, the user status view and incident history.

Prometheus rules decide what is wrong; the control plane only records what Alertmanager
reports. An alert's `lumewarp_status` label makes it user-visible, `zone` scopes it and the
catalog/upstream labels resolve to the names Lumewarp knows. Nothing here calls the gateway
request path or runs on it.
"""

from lumewarp_control_plane.alerts.receiver import IncomingAlert, receive

__all__ = ["IncomingAlert", "receive"]
