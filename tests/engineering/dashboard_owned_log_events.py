"""Read the finite owning producer call sites without importing the product."""
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
assert len(forwarders) == 1, "The technical adapter must have one actual symbolic forwarding call"
print(json.dumps({"events": sorted({row["event"] for row in rows}), "calls": rows, "technical_forwarders": forwarders}, sort_keys=True))
