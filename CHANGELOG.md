# Changelog

All notable changes to Task Scheduler Pro will be documented in this file.

## [1.0.4] - 2024-12-15

### Added
- 💡 **Device Control** - New action type to turn on/off/toggle devices
  - Supports lights, switches, fans, covers, climate, input_booleans, and media players
  - Three actions: Turn On, Turn Off, Toggle

### Fixed
- Improved API path handling for Home Assistant ingress compatibility

## [1.0.3] - 2024-12-15

### Fixed
- Fixed API calls not working through Home Assistant ingress
- Changed to relative URL paths for proper ingress support

## [1.0.2] - 2024-12-15

### Fixed
- Fixed add-ons list not loading (403 Forbidden error)
- Now fetches add-ons from `/supervisor/info` endpoint which doesn't require additional permissions

### Changed
- Added cache-control headers to prevent stale UI issues

## [1.0.1] - 2024-12-15

### Fixed
- Improved error handling and debugging
- Added console logging for troubleshooting

## [1.0.0] - 2024-12-15

### Added
- Initial release
- 🔄 Reboot Host action
- 🏠 Restart Home Assistant action
- 📦 Restart Add-on action
- ⚡ Call Service action with custom JSON data
- 🤖 Trigger Automation action
- 📜 Run Script action
- Interval-based scheduling (minutes, hours, days)
- Cron-style scheduling (specific times and days)
- Beautiful dark-themed UI
- Task enable/disable toggle
- Manual task execution
- Execution history tracking
- Real-time task status updates
