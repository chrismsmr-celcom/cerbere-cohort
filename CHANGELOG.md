# Changelog

All notable changes to Cohort are documented here. The project follows Semantic Versioning; while the major version is 0, minor releases may contain breaking changes.

## [0.1.1] - 2026-11-24

### Added
- Agent credential management in workspace settings.
- PostgreSQL integration coverage for shared branch state across REST and MCP clients.

### Changed
- Unify REST and MCP access to the durable workspace branch registry and task state.
- Preserve legacy branch data during registry migration.

### Fixed
- Serialize task operations on the shared service connection.
- Reload durable workspace state for branch reads and handle legacy branch schemas atomically.

### Notes
- Cohort remains in active development. Review the README for current capability boundaries and known limitations.

[Unreleased]: https://github.com/chrismsmr-celcom/cerbere-cohort/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/chrismsmr-celcom/cerbere-cohort/releases/tag/v0.1.1
