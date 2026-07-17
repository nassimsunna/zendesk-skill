"""Zendesk MCP Server - A Python MCP server for Zendesk API integration."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("zd-cli")
except PackageNotFoundError:
    __version__ = "0.0.0"

from zendesk_skill.client import ZendeskAPIError, ZendeskAuthError, ZendeskClient
