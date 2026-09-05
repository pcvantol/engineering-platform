# Component compatibility matrix

**Status:** LR-12 closure. `PLATFORM_COMPONENTS` is the singular supported
component model.

| Historical alias | Remaining consumer | Writes/UI/lifecycle/log authority | Retirement condition |
| --- | --- | --- | --- |
| `dashboard` | historical migration/provenance text only | none | no installed runtime consumer; retired now |
| `inbox` | historical migration/provenance text only | none | no installed runtime consumer; retired now |
| `inbox-watcher` | read-only migration/provenance text only | none | retained only until migration evidence retirement |

The canonical replacements are `operations_console` and
`file_inbox_ingress`. They are distinct canonical identities, not aliases.
Only the canonical model provides status, selectable Console identity, route
validation, component logging, or lifecycle metadata.
