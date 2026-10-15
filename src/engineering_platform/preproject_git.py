"""Finite inert Git metadata reads for the first local inspection profile.

No Git subprocess, configuration evaluation, filter, hook, code execution or
network. Packed objects and unfamiliar metadata are explicitly unsupported;
ordinary Git/Genesis owns those formats, never this bounded read projection.
"""
from __future__ import annotations

import configparser
import hashlib
import re
import stat
import struct
import zlib

from .preproject_inspection import InspectionError, read_relative, relative_parts

_OID = re.compile(r"[0-9a-f]{40}\Z")


def unsupported() -> None:
    raise InspectionError("INSPECTION_GIT_FORMAT_UNSUPPORTED", 422)


def observe(fd: int, entries: dict[str, tuple[int, ...]], *, deadline: float) -> dict[str, object] | None:
    """Read SHA1 loose-object repositories and index2/3 without interpreting config."""
    if ".git" not in entries:
        if any(p.casefold() == ".git" for p in entries):
            unsupported()
        if {"HEAD", "objects", "refs"} <= set(entries):
            unsupported()  # Bare repository, not an ordinary local directory.
        if any(p.endswith("/.git") for p in entries):
            unsupported()
        return None
    if not stat.S_ISDIR(entries[".git"][2]):
        unsupported()  # gitfile / linked worktree / submodule / symlink.
    forbidden = (".git/commondir", ".git/shallow", ".git/info/grafts", ".git/objects/info/alternates",
        ".git/objects/info/http-alternates", ".git/index.lock", ".git/config.worktree", ".gitmodules")
    if any(p in entries for p in forbidden) or any(
            p.startswith((".git/worktrees/", ".git/refs/replace/", ".git/modules/"))
            or (p != ".git" and p.endswith("/.git")) for p in entries):
        unsupported()
    if any(p.casefold().split("/")[-1] == ".gitattributes" for p in entries):
        unsupported()
    for path, info in entries.items():
        if path.startswith(".git/") and not (stat.S_ISDIR(info[2]) or stat.S_ISREG(info[2])):
            unsupported()
    def read(path: str, maximum: int = 524288) -> bytes:
        if path not in entries:
            unsupported()
        return read_relative(fd, path, maximum=maximum, deadline=deadline)
    try:
        config = configparser.RawConfigParser(strict=True)
        config.read_string(read(".git/config", 65536).decode("utf-8"))
        if not config.has_section("core") or config.get("core", "repositoryformatversion") != "0":
            unsupported()
        if config.get("core", "bare", fallback="false").casefold() != "false":
            unsupported()
        if config.has_option("core", "worktree") or config.has_option("core", "attributesfile") or any(
                s.casefold().startswith(("include", "extensions", "submodule")) for s in config.sections()):
            unsupported()
        if (config.get("core", "autocrlf", fallback="false").casefold() not in {"false", "0", "no", "off"}
                or config.has_option("core", "eol") or config.get("core", "filemode", fallback="true").casefold() != "true"):
            unsupported()
        remote_present = any(s.casefold().startswith('remote "') for s in config.sections())
        # Values, URLs, helper commands and credentials never leave the host.
    except (UnicodeDecodeError, configparser.Error) as error:
        raise InspectionError("INSPECTION_GIT_METADATA_INVALID", 422) from error

    object_bytes = 0
    def obj(oid: str, kind: str) -> bytes:
        nonlocal object_bytes
        if _OID.fullmatch(oid) is None:
            unsupported()
        path = ".git/objects/" + oid[:2] + "/" + oid[2:]
        if path not in entries:
            unsupported()  # Includes packed/promised/alternate objects; no lazy fetch.
        compressed = read(path)
        decoder = zlib.decompressobj()
        try:
            raw = decoder.decompress(compressed, 1048577)
        except zlib.error as error:
            raise InspectionError("INSPECTION_GIT_OBJECT_INVALID", 422) from error
        object_bytes += len(raw)
        if len(raw) > 1048576 or object_bytes > 4194304 or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            unsupported()
        header, separator, body = raw.partition(b"\x00")
        if (not separator or header != f"{kind} {len(body)}".encode()
                or hashlib.sha1(raw, usedforsecurity=False).hexdigest() != oid):
            raise InspectionError("INSPECTION_GIT_OBJECT_INVALID", 422)
        return body

    head = read(".git/HEAD", 4096).decode("ascii").strip()
    unborn = False
    if head.startswith("ref: "):
        ref = head[5:]
        relative_parts(ref)
        if not ref.startswith("refs/heads/") or any(c in ref for c in (" ", "~", "^", ":", "?", "*", "[", "\\")):
            unsupported()
        if ".git/" + ref in entries:
            head = read(".git/" + ref, 4096).decode("ascii").strip()
        else:
            matches = []
            if ".git/packed-refs" in entries:
                for line in read(".git/packed-refs").decode("ascii").splitlines():
                    if line and not line.startswith(("#", "^")):
                        fields = line.split(" ")
                        if len(fields) != 2 or _OID.fullmatch(fields[0]) is None:
                            unsupported()
                        if fields[1] == ref:
                            matches.append(fields[0])
            if len(matches) > 1:
                unsupported()
            head = matches[0] if matches else None
            unborn = head is None
    if head is not None and _OID.fullmatch(head) is None:
        unsupported()

    committed = {}
    visited = set()
    def tree(oid: str, prefix: str = "", depth: int = 0) -> None:
        if depth > 16 or len(committed) > 512 or (oid, prefix) in visited:
            unsupported()
        visited.add((oid, prefix))
        raw = obj(oid, "tree")
        while raw:
            if len(committed) >= 512:
                unsupported()
            mode, space, remaining = raw.partition(b" ")
            name, zero, remaining = remaining.partition(b"\x00")
            if not space or not zero or len(remaining) < 20:
                unsupported()
            item_oid, raw = remaining[:20].hex(), remaining[20:]
            text = name.decode("utf-8")
            relative_parts(text)
            if "/" in text or text.casefold() == ".git":
                unsupported()
            path = prefix + text
            if mode == b"40000":
                tree(item_oid, path + "/", depth + 1)
            elif mode in {b"100644", b"100755"} and path not in committed:
                committed[path] = (int(mode, 8), item_oid)
            else:
                unsupported()
    tree_oid = None
    if not unborn:
        first = obj(head, "commit").split(b"\n", 1)[0]
        if not first.startswith(b"tree "):
            unsupported()
        tree_oid = first[5:].decode("ascii")
        tree(tree_oid)

    indexed, unresolved = {}, 0
    if ".git/index" in entries:
        index = read(".git/index")
        if (len(index) < 32 or index[:4] != b"DIRC"
                or hashlib.sha1(index[:-20], usedforsecurity=False).digest() != index[-20:]):
            unsupported()
        version, count = struct.unpack("!II", index[4:12])
        if version not in {2, 3} or count > 512:
            unsupported()
        offset = 12
        for _ in range(count):
            start = offset
            if offset + 62 > len(index) - 20:
                unsupported()
            mode = struct.unpack("!I", index[offset + 24:offset + 28])[0]
            oid = index[offset + 40:offset + 60].hex()
            flags = struct.unpack("!H", index[offset + 60:offset + 62])[0]
            offset += 62
            if flags & 0xC000:  # Extended flags / assume-unchanged are outside V1.
                unsupported()
            end = index.find(b"\x00", offset, len(index) - 20)
            if end < 0:
                unsupported()
            path = index[offset:end].decode("utf-8")
            relative_parts(path)
            if any(p.casefold() == ".git" for p in path.split("/")) or mode not in {0o100644, 0o100755}:
                unsupported()
            if flags & 0x3000:
                unresolved += 1
            elif path in indexed:
                unsupported()
            else:
                indexed[path] = (mode, oid)
            offset = start + ((end + 1 - start + 7) // 8) * 8
        while offset < len(index) - 20:
            if offset + 8 > len(index) - 20:
                unsupported()
            extension, size = index[offset:offset + 4], struct.unpack("!I", index[offset + 4:offset + 8])[0]
            if extension not in {b"TREE", b"REUC", b"UNTR", b"FSMN"}:
                unsupported()
            offset += 8 + size
        if offset != len(index) - 20:
            unsupported()
    if len({p.casefold() for p in indexed}) != len(indexed):
        unsupported()
    staged = sum(committed.get(p) != indexed.get(p) for p in set(committed) | set(indexed))
    unstaged, bytes_hashed = 0, 0
    for path, (mode, expected) in indexed.items():
        if path not in entries:
            unstaged += 1
            continue
        if not stat.S_ISREG(entries[path][2]):
            unsupported()
        raw = read(path)
        bytes_hashed += len(raw)
        if bytes_hashed > 4194304:
            unsupported()
        actual = hashlib.sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest()
        if actual != expected or bool(entries[path][2] & 0o111) != (mode == 0o100755):
            unstaged += 1
    # Ignored files are intentionally counted as untracked candidates; no
    # .gitignore or repository-config evaluation and no false clean claim.
    untracked = sum(not stat.S_ISDIR(info[2]) and p not in indexed
        and not p.startswith(".git/") for p, info in entries.items())
    return {"state": "UNBORN_GIT" if unborn else "EXISTING_GIT", "head": head, "tree": tree_oid,
        "staged_count": staged, "unstaged_count": unstaged, "untracked_candidate_count": untracked,
        "unresolved_index_entries": unresolved, "remote_present": remote_present,
        "ignore_rules": "NOT_EVALUATED", "git_execution": "NONE", "hashed_worktree_bytes": bytes_hashed}
