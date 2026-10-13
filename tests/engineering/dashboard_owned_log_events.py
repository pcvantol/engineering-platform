"""Read finite lifecycle and Console-audit producers without importing the product."""
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = ("agent_state.py", "execution_host.py", "parity_lifecycle_dispatcher.py")
rows = []
forwarders = []

class ProducerCalls(ast.NodeVisitor):
    def __init__(self, path):
        self.path = path
        self.functions = []

    def visit_FunctionDef(self, node):
        self.functions.append(node.name)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in {"log_event", "record_technical_diagnostic", "_record_technical_diagnostic"}:
            event = next((value.value for value in node.keywords if value.arg == "event"), None)
            if event is None and name == "log_event" and len(node.args) > 2:
                event = node.args[2]
            if isinstance(event, ast.Constant) and isinstance(event.value, str):
                rows.append({"event": event.value, "file": str(self.path.relative_to(ROOT)), "line": node.lineno, "callee": name})
            elif name == "record_technical_diagnostic" and self.functions[-1:] == ["_record_technical_diagnostic"] and isinstance(event, ast.Name) and event.id == "event":
                component = next((value.value for value in node.keywords if value.arg == "component"), None)
                if not isinstance(component, ast.Constant) or component.value != "lifecycle_worker":
                    raise AssertionError("Technical diagnostic adapter changed its owning component")
                forwarders.append({"file": str(self.path.relative_to(ROOT)), "line": node.lineno})
            else:
                raise AssertionError(f"Unresolved owning producer event {self.path.name}:{node.lineno}")
        self.generic_visit(node)

for filename in SOURCES:
    path = ROOT / "src/engineering_platform" / filename
    ProducerCalls(path).visit(ast.parse(path.read_text(), filename=str(path)))
# The actual Console audit producer has a conditional literal event, and an
# accepted cancellation persists its audit row directly in CENTRAL. Resolve
# only these named owning functions; unrelated Server producers are not a
# generic event whitelist and unresolved changes fail closed.
def owning_function(filename, name):
    path = ROOT / "src/engineering_platform" / filename
    module = ast.parse(path.read_text(), filename=str(path))
    matches = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1, f"Ambiguous owning Console audit function {filename}:{name}"
    return path, matches[0]

path, function = owning_function("server.py", "_audit_dashboard_action")
calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
         and isinstance(node.func, ast.Name) and node.func.id == "log_event"]
assert len(calls) == 1 and len(calls[0].args) > 2, "Console audit must have one actual log producer"
event = calls[0].args[2]
assert isinstance(event, ast.IfExp), "Console audit must retain its finite conditional event class"
audit_events = []
for branch in (event.body, event.orelse):
    assert isinstance(branch, ast.Constant) and isinstance(branch.value, str), "Unresolved Console audit branch"
    audit_events.append(branch.value)
    rows.append({"event": branch.value, "file": str(path.relative_to(ROOT)), "line": calls[0].lineno,
                 "callee": "log_event", "producer_scope": "console_audit_conditional"})
assert len(set(audit_events)) == 2, "Console audit outcomes must remain distinct"

path, function = owning_function("parallel_action_recovery.py", "request_cancel")
central_events = [value for node in ast.walk(function) if isinstance(node, ast.Dict)
                  for key, value in zip(node.keys, node.values)
                  if isinstance(key, ast.Constant) and key.value == "event"]
assert len(central_events) == 1, "CENTRAL cancellation must have one actual audit event"
event = central_events[0]
assert isinstance(event, ast.Constant) and isinstance(event.value, str) and event.value in audit_events, "Unresolved CENTRAL audit event"
rows.append({"event": event.value, "file": str(path.relative_to(ROOT)), "line": event.lineno,
             "callee": "CENTRAL_transaction", "producer_scope": "console_audit_cancellation"})

assert len(forwarders) == 1, "The technical adapter must have one actual symbolic forwarding call"
print(json.dumps({"events": sorted({row["event"] for row in rows}), "calls": rows, "technical_forwarders": forwarders}, sort_keys=True))
