# Architecture

KNOCK uses a clean, modular architecture:

- **Core**: domain models and orchestration.
- **Conversation**: policy, intent, and response templates.
- **Providers**: pluggable interfaces for AI and hardware capabilities.
- **API/CLI**: entry points that call into the orchestrator.

The core does not depend on concrete hardware or model implementations.
