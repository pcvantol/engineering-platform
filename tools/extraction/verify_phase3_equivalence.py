#!/usr/bin/env python3
"""Fail-closed two-stage extraction and post-extraction provenance audit."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path, PureWindowsPath
import subprocess

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = ROOT / "docs/provenance/PHASE_3_INCREMENT_1_EQUIVALENCE_BASELINE.json"
DEFAULT_LEDGER = ROOT / "docs/provenance/PHASE_3_POST_EXTRACTION_EVOLUTION_LEDGER.json"
DEFAULT_RECEIPT = ROOT / "docs/provenance/phase3-file-equivalence.json"
TYPES = {"GOVERNED_MODIFICATION", "GOVERNED_RENAME", "GOVERNED_SPLIT", "GOVERNED_MERGE", "INTENTIONAL_RETIREMENT"}

def sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()
def normalized(text: str) -> str: return text.replace("tools.engineering", "engineering_platform").replace("tools/engineering", "src/engineering_platform")
def safe(value: object) -> bool:
    if not isinstance(value, str) or not value: return False
    p, w = Path(value), PureWindowsPath(value)
    return not p.is_absolute() and not w.is_absolute() and ".." not in p.parts
def file(root: Path, relative: str) -> Path:
    if not safe(relative): raise ValueError(f"unsafe relative path: {relative!r}")
    path, resolved = root / relative, root.resolve()
    if resolved not in path.resolve().parents: raise ValueError(f"path escapes root: {relative!r}")
    return path
def run(root: Path, *args: str, binary=False):
    result = subprocess.run(["git", "-C", str(root), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if result.returncode: raise ValueError(result.stderr.decode().strip() or "git validation failed")
    return result.stdout if binary else result.stdout.decode().strip()
def blob(root: Path, ref: str, path: str) -> bytes:
    if not safe(path): raise ValueError(f"unsafe relative path: {path!r}")
    return run(root, "show", f"{ref}:{path}", binary=True)
def load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict): raise ValueError(f"{path.name} must be an object")
    return value
def ancestor(root: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", older, newer], check=False).returncode == 0
def responsibility_id(path: str) -> str: return f"{path}::module"
def validate_responsibilities(expected, destinations, retirements, errors, label):
    claims,shared,retired={},set(),set()
    for destination in destinations:
        if not isinstance(destination,dict) or not safe(destination.get("path")) or not isinstance(destination.get("responsibilities"),list): errors.append(f"invalid responsibility destination: {label}"); continue
        for item in destination["responsibilities"]:
            if item not in expected: errors.append(f"unknown responsibility claim: {item}")
            else: claims[item]=claims.get(item,0)+1
        shared.update(destination.get("shared_responsibilities",[]))
    for retirement in retirements:
        if not isinstance(retirement,dict) or retirement.get("responsibility") not in expected or not retirement.get("reason"): errors.append(f"invalid retirement evidence: {label}")
        else: retired.add(retirement["responsibility"])
    for item in expected:
        if item not in claims and item not in retired: errors.append(f"unaccounted responsibility: {item}")
        if item in claims and item in retired: errors.append(f"responsibility both current and retired: {item}")
        if claims.get(item,0)>1 and item not in shared: errors.append(f"duplicate non-shared responsibility: {item}")
        if item in shared and claims.get(item,0)<2: errors.append(f"shared responsibility lacks multiple destinations: {item}")
def cycle_free(edges):
    graph={}
    for left,right in edges: graph.setdefault(left,[]).append(right)
    active,done=set(),set()
    def visit(node):
        if node in active:return False
        if node in done:return True
        active.add(node); result=all(visit(child) for child in graph.get(node,[])); active.remove(node); done.add(node); return result
    return all(visit(node) for node in graph)
def validate_graph(graph, repo=None, anchor=None, qualified=None):
    """Validate a bounded explicit responsibility-flow DAG; returns diagnostics."""
    errors=[]; nodes={n.get("node_id"):n for n in graph.get("nodes",[]) if isinstance(n,dict) and isinstance(n.get("node_id"),str)}; edges=graph.get("edges",[])
    if len(nodes)>512 or len(edges)>2048: return ["graph exceeds bounded qualification size"]
    seen=set(); pairs=[]; origins={}
    for node in nodes.values():
        if node.get("node_type")=="BASELINE":
            for r in node.get("responsibilities",[]): origins[r]=node["node_id"]
    for edge in edges:
        if not isinstance(edge,dict) or edge.get("kind") not in {"MODIFY","MOVE","RENAME","SPLIT","MERGE","REPLACE","RETIRE"}: errors.append("invalid edge type"); continue
        key=(edge.get("from"),edge.get("to"),tuple(edge.get("responsibilities",[])))
        if key in seen: errors.append("duplicate edge")
        seen.add(key); pairs.append((edge.get("from"),edge.get("to")))
        if edge.get("from") not in nodes or edge.get("to") not in nodes or edge.get("from")==edge.get("to"): errors.append("invalid graph endpoint")
        commit=edge.get("governed_commit")
        if repo is not None and (not isinstance(commit,str) or len(commit)!=40 or not ancestor(repo,anchor,commit) or not ancestor(repo,commit,qualified)): errors.append("invalid edge revision")
        for r in edge.get("responsibilities",[]):
            if r not in origins: errors.append(f"invented responsibility: {r}")
    if errors or not cycle_free(pairs): return errors+["cycle detected"]
    if repo is not None:
        for edge in edges:
            for predecessor in (item for item in edges if item.get("to")==edge.get("from")):
                if not ancestor(repo, predecessor.get("governed_commit"), edge.get("governed_commit")):
                    errors.append("backwards edge chronology")
    for r,start in origins.items():
        active=[start]; terminals=[]; visited=set()
        while active:
            node=active.pop(); key=(node,r)
            if key in visited: continue
            visited.add(key); outgoing=[e for e in edges if e.get("from")==node and r in e.get("responsibilities",[])]
            kind=nodes[node].get("node_type")
            if kind in {"CURRENT_TARGET","RETIRED"}:
                if outgoing: errors.append(f"terminal has outgoing flow: {r}")
                terminals.append(node); continue
            if not outgoing: errors.append(f"dangling responsibility: {r}"); continue
            active.extend(e["to"] for e in outgoing)
        if len(terminals)!=1: errors.append(f"invalid terminal count for {r}")
    for node in nodes.values():
        if node.get("node_type")=="CURRENT_TARGET" and not any(e.get("to")==node["node_id"] for e in edges): errors.append(f"orphan current target: {node['node_id']}")
    if repo is not None:
        for node in nodes.values():
            if node.get("node_type")=="CURRENT_TARGET":
                try:
                    if not safe(node.get("path")): errors.append("unsafe current node path")
                    elif not file(repo,node["path"]).is_file(): errors.append("missing current terminal path")
                    elif node.get("sha256") and sha(file(repo,node["path"]).read_bytes()) != node["sha256"]: errors.append("current terminal blob mismatch")
                except OSError: errors.append("missing current terminal path")
    return errors

def traces(graph, responsibility=None, current_path=None):
    nodes={n.get("node_id"):n for n in graph.get("nodes",[]) if isinstance(n,dict)}; edges=graph.get("edges",[]); result=[]
    for base in nodes.values():
        if base.get("node_type")!="BASELINE": continue
        for r in base.get("responsibilities",[]):
            if responsibility and r!=responsibility: continue
            stack=[([base["node_id"]],base["node_id"])]; paths=[]
            while stack:
                chain,node=stack.pop(); outgoing=[e for e in edges if e.get("from")==node and r in e.get("responsibilities",[])]
                if not outgoing and nodes[node].get("node_type") in {"CURRENT_TARGET","RETIRED"}: paths.append(chain); continue
                stack.extend((chain+[e["to"]],e["to"]) for e in outgoing)
            for chain in paths:
                terminal=nodes[chain[-1]]
                if current_path and terminal.get("path")!=current_path: continue
                result.append({"responsibility":r,"nodes":chain,"terminal":terminal.get("node_type"),"current_path":terminal.get("path")})
    return result

def reverse_lookup(graph, path):
    matches=[trace for trace in traces(graph) if trace.get("current_path")==path]
    return {"classification":"HISTORICALLY_DESCENDED","traces":matches} if matches else {"classification":"NEW_POST_EXTRACTION","traces":[]}

def unchanged_traces(rows, target):
    """Direct immutable-baseline terminal evidence; no synthetic Stage-2 edge."""
    result=[]
    for row in rows:
        path=row["target_path"]
        try: current=sha(file(target,path).read_bytes())
        except OSError: continue
        if current == row["target_final_digest"]:
            result.append({"responsibility":responsibility_id(path),"historical_source_path":row["source_path"],"baseline_path":path,"nodes":[f"baseline:{path}",f"current:{path}"],"terminal":"CURRENT_TARGET","current_path":path,"baseline_sha256":row["target_final_digest"],"current_sha256":current,"lineage_classification":"BASELINE_UNCHANGED"})
    return result

def stage1(source: Path, target: Path, baseline: dict, receipt: dict, ledger: dict, errors: list[str]) -> list[dict]:
    identity = ledger.get("historical_extraction", {})
    source_ref, target_ref = identity.get("source_commit"), identity.get("target_baseline_commit")
    if not isinstance(source_ref, str) or not isinstance(target_ref, str):
        errors.append("ledger historical extraction identity is malformed"); return []
    try:
        if run(source, "rev-parse", "HEAD") != source_ref: errors.append("historical source checkout is not at recorded commit")
        if run(source, "status", "--porcelain"): errors.append("historical source checkout is not clean")
        run(target, "cat-file", "-e", f"{target_ref}^{{commit}}")
    except ValueError as error: errors.append(str(error)); return []
    expected = identity.get("equivalence_baseline_sha256")
    if expected != sha(json.dumps(baseline, sort_keys=True, separators=(",", ":")).encode()): errors.append("immutable equivalence baseline receipt digest mismatch")
    if identity.get("historical_equivalence_receipt_sha256") != sha(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()): errors.append("historical equivalence receipt digest mismatch")
    approved = {x.get("target_path"): x for x in baseline.get("allowed_divergences", []) if isinstance(x, dict)}
    rows=[]
    for source_file in sorted((source / "tools/engineering").rglob("*.py")):
        src=source_file.relative_to(source).as_posix(); dst="src/engineering_platform/" + source_file.relative_to(source / "tools/engineering").as_posix()
        try:
            before, after = source_file.read_bytes(), blob(target, target_ref, dst)
            source_digest, target_digest=sha(before),sha(after)
            categories=["namespace_import"] if before != after else []
            if normalized(before.decode("utf-8")) != after.decode("utf-8"):
                rule=approved.get(dst)
                if not rule or rule.get("source_digest") != source_digest or rule.get("target_digest") != target_digest: errors.append(f"historical source-to-target equivalence mismatch: {src}")
                else: categories.append(str(rule.get("category")))
            rows.append({"source_path":src,"target_path":dst,"source_digest":source_digest,"target_pre_rewrite_digest":source_digest,"target_final_digest":target_digest,"rewrite_categories":categories})
        except (OSError, ValueError) as error: errors.append(str(error))
    for addition in baseline.get("allowed_additions", []):
        if not isinstance(addition,dict) or not safe(addition.get("target_path")): errors.append("malformed historical addition"); continue
        dst=addition["target_path"]
        try:
            data=blob(target,target_ref,dst)
            if sha(data) != addition.get("target_digest"): errors.append(f"immutable extraction target mutation, deletion, or rename: {dst}")
            rows.append({"source_path":None,"target_path":dst,"source_digest":None,"target_pre_rewrite_digest":None,"target_final_digest":sha(data),"rewrite_categories":[str(addition.get("category"))]})
        except ValueError as error: errors.append(str(error))
    digest = sha(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode())
    if baseline.get("candidate_baseline_digest") != digest: errors.append("historical candidate baseline digest mismatch")
    return [r for r in rows if isinstance(r.get("source_path"), str)]

def stage2(target: Path, rows: list[dict], ledger: dict, errors: list[str]) -> list[dict]:
    identity, records = ledger.get("historical_extraction", {}), ledger.get("evolutions")
    if not isinstance(records, list): errors.append("ledger evolutions must be a list"); return []
    baseline, head = identity.get("standalone_lineage_anchor_commit", identity.get("target_baseline_commit")), run(target, "rev-parse", "HEAD")
    indexed = {}
    for record in records:
        key = record.get("historical_target_path") if isinstance(record, dict) else None
        if not isinstance(key, str) or key in indexed: errors.append(f"malformed or duplicate evolution record: {key}")
        else: indexed[key] = record
    inventory=[]
    for row in rows:
        path, old = row["target_path"], row["target_final_digest"]
        try:
            if sha(blob(target, baseline, path)) != old: errors.append(f"lineage anchor differs from immutable extraction target: {path}")
        except ValueError as error: errors.append(str(error))
        try: current = sha(file(target, path).read_bytes()) if file(target, path).is_file() else None
        except (OSError, ValueError): current = None
        if current == old: inventory.append({"path":path,"classification":"UNCHANGED"}); continue
        record=indexed.pop(path, None)
        if not record: errors.append(f"unaccounted current target mutation, deletion, or rename: {path}"); inventory.append({"path":path,"classification":"UNACCOUNTED"}); continue
        kind, destinations = record.get("evolution_type"), record.get("destinations")
        if kind not in TYPES or record.get("baseline_sha256") != old or not isinstance(destinations,list): errors.append(f"invalid governed evolution receipt: {path}"); destinations=[]
        validate_responsibilities({responsibility_id(path)},destinations,record.get("retirements",[]),errors,path)
        if kind == "GOVERNED_MODIFICATION" and (len(destinations)!=1 or destinations[0].get("path") != path): errors.append(f"modification changes path: {path}")
        for destination in destinations:
            dst=destination.get("path")
            try: actual=sha(file(target,dst).read_bytes()) if file(target,dst).is_file() else None
            except (OSError,ValueError): actual=None
            if kind != "INTENTIONAL_RETIREMENT" and actual != destination.get("current_sha256"): errors.append(f"unaccounted current content: {dst}")
        first,last=record.get("first_governed_commit"),record.get("last_governed_commit")
        if not all(isinstance(x,str) and len(x)==40 and ancestor(target,baseline,x) and ancestor(target,x,head) for x in (first,last)): errors.append(f"invalid or unreachable governed commit chain: {path}")
        elif kind != "INTENTIONAL_RETIREMENT":
            for destination in destinations:
                try:
                    if sha(blob(target,last,destination["path"])) != destination.get("current_sha256"): errors.append(f"last governed commit does not prove current content: {path}")
                except ValueError as error: errors.append(str(error))
        inventory.append({"path":path,"classification":kind})
    for path in indexed: errors.append(f"evolution receipt has no historical target: {path}")
    return inventory

def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,required=True); parser.add_argument("--target",type=Path,default=ROOT)
    parser.add_argument("--baseline",type=Path,default=DEFAULT_BASELINE); parser.add_argument("--historical-receipt",type=Path,default=DEFAULT_RECEIPT); parser.add_argument("--ledger",type=Path,default=DEFAULT_LEDGER); parser.add_argument("--qualified-revision",required=True); parser.add_argument("--lookup-historical"); parser.add_argument("--lookup-current"); parser.add_argument("--receipt",type=Path)
    args=parser.parse_args(); errors=[]
    try:
        ledger=load(args.ledger); target=args.target.resolve(); anchor=ledger["historical_extraction"]["standalone_lineage_anchor_commit"]; qualified=run(target,"rev-parse",args.qualified_revision)
        if qualified != run(target,"rev-parse","HEAD"): errors.append("checkout HEAD does not equal qualified revision")
        errors.extend(validate_graph(ledger.get("graph",{}),target,anchor,qualified)); rows=stage1(args.source.resolve(),target,load(args.baseline),load(args.historical_receipt),ledger,errors); inventory=stage2(target,rows,ledger,errors)
    except (OSError,ValueError,json.JSONDecodeError) as error: errors.append(str(error)); inventory=[]
    evolved=traces(ledger.get("graph",{})) if 'ledger' in locals() else []
    all_traces=evolved+unchanged_traces(rows,target) if 'rows' in locals() else evolved
    selected=[trace for trace in all_traces if not args.lookup_historical or trace["responsibility"]==args.lookup_historical]
    reverse={"classification":"HISTORICALLY_DESCENDED","traces":[trace for trace in all_traces if trace.get("current_path")==args.lookup_current]} if args.lookup_current and any(trace.get("current_path")==args.lookup_current for trace in all_traces) else ({"classification":"NEW_POST_EXTRACTION","traces":[]} if args.lookup_current else None)
    total=len({trace["responsibility"] for trace in all_traces}); accounted=len({trace["responsibility"] for trace in all_traces if trace["terminal"] in {"CURRENT_TARGET","RETIRED"}})
    result={"model":"TWO_STAGE_PROVENANCE","qualified_revision":args.qualified_revision,"stage_1":"HISTORICAL_EXTRACTION_PROVENANCE","stage_2":"GOVERNED_POST_EXTRACTION_EVOLUTION","inventory":inventory,"unaccounted":sum(x["classification"]=="UNACCOUNTED" for x in inventory),"total_historical_responsibilities":total,"accounted_responsibilities":accounted,"unaccounted_responsibilities":total-accounted,"traces":selected,"reverse_lookup":reverse,"failures":errors,"pass":not errors}
    if args.receipt: args.receipt.write_text(json.dumps(result,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"files":len(inventory),"unaccounted":result["unaccounted"],"failures":errors,"pass":not errors},sort_keys=True)); return 0 if not errors else 1
if __name__ == "__main__": raise SystemExit(main())
