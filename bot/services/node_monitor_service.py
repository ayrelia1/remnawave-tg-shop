import asyncio
import logging
from typing import Dict, Optional, Tuple

from config.settings import Settings
from bot.services.panel_api_service import PanelApiService
from bot.services.notification_service import NotificationService


def _node_is_connected(node: dict) -> bool:
    """Determine if a node is online.

    Remnawave API may use different field names depending on version:
    - isConnected (bool)
    - isOnline (bool)
    - status == "online" / "connected"
    """
    if "isConnected" in node:
        return bool(node["isConnected"])
    if "isOnline" in node:
        return bool(node["isOnline"])
    status = str(node.get("status", "")).lower()
    if status:
        return status in ("online", "connected", "active")
    # If no status field, assume connected
    return True


def _node_key(node: dict) -> str:
    """Return a stable identifier for the node."""
    return node.get("uuid") or node.get("id") or node.get("name") or str(node)


def _node_display_name(node: dict) -> str:
    return node.get("name") or node.get("uuid") or "Unknown"


def _node_address(node: dict) -> str:
    addr = node.get("address") or node.get("host") or node.get("ip") or ""
    port = node.get("port")
    if port and addr:
        return f"{addr}:{port}"
    return addr or "N/A"


class NodeMonitorService:
    """Periodically polls Remnawave panel for node status and sends alerts on changes."""

    def __init__(
        self,
        settings: Settings,
        panel_service: PanelApiService,
        notification_service: NotificationService,
    ):
        self.settings = settings
        self.panel_service = panel_service
        self.notification_service = notification_service
        # key → (is_connected, display_name, address)
        self._last_states: Dict[str, Tuple[bool, str, str]] = {}
        self._task: Optional[asyncio.Task] = None
        self._initialized = False

    async def check_nodes(self) -> Optional[str]:
        """Fetch current node states, compare with last known, send alerts.

        Returns a status summary string or None on API failure.
        """
        nodes = await self.panel_service.get_nodes()
        if nodes is None:
            logging.warning("NodeMonitor: failed to fetch nodes from panel API")
            return None

        lines = []
        new_states: Dict[str, Tuple[bool, str, str]] = {}

        for node in nodes:
            key = _node_key(node)
            name = _node_display_name(node)
            address = _node_address(node)
            connected = _node_is_connected(node)

            new_states[key] = (connected, name, address)

            if self._initialized:
                prev = self._last_states.get(key)
                if prev is not None:
                    prev_connected = prev[0]
                    if prev_connected and not connected:
                        # Node went down
                        logging.warning("NodeMonitor: node DOWN: %s (%s)", name, address)
                        try:
                            await self.notification_service.notify_node_down(name, address)
                        except Exception as e:
                            logging.error("NodeMonitor: failed to send node_down alert: %s", e)
                    elif not prev_connected and connected:
                        # Node recovered
                        logging.info("NodeMonitor: node RECOVERED: %s (%s)", name, address)
                        try:
                            await self.notification_service.notify_node_recovered(name, address)
                        except Exception as e:
                            logging.error("NodeMonitor: failed to send node_recovered alert: %s", e)
                else:
                    # New node appeared — just record it
                    if not connected:
                        logging.warning("NodeMonitor: new node detected as offline: %s (%s)", name, address)
                        try:
                            await self.notification_service.notify_node_down(name, address)
                        except Exception as e:
                            logging.error("NodeMonitor: failed to send node_down alert: %s", e)

            status_icon = "🟢" if connected else "🔴"
            lines.append(f"{status_icon} {name} ({address})")

        # Detect nodes that disappeared from the list entirely
        if self._initialized:
            for key, (was_connected, name, address) in self._last_states.items():
                if key not in new_states and was_connected:
                    logging.warning("NodeMonitor: node disappeared from API: %s (%s)", name, address)
                    try:
                        await self.notification_service.notify_node_down(name, address)
                    except Exception as e:
                        logging.error("NodeMonitor: failed to send node_down alert: %s", e)

        self._last_states = new_states
        self._initialized = True

        if not lines:
            return "Нод не найдено"
        return "\n".join(lines)

    async def _monitor_loop(self):
        interval = max(1, self.settings.NODE_MONITOR_INTERVAL_MINUTES) * 60
        logging.info(
            "NodeMonitor: started, interval=%d minutes",
            self.settings.NODE_MONITOR_INTERVAL_MINUTES,
        )
        while True:
            try:
                await self.check_nodes()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logging.error("NodeMonitor: error in check loop: %s", e, exc_info=True)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                logging.info("NodeMonitor: task cancelled")
                return

    def start_monitoring(self):
        """Start the background node monitoring task."""
        if not self.settings.NODE_MONITOR_ENABLED:
            logging.info("NodeMonitor: disabled by NODE_MONITOR_ENABLED=False")
            return
        self._task = asyncio.create_task(self._monitor_loop(), name="NodeMonitorTask")

    def stop(self):
        """Cancel the background monitoring task."""
        if self._task and not self._task.done():
            self._task.cancel()
